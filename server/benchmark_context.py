#!/usr/bin/python3
import argparse
import json
from pathlib import Path
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {"alpha": "A7C29F", "beta": "B4D81E", "gamma": "C9E63A"}


def benchmark(port, target, expected_context, output, request_timeout=2400):
    token = (ROOT / "server/api-key").read_text().strip()
    base = "http://127.0.0.1:%d" % port

    def api(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=request_timeout) as response:
            return json.load(response)

    props = api("/props")
    context = props["default_generation_settings"]["n_ctx"]
    assert context == expected_context, ("Unexpected actual context", context)
    assert props["model_alias"] == "qwen3-4b-local", props["model_alias"]
    primer = api("/v1/chat/completions", {
        "model": "qwen3-4b-local", "messages": [{"role": "user", "content": "What is 2+2? Reply only with 4."}],
        "max_tokens": 8, "temperature": 0})
    assert primer["choices"][0]["message"]["content"].strip() == "4", primer

    def document(count):
        lines = ["Entry %05d: reference record, no action required." % i for i in range(count)]
        for position, key in [(count // 10, "alpha"), (count // 2, "beta"), (count * 9 // 10, "gamma")]:
            lines[position] = "CHECKPOINT_%s=%s" % (key.upper(), EXPECTED[key])
        return "Reference document:\n" + "\n".join(lines)

    count = max(20, target // 12)
    for _ in range(4):
        text = document(count)
        length = len(api("/tokenize", {"content": text, "add_special": False})["tokens"])
        if abs(length - target) < target * .08:
            break
        count = max(20, int(count * target / length))
    assert length + 400 < context, ("Document does not fit", length, context)
    if target > 8192:
        assert length > 8192, ("Long-context test is too short", length)
    schema = {"type": "object", "properties": {k: {"type": "string"} for k in EXPECTED},
              "required": list(EXPECTED), "additionalProperties": False}
    system = ("Read the reference document and return its CHECKPOINT_ALPHA, CHECKPOINT_BETA "
              "and CHECKPOINT_GAMMA values as JSON keys alpha, beta, gamma. "
              "Use the exact strings from the document.\n" + text)
    result = {"port": port, "actual_context": context, "document_tokens": length,
              "primer_correct": True, "request_timeout_seconds": request_timeout, "requests": []}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        output.write_text(json.dumps(result, indent=2) + "\n")

    save()
    for label, question in [("cold", "Return the three checkpoint values."),
                             ("repeat", "Return the three checkpoint values."),
                             ("new_suffix", "Return the same three checkpoint values again.")]:
        print("%s: document %d tokens; processing..." % (label, length), flush=True)
        started = time.monotonic()
        response = api("/v1/chat/completions", {
            "model": "qwen3-4b-local", "temperature": 0, "seed": 42,
            "cache_prompt": True, "max_tokens": 128,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": question}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "checkpoint_values", "strict": True, "schema": schema}},
        })
        content = response["choices"][0]["message"]["content"]
        answer = json.loads(content)
        timings = response["timings"]
        entry = {"label": label, "wall_seconds": round(time.monotonic() - started, 3),
                 "answer": answer, "correct": answer == EXPECTED, "timings": timings}
        result["requests"].append(entry)
        save()
        print("%s: correct=%s, wall=%.2fs, cached=%d, evaluated=%d, prefill=%.2fs" % (
            label, entry["correct"], entry["wall_seconds"], timings["cache_n"],
            timings["prompt_n"], timings["prompt_ms"] / 1000), flush=True)
        assert entry["correct"], ("Incorrect checkpoint retrieval", label, answer)
        if label != "cold":
            total = timings["cache_n"] + timings["prompt_n"]
            assert timings["cache_n"] / total > .9, ("Prefix cache not effectively reused", entry)
    cold, repeat = result["requests"][:2]
    result["prefill_speedup"] = round(cold["timings"]["prompt_ms"] /
                                       max(repeat["timings"]["prompt_ms"], .001), 2)
    result["passed"] = True
    save()
    print("PASS. Repeated prefill speedup: %sx. Report: %s" % (result["prefill_speedup"], output), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18082)
    parser.add_argument("--target-tokens", type=int, default=10000)
    parser.add_argument("--expected-context", type=int, default=16384)
    parser.add_argument("--output", default=str(ROOT / "logs/context16k-benchmark.json"))
    parser.add_argument("--request-timeout", type=float, default=2400)
    options = parser.parse_args()
    if options.request_timeout <= 0:
        parser.error("--request-timeout must be positive")
    benchmark(options.port, options.target_tokens, options.expected_context, options.output,
              options.request_timeout)
