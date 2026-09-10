"""The HTTP transport: bearer-token auth, and no unauthenticated public binds.

Anyone who can reach the HTTP port can spend the account behind it. With
ADVISOR_HTTP_TOKEN set, every request needs `Authorization: Bearer <token>`;
without one the server refuses to bind anywhere but loopback, unless the
operator says in so many words (ADVISOR_HTTP_NO_AUTH=1) that something else
guards it.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from wisdomtooth import httpauth

PKG_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# The middleware, driven as a bare ASGI app
# --------------------------------------------------------------------------

async def inner(scope, receive, send):
    if scope["type"] == "lifespan":
        inner.lifespans += 1
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"inner"})
inner.lifespans = 0


async def request(app, headers=()):
    scope = {"type": "http", "method": "POST", "path": "/mcp",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
             "query_string": b"", "http_version": "1.1", "scheme": "http",
             "server": ("127.0.0.1", 8484), "client": ("127.0.0.1", 50000),
             "root_path": ""}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], dict(start.get("headers") or []), sent


async def test_a_request_without_the_token_is_refused():
    status, headers, _ = await request(httpauth.BearerAuth(inner, "s3cret"))
    assert status == 401
    assert headers[b"www-authenticate"].startswith(b"Bearer")


async def test_a_wrong_token_is_refused():
    status, _, _ = await request(httpauth.BearerAuth(inner, "s3cret"),
                                 [("Authorization", "Bearer guess")])
    assert status == 401


async def test_the_right_token_is_let_through():
    status, _, sent = await request(httpauth.BearerAuth(inner, "s3cret"),
                                    [("Authorization", "Bearer s3cret")])
    assert status == 200
    assert sent[-1]["body"] == b"inner"


async def test_the_scheme_is_case_insensitive():
    status, _, _ = await request(httpauth.BearerAuth(inner, "s3cret"),
                                 [("Authorization", "bearer s3cret")])
    assert status == 200


async def test_lifespan_events_pass_through():
    """The MCP session manager starts in the app's lifespan; blocking it
    would leave every request without a session manager."""
    before = inner.lifespans

    async def nothing():
        return {"type": "lifespan.startup"}

    await httpauth.BearerAuth(inner, "s3cret")({"type": "lifespan"},
                                                nothing, None)
    assert inner.lifespans == before + 1


def test_which_binds_need_a_token():
    assert httpauth.exposure_problem("127.0.0.1", "", False) is None
    assert httpauth.exposure_problem("localhost", "", False) is None
    assert httpauth.exposure_problem("::1", "", False) is None
    assert "ADVISOR_HTTP_TOKEN" in httpauth.exposure_problem("0.0.0.0", "",
                                                             False)
    assert httpauth.exposure_problem("0.0.0.0", "tok", False) is None
    assert httpauth.exposure_problem("0.0.0.0", "", True) is None


# --------------------------------------------------------------------------
# Over the wire, against a real server process
# --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(fake_claude, **env):
    child = os.environ.copy()
    child.update({"ADVISOR_BACKEND": "claude-code",
                  "ADVISOR_CLAUDE_BIN": str(fake_claude.path),
                  "FAKE_CLAUDE_LOG": str(fake_claude.log),
                  "ADVISOR_TRANSPORT": "http",
                  "PYTHONPATH": str(PKG_ROOT), "PYTHONIOENCODING": "utf-8"})
    child.pop("ANTHROPIC_API_KEY", None)
    child.update(env)
    # sys.exit(main()), as the installed `wisdomtooth-mcp` script does, so a
    # refusal to start shows up as the exit code.
    return subprocess.Popen(
        [sys.executable, "-c",
         "import sys; from wisdomtooth.server import main; sys.exit(main())"],
        env=child, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace")


def initialize(port, token=None) -> int:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18",
                                  "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "1"}}})
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp",
                                 data=body.encode(), headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def wait_for_port(port, proc, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail("server exited: " + proc.stderr.read())
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    pytest.fail("server never listened on port %d" % port)


def test_the_http_server_enforces_the_token(fake_claude):
    port = free_port()
    proc = start_server(fake_claude, ADVISOR_PORT=str(port),
                        ADVISOR_HTTP_TOKEN="tok-123")
    try:
        wait_for_port(port, proc)
        assert initialize(port) == 401
        assert initialize(port, "wrong") == 401
        assert initialize(port, "tok-123") == 200
    finally:
        proc.kill()
        proc.communicate(timeout=10)


def test_a_loopback_server_without_a_token_still_works(fake_claude):
    port = free_port()
    proc = start_server(fake_claude, ADVISOR_PORT=str(port))
    try:
        wait_for_port(port, proc)
        assert initialize(port) == 200
    finally:
        proc.kill()
        proc.communicate(timeout=10)


def test_a_public_bind_without_a_token_refuses_to_start(fake_claude):
    proc = start_server(fake_claude, ADVISOR_HOST="0.0.0.0",
                        ADVISOR_PORT=str(free_port()))
    try:
        _, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("the server started on 0.0.0.0 without a token")
    assert proc.returncode != 0
    assert "ADVISOR_HTTP_TOKEN" in err
