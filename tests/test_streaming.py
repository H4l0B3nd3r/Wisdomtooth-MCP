"""Streamed CLI output: a hang is caught by silence, not by the wall clock.

With a 64,000-word budget every consult is sized to the one-hour cap, so a
stuck CLI used to surface only at the hour mark. Reading `stream-json` line by
line lets the server kill a CLI that has gone quiet, while an answer that is
long but still arriving runs to completion. The same stream tells the heartbeat
what Claude is doing.
"""

import time

import pytest
from mcp import types

from test_mcp_protocol import advisor_session


def consult(srv, **kw):
    kw.setdefault("question", "why is this broken?")
    return srv._consult(**kw)


def remove_help_lines(tmp_path, *lines):
    script = tmp_path / "fake_claude.py"
    src = script.read_text(encoding="utf-8")
    for line in lines:
        src = src.replace(line, "")
    script.write_text(src, encoding="utf-8")


def test_the_cli_is_read_as_a_stream(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    answer = consult(srv)
    argv = fake_claude.last["argv"]
    assert fake_claude.flag_value("--output-format") == "stream-json"
    # The CLI refuses stream-json in print mode without --verbose, and without
    # partial messages a long answer arrives as one silent block.
    assert "--verbose" in argv
    assert "--include-partial-messages" in argv
    assert "FAKE ANSWER" in answer


def test_a_cli_that_cannot_stream_falls_back_to_json(server, fake_claude,
                                                     tmp_path):
    remove_help_lines(tmp_path, "  --include-partial-messages\n")
    srv = server(ADVISOR_BACKEND="claude-code")
    answer = consult(srv)
    assert fake_claude.flag_value("--output-format") == "json"
    assert "--verbose" not in fake_claude.last["argv"]
    assert "FAKE ANSWER" in answer


def test_usage_is_read_from_the_stream_result(server, fake_claude, usage_file):
    import json
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv)
    [record] = [json.loads(line) for line in
                usage_file.read_text(encoding="utf-8").splitlines()]
    assert record["input_tokens"] == 1200
    assert record["output_tokens"] == 340
    assert record["cost_source"] == "cli"


def test_the_idle_timeout_defaults_to_five_minutes(server):
    assert server().IDLE_TIMEOUT == 300


def test_a_silent_cli_is_stopped_after_the_idle_timeout(server, fake_claude,
                                                        monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_IDLE_TIMEOUT="2",
                 ADVISOR_TIMEOUT="120", ADVISOR_TIMEOUT_SCALE="0")
    started = time.monotonic()
    with pytest.raises(RuntimeError) as exc:
        consult(srv)
    assert time.monotonic() - started < 30  # not the 120s wall clock
    message = str(exc.value)
    assert "no output for 2s" in message
    assert "ADVISOR_IDLE_TIMEOUT" in message


def test_a_steady_answer_outlives_the_idle_timeout(server, fake_claude,
                                                   monkeypatch):
    """Four seconds of answer, never more than a fraction of a second apart,
    against a two-second idle limit: slow is not stuck."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "trickle")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "4")
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_IDLE_TIMEOUT="2")
    assert "FAKE ANSWER" in consult(srv)


def test_idle_timeout_zero_leaves_only_the_wall_clock(server, fake_claude,
                                                      monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_IDLE_TIMEOUT="0",
                 ADVISOR_TIMEOUT="3", ADVISOR_TIMEOUT_SCALE="0")
    with pytest.raises(RuntimeError) as exc:
        consult(srv)
    assert "within 3s" in str(exc.value)


def test_an_error_result_in_the_stream_is_still_classified(server, fake_claude,
                                                           monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "usage_limit")
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(srv.UsageLimitError):
        consult(srv)


def test_the_idle_limit_is_handed_to_the_runner(server, fake_claude,
                                               monkeypatch):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_IDLE_TIMEOUT="45")
    seen = {}
    real = srv._run_claude

    def spy(cmd, env=None, workdir=None, timeout_s=60, stdin_text="", **kw):
        seen.update(kw)
        return real(cmd, env, workdir, timeout_s, stdin_text, **kw)

    monkeypatch.setattr(srv, "_run_claude", spy)
    consult(srv)
    assert seen.get("idle_s") == 45


async def test_progress_says_what_claude_is_doing(fake_claude):
    messages = []

    async def on_progress(progress, total, message):
        messages.append(message or "")

    async with advisor_session(fake_claude, FAKE_CLAUDE_MODE="trickle",
                               FAKE_CLAUDE_SLEEP="4",
                               ADVISOR_PROGRESS_INTERVAL="1") as s:
        result = await s.call_tool("ask_wisdomtooth", {
            "question": "q", "context": "c", "attempts_so_far": "a"},
            progress_callback=on_progress)
    text = "\n".join(b.text for b in result.content
                     if isinstance(b, types.TextContent))
    assert "FAKE ANSWER" in text
    assert any("words so far" in m for m in messages), messages


# --------------------------------------------------------------------------
# Error results carry `errors`, not `result`
# --------------------------------------------------------------------------
# The CLI's error results (subtype error_during_execution, error_max_turns,
# error_max_budget_usd, ...) have an `errors` list and no `result` field, per
# the Agent SDK's SDKResultMessage -- in both output formats.

def test_an_error_result_reports_its_errors_not_raw_json(server, fake_claude,
                                                         monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error_list")
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(RuntimeError) as exc:
        consult(srv)
    message = str(exc.value)
    assert "529 Overloaded" in message
    assert '"subtype"' not in message


def test_the_spend_cap_is_named_when_it_stops_a_consult(server, fake_claude,
                                                        monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "over_budget")
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_MAX_BUDGET_USD="0.25")
    with pytest.raises(RuntimeError) as exc:
        consult(srv)
    assert "ADVISOR_MAX_BUDGET_USD" in str(exc.value)
    assert "0.25" in str(exc.value)
