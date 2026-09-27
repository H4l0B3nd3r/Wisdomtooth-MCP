"""End-to-end MCP: a real client speaking stdio to a real server process.

This is the test that answers "do the tool calls actually work for a local
agent?" -- it goes over the wire, not through Python imports. The server subprocess
is pointed at the fake Claude CLI, so nothing is billed.
"""

import os
import sys
from pathlib import Path

from contextlib import asynccontextmanager
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

PKG_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {"ask_wisdomtooth", "review_code", "compare_approaches",
                  "advisor_status", "advisor_auth_check", "advisor_models",
                  "advisor_configure", "advisor_usage", "multi_advisor",
                  "advisor_connect", "advisor_disconnect"}


def _params(**env):
    child = os.environ.copy()
    # Force the subscription path onto the fake CLI installed by `fake_claude`.
    child.update({
        "ADVISOR_BACKEND": "claude-code",
        "PYTHONPATH": str(PKG_ROOT),
        "PYTHONIOENCODING": "utf-8",
    })
    child.pop("ANTHROPIC_API_KEY", None)
    child.update(env)
    return StdioServerParameters(
        command=sys.executable,
        args=["-c", "from wisdomtooth.server import main; main()"],
        env=child,
    )


def field(obj, *names):
    """Read a field under whichever spelling this SDK uses.

    mcp 1.x models are camelCase (`isError`, `inputSchema`); 2.x is snake_case
    (`is_error`, `input_schema`). The wire format is identical either way.
    """
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    raise AttributeError(f"none of {names} on {type(obj).__name__}")


async def _call(session, name, args):
    result = await session.call_tool(name, args)
    text = "\n".join(b.text for b in result.content
                     if isinstance(b, types.TextContent))
    return result, text


@asynccontextmanager
async def advisor_session(fake_claude, **env):
    """An initialized client session against a freshly launched server.

    Deliberately a context manager rather than a fixture: `stdio_client` owns an
    anyio task group, and unwinding that from a fixture teardown happens in a
    different task, which anyio rejects.
    """
    params = _params(ADVISOR_CLAUDE_BIN=str(fake_claude.path),
                     FAKE_CLAUDE_LOG=str(fake_claude.log), **env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


# --------------------------------------------------------------------------
# Handshake and discovery
# --------------------------------------------------------------------------

async def test_server_initializes(fake_claude):
    params = _params(ADVISOR_CLAUDE_BIN=str(fake_claude.path))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            result = await session.initialize()
            assert field(result, "serverInfo", "server_info").name == "wisdomtooth"
            assert field(result, "protocolVersion", "protocol_version")


async def test_every_tool_is_advertised(fake_claude):
    async with advisor_session(fake_claude) as session:
        names = {t.name for t in (await session.list_tools()).tools}
        assert names == EXPECTED_TOOLS


async def test_tools_carry_descriptions_and_schemas(fake_claude):
    async with advisor_session(fake_claude) as session:
        for tool in (await session.list_tools()).tools:
            assert tool.description, tool.name
            assert field(tool, "inputSchema", "input_schema")["type"] == "object"


async def test_consulting_tools_are_annotated_read_only(fake_claude):
    """Clients use annotations to decide what needs an approval prompt."""
    async with advisor_session(fake_claude) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        for name in ("ask_wisdomtooth", "review_code", "compare_approaches"):
            assert tools[name].annotations is not None, name
            ann = tools[name].annotations
            assert field(ann, "readOnlyHint", "read_only_hint") is True, name
            assert field(ann, "openWorldHint", "open_world_hint") is True, name


async def test_escalation_policy_is_in_the_server_instructions(fake_claude):
    params = _params(ADVISOR_CLAUDE_BIN=str(fake_claude.path))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            result = await session.initialize()
            assert "ESCALATION" in (result.instructions or "").upper()


async def test_required_arguments_are_declared(fake_claude):
    async with advisor_session(fake_claude) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        schema = field(tools["ask_wisdomtooth"], "inputSchema", "input_schema")
        required = set(schema.get("required", []))
        assert {"question", "context", "attempts_so_far"} <= required


# --------------------------------------------------------------------------
# Calling the tools
# --------------------------------------------------------------------------

async def test_ask_wisdomtooth_returns_an_answer(fake_claude):
    async with advisor_session(fake_claude) as session:
        result, text = await _call(session, "ask_wisdomtooth", {
            "question": "why does the build fail?",
            "context": "cargo build, linker error LNK2019",
            "attempts_so_far": "read the docs, cleaned target/",
        })
        assert field(result, "isError", "is_error") is not True
        assert "FAKE ANSWER" in text


async def test_ask_wisdomtooth_forwards_all_three_required_fields(fake_claude):
    async with advisor_session(fake_claude) as session:
        await _call(session, "ask_wisdomtooth", {
            "question": "QQQ", "context": "CCC", "attempts_so_far": "AAA"})
        prompt = fake_claude.last["prompt"]
        assert "QQQ" in prompt and "CCC" in prompt and "AAA" in prompt


async def test_review_code_works(fake_claude):
    async with advisor_session(fake_claude) as session:
        _, text = await _call(session, "review_code", {
            "code": "def f(x):\n    return x / 0", "concern": "correctness"})
        assert "FAKE ANSWER" in text
        assert "return x / 0" in fake_claude.last["prompt"]


async def test_compare_approaches_works(fake_claude):
    async with advisor_session(fake_claude) as session:
        _, text = await _call(session, "compare_approaches", {
            "problem": "queue choice", "options": "redis\nsqs", "criteria": "cheap"})
        assert "FAKE ANSWER" in text


async def test_missing_required_argument_is_an_error(fake_claude):
    async with advisor_session(fake_claude) as session:
        result = await session.call_tool("ask_wisdomtooth", {"question": "q"})
        assert field(result, "isError", "is_error") is True


async def test_status_makes_no_model_call(fake_claude):
    async with advisor_session(fake_claude) as session:
        before = len(fake_claude.calls())
        _, text = await _call(session, "advisor_status", {})
        assert "backend" in text.lower()
        assert len(fake_claude.calls()) == before


async def test_auth_check_reports_login_state(fake_claude):
    async with advisor_session(fake_claude) as session:
        _, text = await _call(session, "advisor_auth_check", {})
        assert "claude.ai" in text or "logged in" in text.lower()


async def test_a_backend_failure_is_returned_as_a_tool_error(
        fake_claude, monkeypatch):
    params = _params(ADVISOR_CLAUDE_BIN=str(fake_claude.path),
                     FAKE_CLAUDE_MODE="crash")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("ask_wisdomtooth", {
                "question": "q", "context": "c", "attempts_so_far": "a"})
            assert field(result, "isError", "is_error") is True


# --------------------------------------------------------------------------
# The server must stay responsive during a consult
# --------------------------------------------------------------------------

async def test_server_answers_other_requests_during_a_slow_consult(fake_claude):
    """A blocking consult used to make the server deaf to protocol traffic,
    which some clients treat as a dead server."""
    import anyio

    params = _params(ADVISOR_CLAUDE_BIN=str(fake_claude.path),
                     FAKE_CLAUDE_LOG=str(fake_claude.log),
                     FAKE_CLAUDE_MODE="hang", ADVISOR_TIMEOUT="8",
                     ADVISOR_TIMEOUT_SCALE="0")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            async with anyio.create_task_group() as tg:
                async def slow():
                    with anyio.move_on_after(10):
                        await session.call_tool("ask_wisdomtooth", {
                            "question": "q", "context": "c",
                            "attempts_so_far": "a"})

                tg.start_soon(slow)
                await anyio.sleep(1.5)
                with anyio.fail_after(10):
                    tools = await session.list_tools()
                assert tools.tools
                tg.cancel_scope.cancel()


async def test_a_long_consult_sends_progress_heartbeats(fake_claude):
    """Kilo aborts a tool call that stays silent past its per-server timeout
    but restarts that clock on every progress notification, so a consult
    longer than the client's limit survives only if the server keeps talking."""
    ticks = []

    async def on_progress(progress, total, message):
        ticks.append(progress)

    async with advisor_session(fake_claude, FAKE_CLAUDE_MODE="slow",
                               FAKE_CLAUDE_SLEEP="4",
                               ADVISOR_PROGRESS_INTERVAL="1") as s:
        result = await s.call_tool("ask_wisdomtooth", {
            "question": "q", "context": "c", "attempts_so_far": "a"},
            progress_callback=on_progress)
    text = "\n".join(b.text for b in result.content
                     if isinstance(b, types.TextContent))
    assert "FAKE ANSWER" in text
    assert len(ticks) >= 2
    assert ticks == sorted(ticks)


async def test_the_context_parameter_is_not_advertised(fake_claude):
    """The heartbeat's `ctx` is injected by the SDK, never a tool argument."""
    async with advisor_session(fake_claude) as session:
        for tool in (await session.list_tools()).tools:
            schema = field(tool, "inputSchema", "input_schema")
            assert "ctx" not in schema.get("properties", {}), tool.name


# --------------------------------------------------------------------------
# Guidance must survive the MCP boundary
# --------------------------------------------------------------------------

async def test_input_rejections_keep_their_guidance(fake_claude):
    """The SDK replaces a plain exception's message with "Error executing tool
    X". A validation error whose whole value is telling the user what to do
    instead has to reach them intact."""
    async with advisor_session(fake_claude) as session:
        result, text = await _call(session, "advisor_connect",
                                   {"name": "Bad Name!", "provider": "openai"})
        assert field(result, "isError", "is_error") is True
        assert "lowercase" in text
        assert text.strip() != "Error executing tool advisor_connect"


async def test_configure_rejections_keep_their_guidance(fake_claude):
    async with advisor_session(fake_claude) as session:
        result, text = await _call(session, "advisor_configure",
                                   {"effort": "ludicrous"})
        assert field(result, "isError", "is_error") is True
        assert "low" in text and "xhigh" in text


async def test_backend_failures_keep_their_diagnostics(fake_claude):
    """An auth failure's login instructions are the entire value of the error."""
    async with advisor_session(fake_claude, FAKE_CLAUDE_MODE="auth_fail") as s:
        result, text = await _call(s, "ask_wisdomtooth", {
            "question": "q", "context": "c", "attempts_so_far": "a"})
        assert field(result, "isError", "is_error") is True
        assert "/login" in text


# --------------------------------------------------------------------------
# Minimal tool surface, for small-context local models
# --------------------------------------------------------------------------

ESSENTIAL_TOOLS = {"ask_wisdomtooth", "advisor_status"}


async def test_minimal_mode_hides_the_admin_tools(fake_claude):
    """Every tool schema is charged against the calling model's context on
    every turn, and a longer tool list measurably degrades tool selection in
    small models. The admin tools exist for the operator, not the agent."""
    async with advisor_session(fake_claude, ADVISOR_MINIMAL_TOOLS="1") as s:
        names = {t.name for t in (await s.list_tools()).tools}
        assert names == ESSENTIAL_TOOLS


async def test_minimal_mode_still_consults(fake_claude):
    async with advisor_session(fake_claude, ADVISOR_MINIMAL_TOOLS="1") as s:
        result, text = await _call(s, "ask_wisdomtooth", {
            "question": "q", "context": "c", "attempts_so_far": "a"})
        assert field(result, "isError", "is_error") is not True
        assert "FAKE ANSWER" in text


async def test_minimal_mode_is_materially_smaller(fake_claude):
    """The point is context saved, so measure it rather than assume it."""
    def size(tools):
        return sum(len(t.name) + len(t.description or "")
                   + len(str(field(t, "inputSchema", "input_schema")))
                   for t in tools)

    async with advisor_session(fake_claude) as s:
        full = size((await s.list_tools()).tools)
    async with advisor_session(fake_claude, ADVISOR_MINIMAL_TOOLS="1") as s:
        minimal = size((await s.list_tools()).tools)
    assert minimal < full / 2


# --------------------------------------------------------------------------
# Several advisors over the wire
# --------------------------------------------------------------------------

import json as _json  # noqa: E402


def _advisors(fake_openai):
    return _json.dumps({
        "gpt": {"provider": "openai-compatible",
                "base_url": fake_openai.base_url, "model": "gpt-fake"},
        "gem": {"provider": "openai-compatible",
                "base_url": fake_openai.base_url, "model": "gem-fake"}})


async def test_multi_advisor_is_declared_for_both_uses(fake_claude):
    async with advisor_session(fake_claude) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        schema = field(tools["multi_advisor"], "inputSchema", "input_schema")
        props = schema["properties"]
        assert {"question", "context", "advisors", "targeted_questions",
                "confirm_over_limit"} <= set(props)
        assert "3" in tools["multi_advisor"].description
        ann = tools["multi_advisor"].annotations
        assert field(ann, "openWorldHint", "open_world_hint") is True


async def test_ask_wisdomtooth_takes_an_advisor_over_the_wire(fake_claude,
                                                             fake_openai):
    async with advisor_session(fake_claude,
                               ADVISOR_ADVISORS_JSON=_advisors(fake_openai)) as s:
        result, text = await _call(s, "ask_wisdomtooth", {
            "question": "q", "context": "c", "attempts_so_far": "a",
            "advisor": "gem"})
        assert field(result, "isError", "is_error") is not True
        assert "FAKE OPENAI ANSWER" in text
        assert fake_openai.last["body"]["model"] == "gem-fake"


async def test_multi_advisor_end_to_end_same_question(fake_claude,
                                                       fake_openai):
    async with advisor_session(fake_claude,
                               ADVISOR_ADVISORS_JSON=_advisors(fake_openai)) as s:
        result = await s.call_tool("multi_advisor", {
            "question": "which queue?", "context": "c",
            "attempts_so_far": "a", "advisors": ["claude", "gpt", "gem"]})
        assert field(result, "isError", "is_error") is not True
        text = "\n".join(b.text for b in result.content
                         if isinstance(b, types.TextContent))
        links = [b for b in result.content if getattr(b, "type", "") ==
                 "resource_link"]
        assert "FAKE ANSWER" in text and text.count("FAKE OPENAI ANSWER") == 2
        assert len(links) == 3
        assert "which queue?" in fake_claude.last["prompt"]


async def test_multi_advisor_end_to_end_targeted(fake_claude, fake_openai):
    async with advisor_session(fake_claude,
                               ADVISOR_ADVISORS_JSON=_advisors(fake_openai)) as s:
        _, text = await _call(s, "multi_advisor", {
            "context": "c", "targeted_questions": {
                "claude": "TECH?", "gem": "UIUX?", "gpt": "REVIEW?"}})
        assert "TECH?" in fake_claude.last["prompt"]
        by_model = {c["body"]["model"]: c["body"]["messages"][1]["content"]
                    for c in fake_openai.chats()}
        assert "UIUX?" in by_model["gem-fake"]
        assert "REVIEW?" in by_model["gpt-fake"]
        assert "targeted" in text.lower()


async def test_a_held_consult_reaches_the_agent_with_its_instructions(
        fake_claude, fake_openai):
    advisors = _json.dumps({"claude": {"allowance_tokens": 10,
                                       "allowance_window": "day"}})
    async with advisor_session(fake_claude,
                               ADVISOR_ADVISORS_JSON=advisors) as s:
        result, text = await _call(s, "ask_wisdomtooth", {
            "question": "q", "context": "c", "attempts_so_far": "a"})
        assert field(result, "isError", "is_error") is True
        assert "confirm_over_limit" in text
        result, text = await _call(s, "ask_wisdomtooth", {
            "question": "q", "context": "c", "attempts_so_far": "a",
            "confirm_over_limit": True})
        assert field(result, "isError", "is_error") is not True
        assert "FAKE ANSWER" in text


async def test_advisor_connect_over_the_wire(fake_claude, fake_openai):
    async with advisor_session(fake_claude) as s:
        _, text = await _call(s, "advisor_connect", {
            "name": "local", "provider": "openai-compatible",
            "base_url": fake_openai.base_url, "model": "fake-model-a"})
        assert "connected" in text.lower()
        _, text = await _call(s, "ask_wisdomtooth", {
            "question": "q", "context": "c", "attempts_so_far": "a",
            "advisor": "local"})
        assert "FAKE OPENAI ANSWER" in text
        _, status = await _call(s, "advisor_status", {})
        assert "local" in status
