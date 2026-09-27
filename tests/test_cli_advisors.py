"""Advisors that run another vendor's coding-agent CLI: Codex, Gemini CLI,
Antigravity, Kilo, OpenCode, Qwen Code and GitHub Copilot.

Each runs against `fake_cli.py`, which speaks that CLI's output format, so the
argv, the stdin and the parsing are exercised without any account.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from conftest import advisors_json

HERE = Path(__file__).parent
KINDS = ("codex", "gemini", "agy", "kilo", "opencode", "qwen", "copilot")
PROVIDER = {"agy": "antigravity", "gemini": "gemini-cli"}


class FakeCli:
    def __init__(self, path: Path, log: Path):
        self.path = path
        self.log = log

    def calls(self) -> list:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in
                self.log.read_text(encoding="utf-8").splitlines() if line]

    @property
    def last(self) -> dict:
        calls = self.calls()
        assert calls, "the fake CLI was never run"
        return calls[-1]


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """fake_cli("codex") -> a FakeCli whose launcher speaks codex's format."""
    def install(kind: str, directory: str = "bin") -> FakeCli:
        folder = tmp_path / directory
        folder.mkdir(exist_ok=True)
        script = HERE / "fake_cli.py"
        if os.name == "nt":
            launcher = folder / f"{kind}.cmd"
            launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
                                encoding="utf-8")
        else:
            launcher = folder / kind
            launcher.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                encoding="utf-8")
            launcher.chmod(0o755)
        log = tmp_path / f"{kind}-calls.jsonl"
        monkeypatch.setenv("FAKE_CLI_KIND", kind)
        monkeypatch.setenv("FAKE_CLI_LOG", str(log))
        return FakeCli(launcher, log)
    return install


def _srv(server, cli, kind, **entry):
    spec = {"provider": PROVIDER.get(kind, kind), "command": str(cli.path)}
    spec.update(entry)
    return server(ADVISOR_ADVISORS_JSON=advisors_json(**{kind: spec}))


# --------------------------------------------------------------------------
# Every preset
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_each_cli_answers_through_its_own_format(server, fake_cli, kind):
    cli = fake_cli(kind)
    srv = _srv(server, cli, kind)
    answer = srv._consult(question="QUESTION-MARK", context="CONTEXT-MARK",
                          advisor=kind)
    assert f"FAKE {kind} ANSWER" in answer
    assert "<think>" not in answer


@pytest.mark.parametrize("kind", KINDS)
def test_the_prompt_and_instructions_travel_on_stdin(server, fake_cli, kind):
    """Windows caps a command line, and an npm .cmd shim cuts an argument at
    its first newline, so nothing long or multi-line goes in argv."""
    cli = fake_cli(kind)
    srv = _srv(server, cli, kind)
    srv._consult(question="QUESTION-MARK", context="CONTEXT-MARK", advisor=kind)
    call = cli.last
    assert "QUESTION-MARK" in call["stdin"] and "CONTEXT-MARK" in call["stdin"]
    assert "expert technical advisor" in call["stdin"]  # the system prompt
    assert not any("QUESTION-MARK" in arg for arg in call["argv"])


@pytest.mark.parametrize("kind, flags", [
    ("codex", ["exec", "--sandbox", "read-only", "--ephemeral", "-"]),
    ("gemini", ["--approval-mode", "plan"]),
    ("agy", ["--sandbox"]),
    ("kilo", ["run", "--agent", "ask"]),
    ("opencode", ["run", "--agent", "plan"]),
    ("qwen", ["--approval-mode", "plan"]),
    ("copilot", ["--deny-tool", "shell", "write"]),
])
def test_each_cli_runs_read_only(server, fake_cli, kind, flags):
    """These are agents that can edit files and run commands; an advisor only
    answers."""
    cli = fake_cli(kind)
    srv = _srv(server, cli, kind)
    srv._consult(question="q", advisor=kind)
    argv = cli.last["argv"]
    for flag in flags:
        assert flag in argv, f"{kind}: missing {flag} in {argv}"
    for dangerous in ("--yolo", "--dangerously-skip-permissions", "--auto",
                      "--allow-all-tools", "--full-auto"):
        assert dangerous not in argv


@pytest.mark.parametrize("kind", KINDS)
def test_each_cli_runs_in_an_empty_folder(server, fake_cli, kind):
    cli = fake_cli(kind)
    srv = _srv(server, cli, kind)
    srv._consult(question="q", advisor=kind)
    assert Path(cli.last["cwd"]).resolve() == Path(srv._workdir()).resolve()


@pytest.mark.parametrize("kind, flag", [
    ("codex", "-m"), ("gemini", "-m"), ("agy", "--model"), ("kilo", "-m"),
    ("opencode", "-m"), ("qwen", "-m"), ("copilot", "--model")])
def test_a_model_is_passed_with_the_clis_own_flag(server, fake_cli, kind, flag):
    cli = fake_cli(kind)
    srv = _srv(server, cli, kind)
    answer = srv._consult(question="q", advisor=kind, model="some-model-1")
    argv = cli.last["argv"]
    assert argv[argv.index(flag) + 1] == "some-model-1"
    assert "model=some-model-1" in answer


def test_no_model_means_the_clis_own_default(server, fake_cli):
    cli = fake_cli("codex")
    srv = _srv(server, cli, "codex")
    srv._consult(question="q", advisor="codex")
    assert "-m" not in cli.last["argv"]


@pytest.mark.parametrize("kind, expect", [
    ("codex", ["-c", "model_reasoning_effort=high"]),
    ("agy", ["--effort", "high"]),
    ("kilo", ["--variant", "high"]),
])
def test_effort_uses_the_clis_own_setting(server, fake_cli, kind, expect):
    cli = fake_cli(kind)
    srv = _srv(server, cli, kind)
    srv._consult(question="q", advisor=kind, effort="high")
    argv = cli.last["argv"]
    i = argv.index(expect[0])
    assert argv[i:i + 2] == expect


def test_effort_is_clamped_to_what_the_cli_accepts(server, fake_cli):
    cli = fake_cli("agy")  # low / medium / high only
    srv = _srv(server, cli, "agy")
    srv._consult(question="q", advisor="agy", effort="max")
    argv = cli.last["argv"]
    assert argv[argv.index("--effort") + 1] == "high"


@pytest.mark.parametrize("kind", ["codex", "agy", "kilo", "gemini", "qwen"])
def test_token_usage_reaches_the_ledger(server, fake_cli, usage_file, kind):
    cli = fake_cli(kind)
    srv = _srv(server, cli, kind)
    srv._consult(question="q", advisor=kind)
    record = json.loads(usage_file.read_text(encoding="utf-8").splitlines()[-1])
    assert record["advisor"] == kind
    assert record["input_tokens"] > 0 and record["output_tokens"] > 0


def test_the_footer_says_whose_install_paid(server, fake_cli):
    cli = fake_cli("codex")
    srv = _srv(server, cli, "codex")
    answer = srv._consult(question="q", advisor="codex")
    assert "codex" in answer.lower()
    assert "your own" in answer.lower()


# --------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind, text", [
    ("codex", "stream exploded"), ("agy", "quota exceeded"),
    ("kilo", "provider down"), ("gemini", "quota exceeded"),
    ("qwen", "API Error"), ("copilot", "model not available")])
def test_a_failed_run_reports_the_clis_own_message(server, fake_cli,
                                                   monkeypatch, kind, text):
    cli = fake_cli(kind)
    monkeypatch.setenv("FAKE_CLI_MODE", "error")
    srv = _srv(server, cli, kind)
    with pytest.raises(srv.AdvisorError) as exc:
        srv._consult(question="q", advisor=kind)
    assert text in str(exc.value)


def test_a_sign_in_problem_says_to_sign_in_to_that_cli(server, fake_cli,
                                                       monkeypatch):
    cli = fake_cli("codex")
    monkeypatch.setenv("FAKE_CLI_MODE", "auth")
    srv = _srv(server, cli, "codex")
    with pytest.raises(srv.AdvisorError) as exc:
        srv._consult(question="q", advisor="codex")
    text = str(exc.value)
    assert "sign in" in text.lower() and "codex" in text


def test_a_silent_cli_is_stopped_by_the_idle_limit(server, fake_cli,
                                                   monkeypatch):
    cli = fake_cli("codex")
    monkeypatch.setenv("FAKE_CLI_MODE", "hang")
    srv = server(ADVISOR_IDLE_TIMEOUT="2", ADVISOR_ADVISORS_JSON=advisors_json(
        codex={"provider": "codex", "command": str(cli.path)}))
    with pytest.raises(srv.AdvisorError) as exc:
        srv._consult(question="q", advisor="codex")
    assert "silent" in str(exc.value)


def test_a_cli_that_is_not_installed_is_not_ready(server, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(codex={"provider": "codex"}))
    ready, why = srv._advisor_ready("codex")
    assert not ready
    assert "codex" in why and "install" in why.lower()


@pytest.mark.parametrize("model", ["gpt&calc", "a|b", "x>y", "%PATH%", "a b"])
def test_a_model_name_with_shell_syntax_is_refused(server, fake_cli, model):
    cli = fake_cli("codex")
    srv = _srv(server, cli, "codex")
    with pytest.raises(ValueError):
        srv._consult(question="q", advisor="codex", model=model)
    assert cli.calls() == []


# --------------------------------------------------------------------------
# Recursion
# --------------------------------------------------------------------------

def test_the_cli_is_marked_as_running_under_wisdomtooth(server, fake_cli):
    cli = fake_cli("kilo")
    srv = _srv(server, cli, "kilo")
    srv._consult(question="q", advisor="kilo")
    assert cli.last["nested"] == "1"


def test_a_server_started_by_an_advisor_cli_refuses_to_consult(
        server, fake_claude, monkeypatch):
    """Kilo, Qwen and others load the user's MCP servers -- this one included.
    Without a stop, an advisor could consult Wisdomtooth, which asks the
    advisor again, without end."""
    monkeypatch.setenv("WISDOMTOOTH_NESTED", "1")
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(srv.AdvisorError) as exc:
        srv._consult(question="q")
    assert "loop" in str(exc.value)
    assert [c for c in fake_claude.calls() if c["prompt"]] == []


# --------------------------------------------------------------------------
# Connecting, status and several advisors
# --------------------------------------------------------------------------

def test_advisor_connect_adds_a_cli_advisor(server, fake_cli):
    cli = fake_cli("codex")
    srv = server()
    text = srv._connect_advisor("codex", "codex", base_url="")
    assert "codex" in text
    srv._ADVISORS["codex"]  # registered
    srv2 = server(ADVISOR_ADVISORS_JSON=advisors_json(
        codex={"provider": "codex", "command": str(cli.path)}))
    assert "FAKE codex ANSWER" in srv2._consult(question="q", advisor="codex")


def test_status_lists_a_cli_advisor(server, fake_cli):
    cli = fake_cli("agy")
    srv = _srv(server, cli, "agy")
    status = srv._status_report()
    assert "agy" in status and "Antigravity" in status


def test_multi_advisor_mixes_claude_and_a_cli(server, fake_claude, fake_cli):
    cli = fake_cli("codex")
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(
                     codex={"provider": "codex", "command": str(cli.path)}))
    text = srv._multi(question="q", advisors=["claude", "codex"])
    assert "FAKE ANSWER" in text and "FAKE codex ANSWER" in text


def test_a_generic_cli_reads_stdin_and_prints_its_answer(server, fake_cli):
    cli = fake_cli("mytool")
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(
        mine={"provider": "cli", "command": str(cli.path),
              "args": ["--quiet"]}))
    assert "FAKE mytool ANSWER" in srv._consult(question="q", advisor="mine")
    assert cli.last["argv"] == ["--quiet"]


def test_the_gemini_api_provider_is_not_the_gemini_cli(server):
    """`gemini` is Google's API; the CLI is `gemini-cli`."""
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(g={"provider": "gemini"}))
    assert srv._ADVISORS["g"].kind == "openai"
