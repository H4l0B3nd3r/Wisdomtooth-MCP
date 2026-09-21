"""A fake OpenAI-compatible endpoint, served for real on loopback.

The OpenAI-compatible backend is exercised over the wire -- request body, auth
header, server-sent events, the trailing usage chunk, rate-limit headers --
exactly as it talks to OpenAI, Gemini, OpenRouter, LM Studio or Ollama.
Nothing inside the server under test is mocked.
"""

import http.server
import json
import threading
import time


class FakeOpenAI:
    """State shared with the request handler; tests set attributes on it."""

    def __init__(self):
        self.requests: list = []
        self.answer = "FAKE OPENAI ANSWER"
        self.reasoning = ""           # streamed first, as delta.reasoning_content
        # ok | auth | quota | ratelimit | 500 | hang | json | not_found
        self.mode = "ok"
        self.required_key = ""        # when set, any other key gets a 401
        self.reject_params: tuple = ()  # a 400 while the body carries any of these
        self.delay = 0.0              # seconds before the response starts
        self.tick = 0.0               # seconds between streamed words
        self.usage = {"prompt_tokens": 900, "completion_tokens": 250,
                      "prompt_tokens_details": {"cached_tokens": 100}}
        self.headers = {"x-ratelimit-limit-tokens": "30000",
                        "x-ratelimit-remaining-tokens": "29000",
                        "x-ratelimit-reset-tokens": "2s"}
        self.key_info = None          # served at GET <base>/key (OpenRouter)
        self.models = ["fake-model-a", "fake-model-b"]
        self.base_url = ""

    @property
    def last(self) -> dict:
        assert self.requests, "the fake OpenAI endpoint was never called"
        return self.requests[-1]

    def chats(self) -> list:
        return [r for r in self.requests
                if r["path"].endswith("/chat/completions")]


def _handler(state: FakeOpenAI):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _json(self, status, obj, extra=None):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            if not state.required_key:
                return True
            if self.headers.get("Authorization") == "Bearer " + state.required_key:
                return True
            self._json(401, {"error": {"message": "Incorrect API key provided",
                                       "type": "invalid_request_error",
                                       "code": "invalid_api_key"}})
            return False

        def do_GET(self):
            state.requests.append({"path": self.path, "method": "GET",
                                   "auth": self.headers.get("Authorization")})
            if not self._authorized():
                return
            if self.path.endswith("/models"):
                self._json(200, {"object": "list", "data": [
                    {"id": m, "object": "model"} for m in state.models]})
            elif self.path.endswith("/key") and state.key_info is not None:
                self._json(200, {"data": state.key_info})
            else:
                self._json(404, {"error": {"message": "not found"}})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            state.requests.append({"path": self.path, "method": "POST",
                                   "auth": self.headers.get("Authorization"),
                                   "body": body})
            if not self._authorized():
                return
            if state.delay:
                time.sleep(state.delay)
            mode = state.mode
            if mode == "auth":
                return self._json(401, {"error": {"message": "bad key"}})
            if mode == "quota":
                return self._json(429, {"error": {
                    "message": "You exceeded your current quota, please check "
                               "your plan and billing details.",
                    "type": "insufficient_quota", "code": "insufficient_quota"}})
            if mode == "ratelimit":
                return self._json(429, {"error": {
                    "message": "Rate limit reached for requests",
                    "type": "requests", "code": "rate_limit_exceeded"}},
                    {"retry-after": "7"})
            if mode == "500":
                return self._json(500, {"error": {"message": "upstream exploded"}})
            if mode == "not_found":
                return self._json(404, {"error": {
                    "message": f"The model `{body.get('model')}` does not exist"}})
            bad = [p for p in state.reject_params if p in body]
            if bad:
                return self._json(400, {"error": {
                    "message": f"Unsupported parameter: '{bad[0]}' is not "
                               "supported with this model.",
                    "param": bad[0]}})
            if mode == "json" or not body.get("stream"):
                return self._json(200, {
                    "id": "x", "object": "chat.completion",
                    "model": body.get("model"),
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant",
                                             "content": state.answer}}],
                    "usage": state.usage}, state.headers)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            for key, value in state.headers.items():
                self.send_header(key, value)
            self.end_headers()

            def send(obj):
                self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
                self.wfile.flush()

            def chunk(delta, finish=None):
                send({"id": "x", "object": "chat.completion.chunk",
                      "model": body.get("model"),
                      "choices": [{"index": 0, "delta": delta,
                                   "finish_reason": finish}]})

            try:
                chunk({"role": "assistant", "content": ""})
                if state.reasoning:
                    chunk({"reasoning_content": state.reasoning})
                if mode == "hang":
                    time.sleep(600)
                for i, word in enumerate(state.answer.split(" ")):
                    chunk({"content": ("" if i == 0 else " ") + word})
                    if state.tick:
                        time.sleep(state.tick)
                chunk({}, finish="stop")
                if (body.get("stream_options") or {}).get("include_usage"):
                    send({"id": "x", "object": "chat.completion.chunk",
                          "choices": [], "usage": state.usage})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except OSError:
                pass  # the client hung up, e.g. a cancelled consult
            self.close_connection = True

    return Handler


def start() -> tuple:
    """Serve a FakeOpenAI on an ephemeral loopback port; return (state, stop)."""
    state = FakeOpenAI()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state.base_url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"

    def stop():
        httpd.shutdown()
        httpd.server_close()
    return state, stop
