#!/usr/bin/python3
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from benchmark_context import benchmark

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = Path.home() / "Library/Application Support/radeon-llm"
BIN = RUNTIME / "llama.cpp/build-vulkan/bin/llama-server"
PORT = 18082


def main(dry_run=False, experimental_fa=False, ops_only=False, short_only=False, request_timeout=2400):
    config = json.loads((ROOT / "server/config.json").read_text())
    sdk = RUNTIME / "vulkan-sdk/macOS"
    driver = RUNTIME / "moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS"
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GGML_VK_") or key.startswith("LLAMA_ARG_"):
            del env[key]
    env.update(VULKAN_SDK=str(sdk), VK_DRIVER_FILES=str(driver / "MoltenVK_icd.json"),
               VK_ICD_FILENAMES=str(driver / "MoltenVK_icd.json"),
               DYLD_LIBRARY_PATH=":".join([str(driver), str(sdk / "lib"), str(BIN.parent)]))
    binary = BIN
    prefix = "context16k"
    if experimental_fa:
        binary = ROOT / "experimental/bin/llama-server"
        assert binary.exists(), "Run ./build-experimental.command first"
        env["GGML_VK_AMD_MOLTENVK_FA_SHARED"] = "1"
        env["MVK_CONFIG_FAST_MATH_ENABLED"] = "0"
        env["DYLD_LIBRARY_PATH"] = ":".join([str(driver), str(sdk / "lib"), str(binary.parent)])
        prefix = "context16k-gpu-fa"
    key_path = RUNTIME / "server/api-key"
    assert BIN.exists() and key_path.exists(), "Install the stable daemon first"
    token = key_path.read_text().strip()
    assert token == (ROOT / "server/api-key").read_text().strip(), "Runtime/project API keys differ"
    device = "Vulkan0"
    if not dry_run:
        probe = subprocess.run([str(binary), "--list-devices"], env=env, capture_output=True,
                               text=True, timeout=90)
        match = re.search(r"^\s*(Vulkan\d+):.*Radeon", probe.stdout, re.M)
        assert probe.returncode == 0 and match, "Radeon unavailable; run from the macOS Terminal"
        device = match.group(1)
    args = [str(binary), "-m", str(RUNTIME / config["model"]), "--alias", config["alias"],
            "--host", "127.0.0.1", "--port", str(PORT), "-c", "16384", "--parallel", "1",
            "--device", device, "-ngl", "99", "--fit", "off", "-b", "128", "-ub", "128", "-t", "8",
            "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--cache-ram", "0",
            "--jinja", "--no-context-shift", "--no-webui", "--api-key-file", str(key_path),
            "--temp", "0", "--verbosity", "4", "--no-warmup"]
    if dry_run:
        print(json.dumps({"test_context": 16384, "K": "q8_0", "V": "q8_0", "flash_attention": True,
                          "port": PORT, "server_args": args, "stable_config_changed": False,
                          "experimental_fa": experimental_fa,
                          "shared_memory_attention": experimental_fa,
                          "strict_metal_math": experimental_fa,
                          "ops_only": ops_only, "short_only": short_only,
                          "request_timeout_seconds": request_timeout}, indent=2))
        return
    status = subprocess.check_output([str(ROOT / "daemon.command"), "status"], text=True)
    assert "API: ready" in status, "Stable server must be ready before the experiment"
    log_path = ROOT / ("logs/%s-server.log" % prefix)
    process = None
    failed = None
    stopped = False
    try:
        print("Stopping stable 8k daemon temporarily...", flush=True)
        stopped = True
        subprocess.run([str(ROOT / "daemon.command"), "stop"], check=True)
        time.sleep(3)
        if experimental_fa:
            tester = binary.parent / "test-backend-ops"
            tests = [
                ("matrix", "MUL_MAT", "type_a=(f32|f16|q4_K|q6_K),type_b=f32,m=16,n=(1|8),k=(256|4096),", 40),
                ("kv-rotation", "MUL_MAT_HADAMARD", r"type_a=f32,type_b=(f32|f16),m=(64|128),n=(1|32),k=(64|128),", 9),
                ("attention", "FLASH_ATTN_EXT", r"hsk=128,hsv=128,nh=8,nr23=\[4,1\],kv=(128|256|1024|2048|8192|16384),nb=(1|2|3|7|21|31|32|64|128),.*type_K=q8_0,type_V=q8_0", 34),
            ]
            for label, operation, pattern, minimum in tests:
                destination = ROOT / ("logs/experimental-%s-tests.log" % label)
                print("Checking %s operations against CPU; log: %s" % (label, destination), flush=True)
                with destination.open("w") as test_log:
                    check = subprocess.run([str(tester), "test", "-b", device, "-o", operation, "-p", pattern],
                                           env=env, stdout=test_log, stderr=subprocess.STDOUT, timeout=600)
                text = destination.read_text(errors="replace")
                summaries = re.findall(r"(\d+)/(\d+) tests passed", text)
                if check.returncode != 0 or not summaries:
                    raise RuntimeError("Operation test failed: %s\n%s" % (
                        destination, "\n".join(text.splitlines()[-35:])))
                passed, total = map(int, summaries[-1])
                assert passed == total and total >= minimum, ("Insufficient passing coverage", label, summaries)
                print("%s: %d/%d passed" % (label, passed, total), flush=True)
            if ops_only:
                print("Operation checks passed; skipping model benchmark.", flush=True)
                return
        with log_path.open("w") as logfile:
            process = subprocess.Popen(args, env=env, stdout=logfile, stderr=subprocess.STDOUT)
            print("Starting isolated 16k/Q8 server; log: " + str(log_path), flush=True)
            deadline = time.monotonic() + 240
            while True:
                if process.poll() is not None:
                    raise RuntimeError("16k server exited during startup")
                try:
                    req = urllib.request.Request("http://127.0.0.1:%d/health" % PORT,
                                                 headers={"Authorization": "Bearer " + token})
                    with urllib.request.urlopen(req, timeout=2) as response:
                        if json.load(response).get("status") == "ok":
                            break
                except (OSError, urllib.error.URLError):
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("16k server startup timed out")
                time.sleep(2)
            startup_log = log_path.read_text(errors="replace")
            for line in startup_log.splitlines():
                if any(item in line for item in ["offloaded 37/37", "KV buffer size", "flash_attn ", "n_ctx ", "K (q8_0)"]):
                    print(line, flush=True)
            assert re.search(r"flash_attn\s*=\s*(enabled|true|1)", startup_log), "Flash Attention not confirmed"
            assert "q8_0" in startup_log and "offloaded 37/37" in startup_log, "Q8/full GPU offload not confirmed"
            if experimental_fa:
                assert "experimental AMD MoltenVK shared FA only = 1; other shuffle disabled" in startup_log, "FA-only shared-memory override not active"
                groups = re.findall(r"graph[^\n]*splits = (\d+)(?: / (\d+))?", startup_log)
                split_values = [value for group in groups for value in group if value]
                assert split_values and max(map(int, split_values)) <= 8, ("Graph still has substantial CPU fallback; skipping long test", split_values)
                print("GPU graph splits: " + "/".join(split_values), flush=True)
            result_path = ROOT / ("logs/%s-%s.json" % (prefix, "short" if short_only else "benchmark"))
            if experimental_fa or short_only:
                short_result = benchmark(PORT, 1024, 16384, ROOT / ("logs/%s-short.json" % prefix),
                                         request_timeout)
            if short_only:
                result = short_result
            else:
                result = benchmark(PORT, 10000, 16384, result_path, request_timeout)
            subprocess.run([str(ROOT / "check-api.command"), "--port", str(PORT)], check=True)
            log = log_path.read_text(errors="replace")
            assert re.search(r"flash_attn\s*=\s*(enabled|true|1)", log), "Flash Attention not confirmed in log"
            assert "q8_0" in log and "offloaded 37/37" in log, "Q8/full GPU offload not confirmed"
            result["api_check_passed"] = True
            result["flash_attention_enabled_and_weights_offloaded"] = True
            result["experimental_fa"] = experimental_fa
            if experimental_fa:
                result["graph_splits"] = int(split_values[-1])
            result_path.write_text(json.dumps(result, indent=2) + "\n")
            print("16k/Q8 %s retrieval, prefix cache and API tests passed." %
                  ("short-context" if short_only else "long-context"), flush=True)
    except BaseException as exc:
        failed = exc
        print("Experiment stopped: " + str(exc), file=sys.stderr, flush=True)
        if log_path.exists():
            print("\n".join(log_path.read_text(errors="replace").splitlines()[-25:]), file=sys.stderr)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if stopped:
            print("Restoring stable 8k daemon...", flush=True)
            subprocess.run([str(ROOT / "daemon.command"), "start"], check=True)
    if failed is not None:
        raise SystemExit(1)


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt("Signal %d" % signum)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--experimental-fa", "--experimental-shuffle", dest="experimental_fa", action="store_true")
    parser.add_argument("--ops-only", action="store_true")
    parser.add_argument("--short-only", action="store_true",
                        help="Check short retrieval and API without the 10k-token benchmark")
    parser.add_argument("--request-timeout", type=float, default=2400,
                        help="Socket timeout per benchmark API request in seconds (default: 2400)")
    options = parser.parse_args()
    if options.request_timeout <= 0:
        parser.error("--request-timeout must be positive")
    if options.ops_only and options.short_only:
        parser.error("--ops-only and --short-only cannot be combined")
    main(options.dry_run, options.experimental_fa, options.ops_only,
         options.short_only, options.request_timeout)
