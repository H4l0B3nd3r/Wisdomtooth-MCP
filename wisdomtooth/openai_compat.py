"""The OpenAI chat-completions protocol, over the standard library.

ChatGPT, Gemini's compatibility endpoint, OpenRouter, LM Studio, Ollama, vLLM
and llama.cpp all accept the same request, so one small client serves them
all -- with no SDK dependency, which keeps the install light and lets both the
mcp 1.x and 2.x environments run the same code.

Requests stream (server-sent events) for the same reasons the Claude backends
stream: each event proves the consult is alive, so silence is caught by the
idle limit rather than the wall clock, the heartbeat can say what the model is
doing, and a cancelled consult stops at the next event instead of running on,
billed, to the end.
"""

import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

# Headers worth keeping: the rate-limit family most hosted APIs send, and
# Retry-After on a 429.
_KEPT_HEADERS = ("x-ratelimit-", "retry-after")
_CHARS_PER_WORD = 6


class HTTPFailure(Exception):
    """The endpoint answered with an error status."""

    def __init__(self, status: int, message: str, headers: Optional[dict] = None,
                 code: str = ""):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.headers = headers or {}
        self.code = code


class Unreachable(Exception):
    """Nothing answered at the URL: refused, DNS failure, TLS failure."""


class Silent(Exception):
    """The endpoint sent nothing for `idle_s` seconds."""

    def __init__(self, idle_s: float, elapsed: float):
        super().__init__(f"no data for {idle_s:g}s")
        self.idle_s = idle_s
        self.elapsed = elapsed


class Cancelled(Exception):
    """The client gave up; the stream was closed."""


class WallClock(Exception):
    """The consult outlived its wall-clock limit while still streaming."""

    def __init__(self, timeout_s: float):
        super().__init__(f"exceeded {timeout_s:g}s")
        self.timeout_s = timeout_s


@dataclass
class StreamState:
    """What the stream has said so far, for the heartbeat."""
    phase: str = ""
    chars: int = 0
    response: object = None

    @property
    def note(self) -> str:
        if self.phase == "writing" and self.chars:
            return (f"writing, ~{max(1, self.chars // _CHARS_PER_WORD):,} "
                    "words so far")
        return self.phase

    def close(self) -> None:
        """Unblock a read in progress by shutting the socket down.

        Called from another thread than the one reading. The response itself
        is left for the reading thread to close: closing it here races that
        thread's `readline`, which on Linux then fails inside http.client
        with an AttributeError instead of returning. Only when there is no
        socket to shut down is the response closed from here.
        """
        resp = self.response
        if resp is None:
            return
        try:
            sock = resp.fp.raw._sock  # http.client's socket, behind SocketIO
            sock.shutdown(socket.SHUT_RDWR)
            return
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass


@dataclass
class ChatResult:
    text: str
    usage: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    finish_reason: str = ""
    model: str = ""


def _kept(headers) -> dict:
    return {k.lower(): v for k, v in (headers or {}).items()
            if k.lower().startswith(_KEPT_HEADERS)}


def _error_message(raw: bytes) -> tuple:
    try:
        obj = json.loads(raw or b"{}")
    except ValueError:
        return (raw or b"").decode("utf-8", errors="replace")[:300].strip(), ""
    err = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(err, dict):
        return (str(err.get("message") or err)[:500],
                str(err.get("code") or err.get("type") or ""))
    if isinstance(err, str):
        return err[:500], ""
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        # Google's error envelope arrives as a one-element list.
        return _error_message(json.dumps(obj[0]).encode())
    return json.dumps(obj)[:300], ""


def _headers(key: str) -> dict:
    headers = {"Content-Type": "application/json",
               "Accept": "text/event-stream, application/json",
               "User-Agent": "wisdomtooth-mcp"}
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def ssl_context() -> ssl.SSLContext:
    """Certificate verification the way the Anthropic SDK does it.

    SSL_CERT_FILE or SSL_CERT_DIR when set, otherwise the operating system's
    own trust store through `truststore`. Python's default store is empty on a
    python.org install on macOS, and lacks the CA a corporate proxy adds to
    the system store.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    if os.environ.get("SSL_CERT_DIR"):
        return ssl.create_default_context(capath=os.environ["SSL_CERT_DIR"])
    try:
        import truststore
    except ImportError:  # pragma: no cover - a dependency; defensive only
        return ssl.create_default_context()
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _open(req, timeout):
    try:
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=ssl_context())
    except urllib.error.HTTPError as exc:
        message, code = _error_message(exc.read())
        raise HTTPFailure(exc.code, message or exc.reason, _kept(exc.headers),
                          code) from None
    except (socket.timeout, TimeoutError) as exc:
        raise Silent(timeout, timeout) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise Silent(timeout, timeout) from exc
        raise Unreachable(str(exc.reason)) from exc
    except (ConnectionError, OSError) as exc:
        raise Unreachable(str(exc)) from exc


def get_json(url: str, key: str = "", timeout_s: float = 10.0):
    """GET a JSON document (a model list, a key's balance)."""
    req = urllib.request.Request(url, headers=_headers(key), method="GET")
    with _open(req, timeout_s) as resp:
        return json.loads(resp.read() or b"{}")


def chat(url: str, key: str, body: dict, timeout_s: float, idle_s: float = 0,
         holder=None) -> ChatResult:
    """POST one chat completion and collect the answer.

    `idle_s` bounds each read (0 = only the wall clock); `timeout_s` bounds
    the whole consult. `holder` is the consult's Cancellation: its `stream`
    gets this request's StreamState, so the heartbeat can describe it and
    `cancel` can close it.
    """
    started = time.monotonic()
    read_timeout = idle_s or timeout_s
    state = StreamState(phase="waiting for the first token")
    if holder is not None:
        holder.stream = state
        if holder.cancelled:
            raise Cancelled()
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=_headers(key), method="POST")
    resp = _open(req, min(read_timeout, timeout_s))
    state.response = resp
    headers = _kept(resp.headers)
    kind = (resp.headers.get("Content-Type") or "").lower()
    try:
        if "text/event-stream" not in kind:
            return _whole(resp, headers)
        return _events(resp, headers, state, holder, started, timeout_s,
                       read_timeout)
    except (socket.timeout, TimeoutError) as exc:
        if holder is not None and holder.cancelled:
            raise Cancelled() from exc
        raise Silent(read_timeout, round(time.monotonic() - started)) from exc
    except (OSError, ValueError, AttributeError) as exc:
        # A socket shut down under a read surfaces as any of these, depending
        # on the platform and where http.client was when it happened.
        if holder is not None and holder.cancelled:
            raise Cancelled() from exc
        raise
    finally:
        state.response = None
        try:
            resp.close()
        except Exception:
            pass


def _whole(resp, headers) -> ChatResult:
    """A server that ignored `stream` and answered in one object."""
    obj = json.loads(resp.read() or b"{}")
    choice = (obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return ChatResult(text=str(message.get("content") or ""),
                      usage=obj.get("usage") or {}, headers=headers,
                      finish_reason=str(choice.get("finish_reason") or ""),
                      model=str(obj.get("model") or ""))


def _events(resp, headers, state, holder, started, timeout_s,
            read_timeout) -> ChatResult:
    parts, usage, finish, model = [], {}, "", ""
    data_lines: list = []

    def handle(payload: str) -> bool:
        nonlocal usage, finish, model
        if payload.strip() == "[DONE]":
            return True
        try:
            obj = json.loads(payload)
        except ValueError:
            return False  # a keep-alive or a vendor comment
        if not isinstance(obj, dict):
            return False
        if obj.get("error"):
            message, code = _error_message(json.dumps(obj).encode())
            raise HTTPFailure(200, message, headers, code)
        if obj.get("usage"):
            usage = obj["usage"]
        model = str(obj.get("model") or model)
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("reasoning_content") or delta.get("reasoning"):
                state.phase = "thinking"
            text = delta.get("content")
            if text:
                parts.append(text)
                state.phase = "writing"
                state.chars += len(text)
            if choice.get("finish_reason"):
                finish = str(choice["finish_reason"])
        return False

    while True:
        if holder is not None and holder.cancelled:
            raise Cancelled()
        if time.monotonic() - started > timeout_s:
            raise WallClock(timeout_s)
        raw = resp.readline()
        if not raw:
            break  # the server closed the stream
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
            continue
        if line == "" and data_lines:
            done = handle("\n".join(data_lines))
            data_lines = []
            if done:
                break
    if data_lines:
        handle("\n".join(data_lines))
    if holder is not None and holder.cancelled:
        raise Cancelled()
    return ChatResult(text="".join(parts), usage=usage, headers=headers,
                      finish_reason=finish, model=model)
