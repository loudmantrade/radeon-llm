#!/usr/bin/python3
import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "server/config.json" if (ROOT / "server/config.json").exists() else ROOT / "server/config.example.json").read_text())
LABEL = "local.radeon-llm.qwen3"
DOMAIN = "gui/" + str(os.getuid())
SERVICE = DOMAIN + "/" + LABEL
PLIST = ROOT / "server" / (LABEL + ".plist")
INSTALLED = Path.home() / "Library/LaunchAgents" / PLIST.name
KEY = ROOT / "server/api-key"
BIN = ROOT / CONFIG.get("native_binary", "llama.cpp/build-vulkan/bin/llama-server")
RUNTIME = Path.home() / "Library/Application Support/radeon-llm"


def prepare():
    (ROOT / "logs").mkdir(exist_ok=True)
    if not (ROOT / "server/config.json").exists():
        (ROOT / "server/config.json").write_text(json.dumps(CONFIG, indent=2) + "\n")
    if not KEY.exists():
        fd = os.open(str(KEY), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    if not (ROOT / "opencode.json").exists() and (ROOT / "opencode.example.json").exists():
        body = json.loads((ROOT / "opencode.example.json").read_text())
        body["providers"]["radeon"]["settings"]["apiKey"] = "{file:" + str(KEY) + "}"
        (ROOT / "opencode.json").write_text(json.dumps(body, indent=2) + "\n")
    data = {
        "Label": LABEL,
        "ProgramArguments": [CONFIG.get("python", sys.executable), str(Path(__file__).resolve()), "run"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "StandardOutPath": str(ROOT / "logs/server.stdout.log"),
        "StandardErrorPath": str(ROOT / "logs/server.stderr.log"),
        "ProcessType": "Interactive",
    }
    with PLIST.open("wb") as f:
        plistlib.dump(data, f)


def environment():
    env = os.environ.copy()
    sdk = ROOT / "vulkan-sdk/macOS"
    driver = ROOT / "moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS"
    if CONFIG.get("backend", "vulkan") == "vulkan":
        env["VULKAN_SDK"] = str(sdk)
        env["VK_DRIVER_FILES"] = str(driver / "MoltenVK_icd.json")
        env["VK_ICD_FILENAMES"] = env["VK_DRIVER_FILES"]
        env["DYLD_LIBRARY_PATH"] = ":".join([str(driver), str(sdk / "lib"), str(BIN.parent)])
    else:
        for key in ("VK_DRIVER_FILES", "VK_ICD_FILENAMES"):
            env.pop(key, None)
        env["DYLD_LIBRARY_PATH"] = str(BIN.parent)
    for key in list(env):
        if key.startswith("GGML_VK_") or key.startswith("LLAMA_ARG_"):
            del env[key]
    env.pop("LLAMA_KV_SHIFT_ACTIVE_ONLY", None)
    if CONFIG.get("amd_shared_fa", False):
        env["GGML_VK_AMD_MOLTENVK_FA_SHARED"] = "1"
        env["MVK_CONFIG_FAST_MATH_ENABLED"] = "0"
    if CONFIG.get("kv_shift_active_only", False):
        env["LLAMA_KV_SHIFT_ACTIVE_ONLY"] = "1"
    return env


def command(cpu=False, port=None, context=None, cache_dir=None):
    if not BIN.exists():
        raise RuntimeError("Build llama-server first: ./build-server.command")
    env = environment()
    device = "none"
    if not cpu:
        print("Daemon: probing GPU devices...", flush=True)
        result = subprocess.run([str(BIN), "--list-devices"], env=env,
                                capture_output=True, text=True, timeout=90)
        listing = result.stdout + result.stderr
        if CONFIG.get("backend", "vulkan") == "metal":
            match = re.search(r"^\s*(MTL\d+):", listing, re.M)
        else:
            match = re.search(r"^\s*(Vulkan\d+):.*Radeon", listing, re.M) or re.search(r"^\s*(Vulkan\d+):", listing, re.M)
        if result.returncode or not match:
            raise RuntimeError("GPU unavailable. Start from the macOS Terminal.\n" +
                               result.stdout + result.stderr)
        device = match.group(1)
    args = [str(BIN), "-m", str(ROOT / CONFIG["model"]),
            "--alias", CONFIG["alias"], "--host", CONFIG["host"],
            "--port", str(port or CONFIG["port"]),
            "-c", str(context or CONFIG["context"]), "--parallel", "1",
            "--device", device, "-ngl", "0" if cpu else "99",
            "--fit", CONFIG.get("fit", "on"),
            "--fit-target", str(CONFIG["fit_margin_mib"]),
            "-b", "128", "-ub", "128", "-t", "8", "-fa", CONFIG.get("flash_attention", "off"),
            "-ctk", CONFIG.get("cache_type_k", "f16"), "-ctv", CONFIG.get("cache_type_v", "f16"),
            "--jinja", "--no-context-shift", "--no-webui",
            "--api-key-file", str(KEY), "--temp", "0.2",
            "-n", str(CONFIG["max_output"]), "--verbosity", "4", "--no-warmup"]
    reuse = CONFIG.get("cache_reuse_tokens", 0)
    if reuse:
        if not CONFIG.get("kv_shift_active_only", False):
            raise RuntimeError("Experimental chunk reuse requires the active-only shift runtime")
        args += ["--cache-reuse", str(reuse)]
    if CONFIG.get("persistent_kv", False):
        directory = Path(cache_dir) if cache_dir else ROOT / "cache/kv"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        args += ["--slots", "--slot-save-path", str(directory.resolve()) + "/", "--cache-ram", "0"]
    return args, env


def launchctl(*args, check=True):
    result = subprocess.run(["/bin/launchctl", *args], check=False,
                            capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError("launchctl %s failed: %s" % (args[0], (result.stderr or result.stdout).strip()))
    return result


def loaded():
    return launchctl("print", SERVICE, check=False).returncode == 0


def stop_service():
    state = launchctl("print", SERVICE, check=False)
    if state.returncode:
        return
    match = re.search(r"^\s*pid = (\d+)", state.stdout, re.M)
    previous_pid = int(match.group(1)) if match else None
    launchctl("bootout", SERVICE)
    # bootout can return before the old process has finished shutting down.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", CONFIG["port"]), timeout=.3):
                listening = True
        except OSError:
            listening = False
        alive = False
        if previous_pid:
            try:
                os.kill(previous_pid, 0)
                alive = True
            except ProcessLookupError:
                pass
            except PermissionError:
                # Some desktop sandboxes forbid kill(pid, 0). The index lock is
                # released after the old native child has exited.
                owner = RUNTIME / "cache/kv/owner.lock"
                if owner.exists():
                    with owner.open("r") as stream:
                        try:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                        except BlockingIOError:
                            alive = True
                else:
                    alive = time.monotonic() < deadline - 29
        if not loaded() and not listening and not alive:
            return
        time.sleep(.25)
    raise RuntimeError("Previous service did not stop within 30 seconds; inspect ./daemon.command logs")


def startup_details():
    state = launchctl("print", SERVICE, check=False)
    details = ["LaunchAgent state:\n" + (state.stdout or state.stderr).strip()]
    for name in ["server.stdout.log", "server.stderr.log"]:
        path = RUNTIME / "logs" / name
        if path.exists():
            details.append(str(path) + ":\n" + "\n".join(path.read_text(errors="replace").splitlines()[-15:]))
    return "\n".join(details)


def owns_installed():
    if not INSTALLED.exists():
        return True
    arguments = plistlib.loads(INSTALLED.read_bytes()).get("ProgramArguments", [])
    return len(arguments) >= 2 and arguments[1] in {
        str(ROOT / "server/daemon.py"), str(RUNTIME / "server/daemon.py")}


def deploy(destination=RUNTIME):
    destination = Path(destination)
    if destination.resolve() == ROOT:
        prepare()
        return PLIST
    pairs = [(Path(__file__), destination / "server/daemon.py"),
             (ROOT / "server/slot_cache.py", destination / "server/slot_cache.py"),
             (ROOT / "server/platform_support.py", destination / "server/platform_support.py"),
             (ROOT / "server/cache_index.py", destination / "server/cache_index.py"),
             (ROOT / "server/tiered_cache.py", destination / "server/tiered_cache.py"),
             (ROOT / "server/cache_proxy.py", destination / "server/cache_proxy.py"),
             (ROOT / "server/config.json", destination / "server/config.json"),
             (KEY, destination / "server/api-key"),
             (ROOT / CONFIG["model"], destination / CONFIG["model"]),
             (ROOT / "models/Qwen3-LICENSE", destination / "models/Qwen3-LICENSE"),
             (BIN, destination / BIN.relative_to(ROOT))]
    pairs += [(p, destination / p.relative_to(ROOT)) for p in BIN.parent.glob("*.dylib")]
    sdk_lib = ROOT / "vulkan-sdk/macOS/lib"
    if CONFIG.get("backend", "vulkan") == "vulkan":
        pairs += [(p, destination / p.relative_to(ROOT)) for p in sdk_lib.glob("libvulkan*.dylib")]
    driver = ROOT / "moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS"
    if CONFIG.get("backend", "vulkan") == "vulkan":
        pairs += [(driver / name, destination / (driver / name).relative_to(ROOT))
                  for name in ["libMoltenVK.dylib", "MoltenVK_icd.json"]]
    for source, target in pairs:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        if source.is_symlink():
            if target.exists():
                target.unlink()
            target.symlink_to(os.readlink(source))
        elif not target.exists() or source.stat().st_size != target.stat().st_size or \
                source.stat().st_mtime_ns != target.stat().st_mtime_ns:
            print("Deploying " + str(source.relative_to(ROOT)), flush=True)
            shutil.copy2(source, target)
    # Use sibling native libraries instead of build-time paths inside Documents.
    native = [destination / BIN.relative_to(ROOT)]
    native += [p for p in native[0].parent.glob("*.dylib") if not p.is_symlink()]
    for path in native:
        listing = subprocess.check_output(["/usr/bin/otool", "-l", str(path)], text=True)
        rpaths = re.findall(r"cmd LC_RPATH\n\s*cmdsize \d+\n\s*path (.*?) \(offset", listing)
        changed = False
        for rpath in rpaths:
            if str(ROOT) in rpath or "/Documents/" in rpath:
                subprocess.run(["/usr/bin/install_name_tool", "-delete_rpath", rpath, str(path)], check=True)
                changed = True
        if "@loader_path" not in rpaths:
            subprocess.run(["/usr/bin/install_name_tool", "-add_rpath", "@loader_path", str(path)], check=True)
            changed = True
        if changed:
            # install_name_tool invalidates the linker's ad-hoc signature on arm64.
            subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(path)], check=True)
    subprocess.run([CONFIG.get("python", sys.executable), str(destination / "server/daemon.py"), "prepare"], check=True)
    return destination / "server" / PLIST.name


def ready():
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/models" % CONFIG["port"],
        headers={"Authorization": "Bearer " + KEY.read_text().strip()})
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.load(response)
        runtime_config = RUNTIME / "server/config.json"
        if runtime_config.exists() and json.loads(runtime_config.read_text()).get("persistent_kv", False):
            marker = json.loads((RUNTIME / "cache/kv/ready.json").read_text())
            state = launchctl("print", SERVICE, check=False)
            match = re.search(r"^\s*pid = (\d+)", state.stdout, re.M)
            if state.returncode or not match or int(match.group(1)) != marker["supervisor_pid"]:
                return False
        return any(m["id"] == CONFIG["alias"] for m in data.get("data", []))
    except (OSError, urllib.error.URLError, ValueError, KeyError):
        return False


def start():
    if not INSTALLED.exists() or "/Documents/" in plistlib.loads(INSTALLED.read_bytes()).get("WorkingDirectory", ""):
        raise RuntimeError("Run ./daemon.command install to deploy the service outside Documents")
    if loaded() and ready():
        print("Ready: http://127.0.0.1:%d/v1" % CONFIG["port"])
        return
    if not loaded():
        result = launchctl("bootstrap", DOMAIN, str(INSTALLED if INSTALLED.exists() else PLIST), check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
    # Explicitly start newly registered jobs as well as existing jobs.
    kicked = launchctl("kickstart", "-p", SERVICE, check=False)
    if kicked.returncode:
        raise RuntimeError("Unable to start LaunchAgent: " + kicked.stderr.strip() + "\n" + startup_details())
    if kicked.stdout.strip():
        print("LaunchAgent PID: " + kicked.stdout.strip(), flush=True)
    print("Loading model; waiting for API readiness...", flush=True)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if ready():
            print("Ready: http://127.0.0.1:%d/v1" % CONFIG["port"])
            return
        state = launchctl("print", SERVICE, check=False).stdout
        if not state:
            raise RuntimeError("LaunchAgent disappeared during startup.\n" + startup_details())
        if "state = running" not in state and re.search(r"last exit code = [1-9]\d*", state):
            data = plistlib.loads(INSTALLED.read_bytes())
            error_log = Path(data["StandardErrorPath"])
            detail = "\n".join(error_log.read_text(errors="replace").splitlines()[-12:]) if error_log.exists() else ""
            launchctl("bootout", SERVICE, check=False)
            raise RuntimeError("Server exited during startup; stopped crash loop.\n" + detail)
        time.sleep(2)
    raise RuntimeError("API not ready after 180 seconds.\n" + startup_details())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "run", "start", "stop", "restart",
                                          "status", "logs", "install", "uninstall", "update-scripts"])
    parser.add_argument("--cpu", action="store_true", help="foreground diagnostic run only")
    parser.add_argument("--port", type=int)
    parser.add_argument("--context", type=int)
    parser.add_argument("--cache-dir", help="foreground diagnostic cache directory")
    opts = parser.parse_args()
    if opts.action in ["prepare", "run", "start", "restart", "install"]:
        prepare()
    if opts.action == "run":
        print("Daemon: starting from " + str(ROOT), flush=True)
        if CONFIG.get("cache_reuse_tokens", 0):
            print("Daemon: EXPERIMENTAL KV chunk reuse, min=%d, active-only shift=%s" %
                  (CONFIG["cache_reuse_tokens"], CONFIG.get("kv_shift_active_only", False)), flush=True)
        args, env = command(opts.cpu, opts.port, opts.context, opts.cache_dir)
        os.chdir(ROOT)
        if CONFIG.get("persistent_kv", False):
            from slot_cache import SlotCache, identity, supervise
            os.umask(0o077)
            directory = Path(opts.cache_dir) if opts.cache_dir else ROOT / "cache/kv"
            if CONFIG.get("indexed_kv", False):
                from tiered_cache import TieredCache
                from cache_proxy import supervise_proxy
                public_port = opts.port or CONFIG["port"]
                backend_port = public_port + 1 if opts.port else CONFIG.get("kv_backend_port", 8081)
                if backend_port == public_port:
                    raise RuntimeError("Public and native backend ports must differ")
                # Port forwarding does not change KV compatibility. Preserve the
                # pre-proxy identity so the last single-snapshot cache can migrate.
                fingerprint = identity(args)
                args[args.index("--port") + 1] = str(backend_port)
                cache = TieredCache(directory, backend_port, KEY.read_text().strip(), fingerprint, CONFIG)
                raise SystemExit(supervise_proxy(args, env, cache, directory / "ready.json",
                                                public_port, backend_port, CONFIG.get("kv_idle_seconds", 5)))
            cache = SlotCache(directory, opts.port or CONFIG["port"], KEY.read_text().strip(), identity(args))
            raise SystemExit(supervise(args, env, cache, directory / "ready.json",
                                      CONFIG.get("kv_idle_seconds", 5)))
        os.execve(str(BIN), args, env)
    elif opts.action in ["stop", "uninstall", "restart"]:
        if not owns_installed():
            raise RuntimeError("Installed job belongs to another project")
        stop_service()
        if opts.action == "uninstall" and INSTALLED.exists():
            INSTALLED.unlink()
        if opts.action == "restart":
            start()
        else:
            print("Stopped" + (" and uninstalled" if opts.action == "uninstall" else ""))
    elif opts.action == "update-scripts":
        if not owns_installed() or not (RUNTIME / "server/daemon.py").exists():
            raise RuntimeError("Install the stable daemon first")
        if ROOT == RUNTIME:
            raise RuntimeError("Run update-scripts from the project directory")
        if not (RUNTIME / CONFIG.get("native_binary", "llama.cpp/build-vulkan/bin/llama-server")).exists():
            raise RuntimeError("Selected native runtime is not installed. Run ./enable-q8.command first")
        stop_service()
        # Deliberately preserve installed model, native libraries and driver.
        for name in ["daemon.py", "slot_cache.py", "cache_index.py", "tiered_cache.py", "cache_proxy.py", "platform_support.py", "config.json"]:
            shutil.copy2(ROOT / "server" / name, RUNTIME / "server" / name)
        subprocess.run([CONFIG.get("python", sys.executable), str(RUNTIME / "server/daemon.py"), "prepare"], check=True)
        shutil.copyfile(RUNTIME / "server" / PLIST.name, INSTALLED)
        print("Updated service scripts; installed native runtime preserved", flush=True)
        start()
    elif opts.action == "install":
        if not owns_installed():
            raise RuntimeError("An existing LaunchAgent belongs to another project")
        stop_service()
        deployed_plist = deploy()
        INSTALLED.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(deployed_plist, INSTALLED)
        print("Installed login startup: " + str(INSTALLED))
        start()
    elif opts.action == "start":
        start()
    elif opts.action == "status":
        result = launchctl("print", SERVICE, check=False)
        print("LaunchAgent: " + ("loaded" if result.returncode == 0 else "stopped"))
        for line in result.stdout.splitlines():
            if re.match(r"\s*(state|pid|last exit code) =", line):
                print(line.strip())
        print("API: " + ("ready" if KEY.exists() and ready() else "not ready"))
        runtime_config = RUNTIME / "server/config.json"
        actual = json.loads(runtime_config.read_text()) if runtime_config.exists() else CONFIG
        print("KV reuse: " + ("EXPERIMENTAL, min=%d tokens" % actual["cache_reuse_tokens"]
              if actual.get("cache_reuse_tokens", 0) else "exact prefix"))
        cache_index = RUNTIME / "cache/kv/latest.json"
        if cache_index.exists():
            try:
                snapshot = json.loads(cache_index.read_text())
                print("Disk KV snapshot: %d tokens, %.1f MiB" %
                      (snapshot["tokens"], snapshot["bytes"] / 2**20))
            except (OSError, ValueError, KeyError, TypeError):
                print("Disk KV snapshot: unreadable")
    elif opts.action == "logs":
        installed_data = plistlib.loads(INSTALLED.read_bytes()) if INSTALLED.exists() and owns_installed() else {}
        for name in ["server.stdout.log", "server.stderr.log"]:
            field = "StandardOutPath" if name == "server.stdout.log" else "StandardErrorPath"
            path = Path(installed_data.get(field, str(ROOT / "logs" / name)))
            print("\n" + str(path))
            if path.exists():
                print("\n".join(path.read_text(errors="replace").splitlines()[-60:]))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
