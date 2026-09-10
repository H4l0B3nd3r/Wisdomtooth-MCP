"""Shared fixtures.

Two things make this server awkward to test, and both are solved here:

1. Configuration is read at import time into module-level constants, so a test
   that wants a different `ADVISOR_*` value must reload the module. `server`
   does exactly that, and restores the original environment afterwards.

2. The subscription backend shells out to the real `claude` CLI. `fake_claude`
   installs a stand-in executable that speaks the same contract (flags,
   stdin prompt, `--output-format json` and `stream-json`) and records every
   invocation, so the
   whole backend can be exercised with no credentials and no spend.
"""

import importlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Env vars the server reads at import time, cleared before every reload so a
# stray value -- from the developer's shell or from an earlier test -- can never
# change an outcome. Derived from the server's own config table rather than
# hand-listed: a new setting would otherwise silently leak between tests, which
# is exactly the bug this list exists to prevent.
import wisdomtooth.server as _srv  # noqa: E402

ADVISOR_ENV = tuple(sorted(set(_srv.CONFIG_KEYS.values()) | {
    "ADVISOR_KEEP_AUTH_ENV", "ADVISOR_NO_STRICT_MCP", "ADVISOR_NSFW_EXTRA_JSON",
    "ADVISOR_CONFIG",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
}))


# Harness plumbing, preserved across reloads. `fake_claude` sets
# ADVISOR_CLAUDE_BIN and `no_claude` clears it, both before the test body calls
# the factory; wiping it here would silently send every subprocess test at the
# real CLI. ADVISOR_CONSULT_DIR is pinned to a temp directory by the autouse
# `consult_dir` fixture below, and wiping it would scatter transcripts through
# the developer's real ~/.wisdomtooth.
_PRESERVED = {"ADVISOR_CLAUDE_BIN", "ADVISOR_CONSULT_DIR", "ADVISOR_USAGE_FILE"}


def _reload(env: dict) -> object:
    for key in ADVISOR_ENV:
        if key not in _PRESERVED:
            os.environ.pop(key, None)
    os.environ.update({k: str(v) for k, v in env.items()})
    import wisdomtooth.server as mod
    return importlib.reload(mod)


@pytest.fixture(autouse=True)
def consult_dir(tmp_path, monkeypatch):
    """Send consult transcripts to a temp directory, for every test.

    Autouse and unconditional: a consult writes a transcript by default, so
    without this any test that reaches `_consult` -- directly or over stdio --
    would litter the developer's real home directory. Tests that care about
    the transcripts read this path; the rest simply stay clean.
    """
    path = tmp_path / "consults"
    monkeypatch.setenv("ADVISOR_CONSULT_DIR", str(path))
    return path


@pytest.fixture(autouse=True)
def usage_file(tmp_path, monkeypatch):
    """Keep the usage ledger out of the developer's home, like transcripts."""
    path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("ADVISOR_USAGE_FILE", str(path))
    return path


@pytest.fixture
def server():
    """Reload `wisdomtooth.server` under a chosen environment.

    Usage: `srv = server(ADVISOR_BACKEND="api", ANTHROPIC_API_KEY="k")`.
    """
    saved = {k: os.environ.get(k) for k in ADVISOR_ENV}

    def _factory(**env):
        return _reload(env)

    yield _factory

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    importlib.reload(importlib.import_module("wisdomtooth.server"))


# --------------------------------------------------------------------------
# Fake `claude` CLI
# --------------------------------------------------------------------------

# Mirrors the flags the real CLI exposes (verified against Claude Code 2.1.x).
# `--help` output is what the server probes for feature detection, so it has to
# look like the real thing.
_FAKE_CLI = r'''
import json, os, sys, time

HELP = """Usage: claude [options] [command] [prompt]
Options:
  --allowedTools, --allowed-tools <tools...>
  --append-system-prompt <prompt>
  --disable-slash-commands
  --effort <level>
  --include-partial-messages
  --max-budget-usd <amount>
  --model <model>
  --no-session-persistence
  --output-format <format>              "text", "json", or "stream-json"
  --permission-prompts <target>
  -p, --print
  --setting-sources <sources>
  --strict-mcp-config
  --system-prompt <prompt>
  --tools <tools...>
  (context via: --system-prompt[-file], --append-system-prompt[-file])
  --verbose
  -v, --version
__EXTRA_HELP__
Commands:
  auth
"""

argv = sys.argv[1:]


def system_prompt():
    """The prompt file's content, read while it still exists: the server
    deletes each consult's prompt file once the CLI exits."""
    if "--system-prompt-file" not in argv:
        return None
    try:
        with open(argv[argv.index("--system-prompt-file") + 1],
                  encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def log(prompt=""):
    """Record every invocation -- prompt runs, --help probes and auth checks."""
    record = {
        "argv": argv,
        "prompt": prompt,
        "system_prompt": system_prompt(),
        "cwd": os.getcwd(),
        # Only the variables the billing tests care about; a full env dump
        # would make the recording file enormous.
        "env": {k: os.environ.get(k) for k in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")},
    }
    with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as fh:
        print(json.dumps(record), file=fh)


log()

if "--version" in argv:
    print("9.9.9 (Claude Code)")
    sys.exit(0)

if "--help" in argv or "-h" in argv:
    sys.stdout.write(HELP.replace("__EXTRA_HELP__", ""))
    sys.exit(0)

if argv[:1] == ["auth"]:
    time.sleep(float(os.environ.get("FAKE_AUTH_SLEEP", "0")))
    sys.stdout.write(os.environ.get("FAKE_AUTH_STATUS", json.dumps(
        {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro",
         "email": "test@example.com"})))
    sys.exit(int(os.environ.get("FAKE_AUTH_RC", "0")))

prompt = sys.stdin.read()
log(prompt)

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
# `--output-format stream-json` gets one JSON event per line, shaped like the
# real CLI's (checked against Claude Code 2.1.267): a system init, the raw API
# stream events, the whole assistant message, then the same `result` object
# that `--output-format json` prints on its own.
streaming = ("--output-format" in argv
             and argv[argv.index("--output-format") + 1] == "stream-json")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def event(ev):
    emit({"type": "stream_event", "event": ev})


def finish(payload):
    if streaming:
        emit(payload)
    else:
        sys.stdout.write(json.dumps(payload))
    sys.exit(0)


def write_answer(text):
    event({"type": "content_block_start", "index": 1,
           "content_block": {"type": "text", "text": ""}})
    for word in text.split(" "):
        event({"type": "content_block_delta", "index": 1,
               "delta": {"type": "text_delta", "text": word + " "}})


if streaming and mode not in ("not_json", "hang"):
    emit({"type": "system", "subtype": "init", "tools": [], "mcp_servers": []})
    event({"type": "message_start", "message": {"role": "assistant"}})
    event({"type": "content_block_start", "index": 0,
           "content_block": {"type": "thinking", "thinking": ""}})
    event({"type": "content_block_delta", "index": 0,
           "delta": {"type": "thinking_delta", "thinking": ""}})

if mode == "slow":  # a long consult that does finish
    time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "3")))
    mode = "ok"

if mode == "trickle":  # a long answer that keeps streaming the whole time
    stop = time.monotonic() + float(os.environ.get("FAKE_CLAUDE_SLEEP", "3"))
    event({"type": "content_block_start", "index": 1,
           "content_block": {"type": "text", "text": ""}})
    while time.monotonic() < stop:
        event({"type": "content_block_delta", "index": 1,
               "delta": {"type": "text_delta", "text": "more words here "}})
        time.sleep(float(os.environ.get("FAKE_CLAUDE_TICK", "0.3")))
    mode = "ok"

if mode == "hang":
    time.sleep(600)
elif mode == "stall":  # started answering, then went silent
    write_answer("partial")
    time.sleep(600)
elif mode == "auth_fail":
    sys.stderr.write("Invalid API key · Please run /login")
    sys.exit(1)
elif mode == "usage_limit":
    finish({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "Claude AI usage limit reached. Your limit will reset at 5pm."})
elif mode == "is_error":
    finish({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "something exploded"})
elif mode == "error_list":  # error results carry `errors`, not `result`
    finish({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "errors": ["API Error: 529 Overloaded. Try again shortly."]})
elif mode == "over_budget":
    finish({
        "type": "result", "subtype": "error_max_budget_usd", "is_error": True,
        "total_cost_usd": 0.26, "errors": []})
elif mode == "empty":
    finish({
        "type": "result", "subtype": "success", "is_error": False, "result": ""})
elif mode == "not_json":
    sys.stdout.write("plain text answer, no JSON here")
    sys.exit(0)
elif mode == "crash":
    sys.stderr.write("boom: unexpected internal error")
    sys.exit(3)
else:
    model = argv[argv.index("--model") + 1] if "--model" in argv else "?"
    answer = "FAKE ANSWER for model=" + model
    if streaming:
        write_answer(answer)
        event({"type": "content_block_stop", "index": 1})
        event({"type": "message_stop"})
        emit({"type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": answer}]}})
    finish({
        "type": "result", "subtype": "success", "is_error": False,
        "result": answer,
        "total_cost_usd": 0.01,
        "duration_ms": 1500,
        "usage": {"input_tokens": 1200, "output_tokens": 340,
                  "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0},
        "modelUsage": {"claude-test-" + model: {
            "inputTokens": 1200, "outputTokens": 340,
            "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
            "costUSD": 0.01}},
    })
'''


class FakeClaude:
    def __init__(self, path: Path, log: Path):
        self.path = path
        self.log = log

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in
                self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    @property
    def last(self) -> dict:
        calls = self.calls()
        assert calls, "fake claude CLI was never invoked"
        return calls[-1]

    def flag_value(self, flag: str, call: dict | None = None) -> str | None:
        argv = (call or self.last)["argv"]
        return argv[argv.index(flag) + 1] if flag in argv else None


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Install a stand-in `claude` executable and point the server at it."""
    script = tmp_path / "fake_claude.py"
    script.write_text(textwrap.dedent(_FAKE_CLI), encoding="utf-8")

    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))

    # A launcher so the server invokes a real executable path, exactly as it
    # would the shipped CLI.
    if os.name == "nt":
        launcher = tmp_path / "claude.bat"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
                            encoding="utf-8")
    else:
        launcher = tmp_path / "claude"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                            encoding="utf-8")
        launcher.chmod(0o755)

    monkeypatch.setenv("ADVISOR_CLAUDE_BIN", str(launcher))
    return FakeClaude(launcher, log)


@pytest.fixture
def no_claude(monkeypatch):
    """Make CLI discovery fail, so `auto` cannot pick the subscription path."""
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    monkeypatch.delenv("ADVISOR_CLAUDE_BIN", raising=False)
