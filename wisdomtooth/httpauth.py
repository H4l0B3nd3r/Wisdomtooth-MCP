"""Authentication for the HTTP transport.

Anyone who can reach the port can spend the account behind it, so HTTP gets a
bearer token (ADVISOR_HTTP_TOKEN), and a bind beyond loopback without one is
refused unless the operator states that something else guards the port
(ADVISOR_HTTP_NO_AUTH=1).

`BearerAuth` is plain ASGI rather than a Starlette `BaseHTTPMiddleware`: the
MCP endpoint streams its responses, and that middleware buffers them.
"""

import hmac
from typing import Optional

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def is_loopback(host: str) -> bool:
    host = str(host).strip().lower().strip("[]")
    return host in LOOPBACK_HOSTS or host.startswith("127.")


def exposure_problem(host: str, token: str, no_auth: bool) -> Optional[str]:
    """Why serving on `host` would be unsafe, or None if it is fine."""
    if token or no_auth or is_loopback(host):
        return None
    return (f"refusing to serve HTTP on {host} without authentication: anyone "
            "who can reach this port could spend the account behind it. Set "
            "ADVISOR_HTTP_TOKEN to a long random string and have the client "
            "send `Authorization: Bearer <token>`, or bind to 127.0.0.1. If a "
            "reverse proxy or firewall already guards the port, set "
            "ADVISOR_HTTP_NO_AUTH=1.")


_REFUSAL = (b'{"error": "unauthorized", "detail": "send the header '
            b'Authorization: Bearer <ADVISOR_HTTP_TOKEN>"}')


class BearerAuth:
    """Let a request through only if it carries `Authorization: Bearer <token>`.
    """

    def __init__(self, app, token: str):
        self.app = app
        self._token = token.encode("utf-8")

    def _authorized(self, scope) -> bool:
        for name, value in scope.get("headers") or []:
            if name.lower() == b"authorization":
                scheme, _, credential = value.decode("latin-1").partition(" ")
                return (scheme.lower() == "bearer"
                        and hmac.compare_digest(credential.strip().encode(
                            "latin-1"), self._token))
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"content-type", b"application/json"),
            (b"www-authenticate", b'Bearer realm="wisdomtooth"'),
            (b"content-length", str(len(_REFUSAL)).encode()),
        ]})
        await send({"type": "http.response.body", "body": _REFUSAL})
