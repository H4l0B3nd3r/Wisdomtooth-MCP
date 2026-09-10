"""The server answers the client promptly even while it waits on the CLI.

Two ways it used to go deaf: the startup banner ran `claude auth status`
before the MCP handshake (Kilo gives a server 30s to connect), and on mcp 1.x
a plain `def` tool runs on the event loop, so `advisor_status` probing a slow
CLI froze every other request.
"""

import time

import anyio
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from test_mcp_protocol import _call, _params


def params(fake_claude, **env):
    return _params(ADVISOR_CLAUDE_BIN=str(fake_claude.path),
                   FAKE_CLAUDE_LOG=str(fake_claude.log), **env)


async def test_the_handshake_does_not_wait_for_the_login_probe(fake_claude):
    started = time.monotonic()
    async with stdio_client(params(fake_claude, ADVISOR_BACKEND="auto",
                                   FAKE_AUTH_SLEEP="12")) as (read, write):
        async with ClientSession(read, write) as session:
            with anyio.fail_after(30):
                await session.initialize()
            elapsed = time.monotonic() - started
    assert elapsed < 9, f"initialize took {elapsed:.1f}s"


async def test_a_slow_status_call_does_not_stall_other_requests(fake_claude):
    async with stdio_client(params(fake_claude, ADVISOR_BACKEND="auto",
                                   FAKE_AUTH_SLEEP="6")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            texts = []
            async with anyio.create_task_group() as tg:
                async def status():
                    texts.append((await _call(session, "advisor_status",
                                              {}))[1])

                tg.start_soon(status)
                await anyio.sleep(0.5)
                started = time.monotonic()
                with anyio.fail_after(20):
                    await session.list_tools()
                assert time.monotonic() - started < 3
            assert "active backend: claude-code" in texts[0]
