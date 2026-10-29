#!/usr/bin/python3
import argparse
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8080)
opts = parser.parse_args()
BASE = "http://127.0.0.1:%d" % opts.port
TOKEN = (ROOT / "server/api-key").read_text().strip()
MODEL = "qwen3-4b-local"


def request(path, payload=None, auth=True):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + TOKEN
    body = None if payload is None else json.dumps(payload).encode()
    return urllib.request.urlopen(urllib.request.Request(BASE + path, data=body,
                                                       headers=headers), timeout=300)


deadline = time.monotonic() + 180
while True:
    try:
        with request("/health") as response:
            assert json.load(response)["status"] == "ok"
        break
    except (OSError, urllib.error.URLError):
        if time.monotonic() > deadline:
            raise RuntimeError("Server not ready: " + BASE)
        time.sleep(2)

results = {"endpoint": BASE, "health": True}
with request("/v1/models") as response:
    models = json.load(response)
assert any(m["id"] == MODEL for m in models["data"]), models
results["models"] = True
try:
    request("/v1/models", auth=False)
    raise AssertionError("Unauthenticated request was accepted")
except urllib.error.HTTPError as exc:
    assert exc.code in (401, 403), exc.code
results["auth"] = True

with request("/v1/chat/completions", {
    "model": MODEL, "messages": [{"role": "user", "content": "What is 2 + 2? Reply with only the digit."}],
    "temperature": 0, "max_tokens": 16,
}) as response:
    chat = json.load(response)
answer = chat["choices"][0]["message"]["content"].strip()
assert answer == "4", chat
results["chat"] = answer
print("Health, model alias, authentication and chat: OK", flush=True)

messages = [{"role": "user", "content": "Use add to calculate 2 plus 2."}]
tools = [{"type": "function", "function": {
    "name": "add", "description": "Add two integers.",
    "parameters": {"type": "object", "properties": {
        "a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"], "additionalProperties": False},
}}]
calls = {}
finish = None
done = False
with request("/v1/chat/completions", {
    "model": MODEL, "messages": messages, "tools": tools,
    "tool_choice": {"type": "function", "function": {"name": "add"}},
    "temperature": 0, "max_tokens": 80, "stream": True,
}) as response:
    assert "text/event-stream" in response.headers.get("Content-Type", "")
    for raw in response:
        line = raw.decode().strip()
        if not line.startswith("data: "):
            continue
        if line == "data: [DONE]":
            done = True
            break
        chunk = json.loads(line[6:])
        for choice in chunk.get("choices", []):
            finish = choice.get("finish_reason") or finish
            for part in choice.get("delta", {}).get("tool_calls", []):
                call = calls.setdefault(part["index"], {"id": "", "type": "function",
                                                          "function": {"name": "", "arguments": ""}})
                if part.get("id"):
                    call["id"] = part["id"]
                for field in ("name", "arguments"):
                    call["function"][field] += part.get("function", {}).get(field, "")
assert done and finish == "tool_calls" and len(calls) == 1, (done, finish, calls)
call = calls[0]
assert call["id"] and call["function"]["name"] == "add", call
arguments = json.loads(call["function"]["arguments"])
assert arguments == {"a": 2, "b": 2}, arguments
results["streamed_tool_call"] = call
print("Streamed function call, arguments and finish reason: OK", flush=True)

messages += [{"role": "assistant", "content": None, "tool_calls": [call]},
             {"role": "tool", "tool_call_id": call["id"], "content": "4"},
             {"role": "user", "content": "Reply with only the numeric result."}]
with request("/v1/chat/completions", {
    "model": MODEL, "messages": messages, "tools": tools,
    "tool_choice": "none", "temperature": 0, "max_tokens": 16,
}) as response:
    result = json.load(response)
answer = result["choices"][0]["message"]["content"].strip()
assert answer == "4", result
results["tool_result_roundtrip"] = answer
destination = ROOT / "logs" / ("api-check-%d.json" % opts.port)
destination.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
print("Tool result round trip: OK\nSaved: " + str(destination), flush=True)
