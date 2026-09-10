"""Shared fixtures.

Two things make this server awkward to test, and both are solved here:

1. Configuration is read at import time into module-level constants, so a test
   that wants a different `ADVISOR_*` value must reload the module. `server`
   does exactly that, and restores the original environment afterwards.

2. The subscription backend shells out to the real `claude` CLI. `fake_claude`
   installs a stand-in executable that speaks the same contract (flags,
   stdin prompt, `--output-format json`) and records every invocation, so the
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
import claude_advisor.server as _srv  # noqa: E402

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
# the developer's real ~/.claude-advisor.
_PRESERVED = {"ADVISOR_CLAUDE_BIN", "ADVISOR_CONSULT_DIR"}


def _reload(env: dict) -> object:
    for key in ADVISOR_ENV:
        if key not in _PRESERVED:
            os.environ.pop(key, None)
    os.environ.update({k: str(v) for k, v in env.items()})
    import claude_advisor.server as mod
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


@pytest.fixture
def server():
    """Reload `claude_advisor.server` under a chosen environment.

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
    importlib.reload(importlib.import_module("claude_advisor.server"))


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
  --max-budget-usd <amount>
  --model <model>
  --no-session-persistence
  --output-format <format>
  --permission-prompts <target>
  -p, --print
  --setting-sources <sources>
  --strict-mcp-config
  --system-prompt <prompt>
  --tools <tools...>
  (context via: --system-prompt[-file], --append-system-prompt[-file])
  -v, --version
__EXTRA_HELP__
Commands:
  auth
"""

argv = sys.argv[1:]


def log(prompt=""):
    """Record every invocation -- prompt runs, --help probes and auth checks."""
    record = {
        "argv": argv,
        "prompt": prompt,
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
    sys.stdout.write(os.environ.get("FAKE_AUTH_STATUS", json.dumps(
        {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro",
         "email": "test@example.com"})))
    sys.exit(int(os.environ.get("FAKE_AUTH_RC", "0")))

prompt = sys.stdin.read()
log(prompt)

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")

if mode == "hang":
    time.sleep(600)
elif mode == "auth_fail":
    sys.stderr.write("Invalid API key · Please run /login")
    sys.exit(1)
elif mode == "usage_limit":
    sys.stdout.write(json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "Claude AI usage limit reached. Your limit will reset at 5pm."}))
    sys.exit(0)
elif mode == "is_error":
    sys.stdout.write(json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "something exploded"}))
    sys.exit(0)
elif mode == "empty":
    sys.stdout.write(json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "result": ""}))
    sys.exit(0)
elif mode == "not_json":
    sys.stdout.write("plain text answer, no JSON here")
    sys.exit(0)
elif mode == "crash":
    sys.stderr.write("boom: unexpected internal error")
    sys.exit(3)
else:
    model = argv[argv.index("--model") + 1] if "--model" in argv else "?"
    sys.stdout.write(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "FAKE ANSWER for model=" + model,
        "total_cost_usd": 0.01,
        "modelUsage": {"claude-test-" + model: {"costUSD": 0.01}},
    }))
    sys.exit(0)
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
