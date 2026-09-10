"""What 0.9.0 added: the usage ledger and caps, the repeat guard, files the
server reads itself, follow-ups, caller presets, and the per-consult plumbing
underneath them (prompt files, cancellation, the backend registry).

Everything runs against the fake CLI, which reports 1,200 input and 340 output
tokens at $0.01 for every successful consult.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from test_mcp_protocol import advisor_session, field


def consult(srv, **kw):
    kw.setdefault("question", "why is this broken?")
    return srv._consult(**kw)


def ledger(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def prompt_calls(fake_claude) -> list:
    """Invocations that carried a consult, not --help probes or auth checks."""
    return [c for c in fake_claude.calls() if c["prompt"]]


# --------------------------------------------------------------------------
# The usage ledger
# --------------------------------------------------------------------------

def test_a_consult_is_logged_with_tokens_and_cost(server, fake_claude,
                                                  usage_file):
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv, kind="ask_wisdomtooth")

    [record] = ledger(usage_file)
    assert record["status"] == "ok"
    assert record["kind"] == "ask_wisdomtooth"
    assert record["backend"] == "claude-code"
    assert record["input_tokens"] == 1200
    assert record["output_tokens"] == 340
    # The CLI's own API-equivalent figure wins over a local estimate.
    assert record["cost_usd"] == pytest.approx(0.01)
    assert record["cost_source"] == "cli"
    assert record["transcript"]


def test_the_answer_footer_reports_usage(server, fake_claude):
    answer = consult(server(ADVISOR_BACKEND="claude-code"))
    assert "1,200 tokens in" in answer
    assert "340 out" in answer
    assert "≈$0.01 at API rates" in answer


def test_a_failed_consult_is_logged_as_an_error(server, fake_claude,
                                                usage_file, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(RuntimeError):
        consult(srv)
    [record] = ledger(usage_file)
    assert record["status"] == "error"


def test_the_ledger_can_be_turned_off(server, fake_claude, usage_file):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_USAGE_LOG="0")
    consult(srv)
    assert not usage_file.exists()
    # The report still covers this process.
    assert "this server process only" in srv._usage_report()
    assert "5 h: 1 consults" in srv.advisor_status()


def test_api_cost_is_estimated_from_list_prices(server):
    srv = server()
    assert srv._estimate_cost("claude-opus-5", 1_000_000, 1_000_000) == \
        pytest.approx(30.0)
    assert srv._estimate_cost("claude-sonnet-5", 1_000_000, 0) == \
        pytest.approx(2.0)
    assert srv._estimate_cost("deep", 0, 1_000_000) == pytest.approx(25.0)
    assert srv._estimate_cost("claude-opus-5", cache_read=1_000_000) == \
        pytest.approx(0.5)
    # A model released after the table: no estimate beats a wrong one.
    assert srv._estimate_cost("claude-someday-9", 1000, 1000) is None


def test_api_usage_is_read_from_the_response(server):
    class Usage:
        input_tokens = 1000
        output_tokens = 500
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    class Message:
        usage = Usage()
        model = "claude-haiku-4-5"  # e.g. served by a refusal fallback

    out = server()._api_usage(Message(), "claude-opus-5")
    assert out["input_tokens"] == 1000
    assert out["output_tokens"] == 500
    # Costed as the model that actually served it.
    assert out["cost_usd"] == pytest.approx((1000 * 1 + 500 * 5) / 1e6)


def test_the_report_summarises_windows_and_models(server, usage_file):
    now = time.time()
    rows = [
        dict(ts=now - 60, status="ok", model="claude-opus-5",
             input_tokens=1000, output_tokens=200, cost_usd=0.01,
             duration_s=10),
        dict(ts=now - 60, status="repeat", model="claude-opus-5"),
        dict(ts=now - 3 * 3600, status="ok", model="claude-sonnet-5",
             input_tokens=500, output_tokens=100, cost_usd=0.002,
             duration_s=5),
        dict(ts=now - 10 * 86400, status="ok", model="claude-opus-5",
             input_tokens=9, output_tokens=9),
    ]
    usage_file.write_text("".join(json.dumps(r) + "\n" for r in rows),
                          encoding="utf-8")
    report = server()._usage_report(7)

    def row(label):
        line = next(l for l in report.splitlines() if l.startswith(label))
        return line[len(label):].split()

    assert row("1 h")[:2] == ["1", "1"]   # consults, repeats
    assert row("5 h")[0] == "2"
    assert row("7 d")[0] == "2"           # the 10-day-old row is outside
    assert "claude-opus-5" in report and "claude-sonnet-5" in report


def test_tokens_from_failed_consults_still_count(server, usage_file):
    """A CLI run can fail after the model ran; those tokens were spent."""
    now = time.time()
    rows = [dict(ts=now - 60, status="ok", input_tokens=1000, cost_usd=0.01),
            dict(ts=now - 60, status="error", input_tokens=500, cost_usd=0.02),
            dict(ts=now - 60, status="repeat", input_tokens=9999)]
    s = server()._summarise(rows)
    assert s["consults"] == 1
    assert s["input"] == 1500
    assert s["cost"] == pytest.approx(0.03)

    usage_file.write_text("".join(json.dumps(r) + "\n" for r in rows),
                          encoding="utf-8")
    with pytest.raises(RuntimeError):
        server(ADVISOR_MAX_USD_PER_DAY="0.025")._check_caps()


def test_sub_cent_costs_do_not_read_as_free(server):
    srv = server()
    assert srv._fmt_usd(0.0034) == "$0.0034"
    assert srv._fmt_usd(0.01) == "$0.01"
    assert srv._fmt_usd(0) == "$0.00"


def test_a_torn_ledger_line_is_skipped(server, usage_file):
    usage_file.write_text('{"ts": 1, "status": "ok"\n{not json}\n',
                          encoding="utf-8")
    assert "USAGE LEDGER" in server()._usage_report()


# --------------------------------------------------------------------------
# Caps
# --------------------------------------------------------------------------

def test_an_hourly_cap_blocks_the_next_consult_without_calling_claude(
        server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_MAX_CONSULTS_PER_HOUR="1")
    consult(srv, question="first")
    before = len(prompt_calls(fake_claude))

    with pytest.raises(RuntimeError) as exc:
        consult(srv, question="second")
    assert "cap reached" in str(exc.value).lower()
    assert "ADVISOR_MAX_CONSULTS_PER_HOUR" in str(exc.value)
    assert len(prompt_calls(fake_claude)) == before


def test_the_daily_dollar_cap(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_MAX_USD_PER_DAY="0.01")
    consult(srv, question="first")
    with pytest.raises(RuntimeError) as exc:
        consult(srv, question="second")
    assert "ADVISOR_MAX_USD_PER_DAY" in str(exc.value)


def test_caps_count_every_process_through_the_ledger(server, usage_file):
    now = time.time()
    usage_file.write_text("".join(
        json.dumps(dict(ts=now - 60 * i, status="ok")) + "\n" for i in range(3)),
        encoding="utf-8")
    with pytest.raises(RuntimeError):
        server(ADVISOR_MAX_CONSULTS_PER_5H="3")._check_caps()


def test_repeats_do_not_count_against_a_cap(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_MAX_CONSULTS_PER_HOUR="1")
    consult(srv, question="same")
    assert "[repeat:" in consult(srv, question="same")


# --------------------------------------------------------------------------
# The repeat guard
# --------------------------------------------------------------------------

def test_an_identical_consult_returns_the_saved_answer_for_free(
        server, fake_claude, usage_file):
    srv = server(ADVISOR_BACKEND="claude-code")
    first_saved, second_saved = {}, {}
    consult(srv, context="same", saved=first_saved)
    second = consult(srv, context="same", saved=second_saved)

    assert len(prompt_calls(fake_claude)) == 1
    assert "FAKE ANSWER" in second
    assert "[repeat:" in second
    assert second_saved["path"] == first_saved["path"]
    assert [r["status"] for r in ledger(usage_file)] == ["ok", "repeat"]


def test_new_context_is_not_a_repeat(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv, context="first attempt")
    consult(srv, context="first attempt, plus the new stack trace")
    assert len(prompt_calls(fake_claude)) == 2


def test_the_repeat_guard_can_be_turned_off(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_REPEAT_WINDOW="0")
    consult(srv, context="same")
    consult(srv, context="same")
    assert len(prompt_calls(fake_claude)) == 2


# --------------------------------------------------------------------------
# Files the server reads itself
# --------------------------------------------------------------------------

@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("def handler():\n    return 42\n",
                                 encoding="utf-8")
    return root


def test_files_are_read_by_the_server(server, fake_claude, project):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_FILE_ROOTS=str(project))
    consult(srv, context_files=["app.py"])
    prompt = fake_claude.last["prompt"]
    assert '<file path="app.py">' in prompt
    assert "return 42" in prompt


@pytest.mark.parametrize("path", ["../secret.py", "SECRET_ABS"])
def test_files_outside_the_roots_are_refused(server, fake_claude, project,
                                             tmp_path, path):
    secret = tmp_path / "secret.py"
    secret.write_text("TOP SECRET", encoding="utf-8")
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_FILE_ROOTS=str(project))
    with pytest.raises(ValueError) as exc:
        consult(srv, context_files=[str(secret) if path == "SECRET_ABS" else path])
    assert "outside the allowed folders" in str(exc.value)
    assert not prompt_calls(fake_claude)


def test_credential_files_are_refused_but_examples_are_not(
        server, fake_claude, project):
    (project / ".env").write_text("TOKEN=do-not-send", encoding="utf-8")
    (project / ".env.example").write_text("TOKEN=", encoding="utf-8")
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_FILE_ROOTS=str(project))
    consult(srv, context_files=[".env", ".env.example"])
    prompt = fake_claude.last["prompt"]
    assert "do-not-send" not in prompt
    assert '<file path=".env.example">' in prompt
    assert "looks like a credential file" in prompt


def test_binary_files_are_skipped(server, fake_claude, project):
    (project / "blob.bin").write_bytes(b"\x00\x01\x02")
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_FILE_ROOTS=str(project))
    consult(srv, context_files=["blob.bin", "app.py"])
    assert "skipped, binary" in fake_claude.last["prompt"]


def test_secrets_in_read_files_are_still_redacted(server, fake_claude, project):
    (project / "cfg.py").write_text('KEY = "sk-ant-api03-CCCCCCCCCCCCCCCCCCCC"',
                                    encoding="utf-8")
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_FILE_ROOTS=str(project))
    consult(srv, context_files=["cfg.py"])
    assert "sk-ant-api03-CCCC" not in fake_claude.last["prompt"]


def test_the_working_directory_is_the_default_root(server, fake_claude,
                                                   project, monkeypatch):
    monkeypatch.chdir(project)
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv, context_files=["app.py"])
    assert "return 42" in fake_claude.last["prompt"]


def test_the_home_folder_is_too_broad_to_expose(server, fake_claude, tmp_path,
                                                monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(home)
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(ValueError) as exc:
        consult(srv, context_files=["app.py"])
    assert "ADVISOR_FILE_ROOTS" in str(exc.value)


# --------------------------------------------------------------------------
# Follow-ups
# --------------------------------------------------------------------------

def test_a_follow_up_resends_the_earlier_exchange(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    saved: dict = {}
    consult(srv, question="FIRST-QUESTION", saved=saved)
    consult(srv, question="second", follow_up_of=Path(saved["path"]).name)

    prompt = fake_claude.last["prompt"]
    assert "<previous_consult>" in prompt
    assert "FIRST-QUESTION" in prompt
    assert "FAKE ANSWER" in prompt


@pytest.mark.parametrize("form", ["path", "backticked"])
def test_a_follow_up_accepts_the_saved_line_as_printed(server, fake_claude,
                                                       form):
    srv = server(ADVISOR_BACKEND="claude-code")
    saved: dict = {}
    consult(srv, question="FIRST-QUESTION", saved=saved)
    ref = saved["path"] if form == "path" else f"`{saved['path']}`"
    consult(srv, question="second", follow_up_of=ref)
    assert "FIRST-QUESTION" in fake_claude.last["prompt"]


def test_a_large_earlier_consult_does_not_crowd_out_the_follow_up(
        server, fake_claude):
    """The cap's middle cut used to land on the earlier answer."""
    srv = server(ADVISOR_BACKEND="claude-code")
    saved: dict = {}
    consult(srv, question="first", context="x" * 58000, saved=saved)
    consult(srv, question="second", context="NEW-CONTEXT",
            follow_up_of=Path(saved["path"]).name)

    prompt = fake_claude.last["prompt"]
    assert "FAKE ANSWER" in prompt
    assert "NEW-CONTEXT" in prompt
    assert "advisor-server truncated" in prompt
    assert len(prompt) < srv.MAX_CONTEXT_CHARS


@pytest.mark.parametrize("ref", ["nope.md", "../../outside.md", "C:evil"])
def test_a_follow_up_only_reads_saved_transcripts(server, fake_claude,
                                                  tmp_path, ref):
    (tmp_path / "outside.md").write_text(
        "## Sent to Claude\n\nx\n\n## Claude's answer\n\nLEAKED",
        encoding="utf-8")
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(ValueError) as exc:
        consult(srv, question="q", follow_up_of=ref)
    assert "no saved consult" in str(exc.value)
    assert not prompt_calls(fake_claude)


# --------------------------------------------------------------------------
# Caller presets
# --------------------------------------------------------------------------

def test_the_default_preset_is_medium(server):
    srv = server()
    assert srv.PRESET == "medium"
    assert srv.ANSWER_BUDGET == 2000
    assert srv.MINIMAL_TOOLS is False


def test_the_small_preset_suits_local_models(server):
    srv = server(ADVISOR_PRESET="small")
    assert srv.ANSWER_BUDGET == 600
    assert srv.MINIMAL_TOOLS is True


def test_the_large_preset_suits_frontier_callers(server):
    assert server(ADVISOR_PRESET="large").ANSWER_BUDGET == 64000


def test_explicit_settings_beat_the_preset(server):
    srv = server(ADVISOR_PRESET="small", ADVISOR_ANSWER_BUDGET="900",
                 ADVISOR_MINIMAL_TOOLS="0")
    assert srv.ANSWER_BUDGET == 900
    assert srv.MINIMAL_TOOLS is False


def test_an_unknown_preset_falls_back_to_the_default(server):
    assert server(ADVISOR_PRESET="enormous").PRESET == "medium"


# --------------------------------------------------------------------------
# Per-consult plumbing
# --------------------------------------------------------------------------

def test_each_consult_gets_its_own_prompt_file_and_it_is_removed(
        server, fake_claude):
    """A shared file let concurrent consults read each other's prompts."""
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_REPEAT_WINDOW="0")
    consult(srv)
    consult(srv)
    calls = prompt_calls(fake_claude)
    paths = [fake_claude.flag_value("--system-prompt-file", c) for c in calls]
    assert len(set(paths)) == 2
    assert not any(os.path.exists(p) for p in paths)
    assert all("expert technical advisor" in c["system_prompt"] for c in calls)


async def test_cancelling_a_consult_stops_the_claude_process(
        server, fake_claude, monkeypatch):
    """A client that gives up must not leave claude running on its quota."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_TIMEOUT="120",
                 ADVISOR_TIMEOUT_SCALE="0")
    holders = []

    class Spy(srv._Cancellation):
        def __init__(self):
            super().__init__()
            holders.append(self)

    monkeypatch.setattr(srv, "_Cancellation", Spy)
    task = asyncio.ensure_future(
        srv._consult_with_heartbeat(None, "q", context="c"))
    for _ in range(150):
        await asyncio.sleep(0.1)
        if prompt_calls(fake_claude):
            break
    assert prompt_calls(fake_claude), "the fake CLI never got the prompt"
    proc = holders[0].proc

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if proc.poll() is not None:
            break
        await asyncio.sleep(0.1)
    assert proc.poll() is not None


def test_a_cancelled_api_stream_is_closed(server):
    """The API backend has no process to kill; it stops at the next event."""
    srv = server()
    closed = []

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append(True)
            return False

        def __iter__(self):
            holder.cancel()
            yield "event"
            pytest.fail("kept streaming after cancellation")

        def get_final_message(self):
            pytest.fail("a cancelled stream has no final message")

    class Api:
        def stream(self, **kwargs):
            return Stream()

    holder = srv._Cancellation()
    token = srv._CANCELLATION.set(holder)
    try:
        with pytest.raises(RuntimeError) as exc:
            srv._stream(Api(), model="m")
    finally:
        srv._CANCELLATION.reset(token)
    assert "cancelled" in str(exc.value)
    assert closed


def test_a_failed_feature_probe_is_retried(server, monkeypatch):
    """Caching an empty probe pinned the process to a bare command line."""
    srv = server()
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        raise OSError("probe timed out")

    monkeypatch.setattr(srv, "_run_claude", run)
    assert srv._cli_features("claude-x") == set()
    srv._cli_features("claude-x")
    assert len(calls) == 2


def test_a_registered_backend_can_be_selected(server, usage_file):
    srv = server(ADVISOR_BACKEND="echo")
    seen = {}

    def echo(system, user_content, model, effort, max_tokens):
        seen["prompt"] = user_content
        srv._note_usage(input_tokens=10, output_tokens=5, cost_usd=0.001)
        return "echo answer"

    srv.register_backend(srv.Backend(
        name="echo", provider="test", billing="TEST ACCOUNT",
        consult=echo, available=lambda: True))

    answer = srv._consult(question="MARKER")
    assert answer.startswith("echo answer")
    assert "billed to TEST ACCOUNT" in answer
    assert "MARKER" in seen["prompt"]
    [record] = ledger(usage_file)
    assert record["backend"] == "echo"
    assert record["output_tokens"] == 5
    assert "TEST ACCOUNT" in srv.advisor_status()


# --------------------------------------------------------------------------
# Over the wire
# --------------------------------------------------------------------------

async def test_the_new_arguments_and_tool_are_advertised(fake_claude):
    async with advisor_session(fake_claude) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
    props = field(tools["ask_wisdomtooth"], "inputSchema",
                  "input_schema")["properties"]
    assert {"context_files", "follow_up_of"} <= set(props)
    assert "context_files" in field(tools["review_code"], "inputSchema",
                                    "input_schema")["properties"]
    assert "advisor_usage" in tools
