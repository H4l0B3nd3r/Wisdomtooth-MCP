"""A stand-in for the vendor CLIs an advisor can run: codex, gemini, agy,
kilo, opencode, qwen and copilot.

FAKE_CLI_KIND picks which CLI's output format to speak, FAKE_CLI_MODE how the
run goes (ok, auth, error, hang), and every invocation -- argv, stdin, the
nesting marker -- is appended to FAKE_CLI_LOG. The formats are the ones each
CLI printed when probed in September 2026.
"""

import json
import os
import sys
import time

argv = sys.argv[1:]
kind = os.environ["FAKE_CLI_KIND"]
mode = os.environ.get("FAKE_CLI_MODE", "ok")
prompt = sys.stdin.read()
with open(os.environ["FAKE_CLI_LOG"], "a", encoding="utf-8") as fh:
    print(json.dumps({"argv": argv, "stdin": prompt, "cwd": os.getcwd(),
                      "nested": os.environ.get("WISDOMTOOTH_NESTED")}), file=fh)


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def flag(name):
    return argv[argv.index(name) + 1] if name in argv else "default"


if mode == "hang":
    time.sleep(600)
if mode == "auth":
    sys.stderr.write("Error: not logged in. Please run login / authenticate.\n")
    sys.exit(1)

model = flag({"codex": "-m", "gemini": "-m", "qwen": "-m", "kilo": "-m",
              "opencode": "-m"}.get(kind, "--model"))
answer = f"FAKE {kind} ANSWER model={model}"

if kind == "codex":
    emit({"type": "thread.started", "thread_id": "t1"})
    emit({"type": "turn.started"})
    if mode == "error":
        emit({"type": "turn.failed", "error": {"message": "stream exploded"}})
        sys.exit(1)
    emit({"type": "item.completed", "item": {"id": "i0", "type": "reasoning",
                                             "text": "thinking"}})
    emit({"type": "item.completed", "item": {"id": "i1", "type": "agent_message",
                                             "text": answer}})
    emit({"type": "turn.completed", "usage": {
        "input_tokens": 900, "cached_input_tokens": 100, "output_tokens": 50}})

elif kind == "agy":
    message = json.loads(prompt.strip().splitlines()[0])
    assert message["event"] == "user", message
    emit({"event": "init", "init": {"tools": []}})
    if mode == "error":
        emit({"event": "result", "result": {"status": "ERROR", "response": "",
                                            "error": "quota exceeded"}})
        sys.exit(1)
    emit({"event": "step_update", "step_update": {
        "step_type": "agent_response", "state": "ACTIVE", "text_delta": answer}})
    emit({"event": "result", "result": {
        "status": "SUCCESS", "response": answer + "\n",
        "usage": {"input_tokens": 800, "output_tokens": 40,
                  "thinking_tokens": 20, "cache_read_tokens": 0}}})

elif kind in ("kilo", "opencode"):
    emit({"type": "step_start", "part": {"type": "step-start"}})
    if mode == "error":
        emit({"type": "error", "error": {"name": "APIError",
                                         "data": {"message": "provider down"}}})
        sys.exit(1)
    emit({"type": "text", "part": {"type": "text",
                                   "text": "<think>hmm</think>" + answer}})
    emit({"type": "step_finish", "part": {
        "type": "step-finish", "cost": 0.002,
        "tokens": {"input": 700, "output": 30, "reasoning": 5,
                   "cache": {"read": 10, "write": 0}}}})

elif kind == "gemini":
    emit({"type": "init", "session_id": "s", "model": model})
    if mode == "error":
        emit({"type": "result", "status": "error",
              "error": {"type": "Error", "message": "quota exceeded"}})
        sys.exit(1)
    emit({"type": "message", "role": "user", "content": "x"})
    emit({"type": "message", "role": "assistant", "content": answer[:6],
          "delta": True})
    emit({"type": "message", "role": "assistant", "content": answer[6:],
          "delta": True})
    emit({"type": "result", "status": "success",
          "stats": {"input_tokens": 600, "output_tokens": 25}})

elif kind == "qwen":
    emit({"type": "system", "subtype": "init", "tools": []})
    text = "[API Error: Connection error.]" if mode == "error" else answer
    emit({"type": "result", "subtype": "success", "is_error": False,
          "result": text, "usage": {"input_tokens": 500, "output_tokens": 20}})

else:  # copilot, and any plain-text CLI
    if mode == "error":
        sys.stderr.write("Error: model not available\n")
        sys.exit(1)
    sys.stdout.write(answer + "\n")
