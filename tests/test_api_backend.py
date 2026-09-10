"""The API backend, exercised against a stubbed Anthropic client.

No network, no key, no spend -- these assert the request shape and the error
translation, which is where the drift against the current Messages API lives.
"""

import types

import anthropic
import pytest


class FakeBlock:
    def __init__(self, text, type="text"):
        self.type = type
        self.text = text


class FakeMessage:
    def __init__(self, text="stub answer", stop_reason="end_turn",
                 stop_details=None, model="claude-opus-5"):
        self.content = [FakeBlock(text)] if text else []
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        self.model = model
        self.usage = types.SimpleNamespace(input_tokens=1, output_tokens=1)


class FakeStream:
    def __init__(self, recorder, message, kwargs):
        self._recorder = recorder
        self._message = message
        self._kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class FakeMessages:
    def __init__(self, recorder, message=None, raises=None):
        self._recorder = recorder
        self._message = message or FakeMessage()
        self._raises = raises

    def stream(self, **kwargs):
        self._recorder.append(kwargs)
        if self._raises:
            exc, self._raises = self._raises, None
            raise exc
        return FakeStream(self._recorder, self._message, kwargs)


class FakeClient:
    def __init__(self, recorder, message=None, raises=None, beta_raises=None):
        self.messages = FakeMessages(recorder, message, raises)
        self.beta = types.SimpleNamespace(
            messages=FakeMessages(recorder, message, beta_raises))


@pytest.fixture
def api(server, monkeypatch, no_claude):
    """An `api`-backend server whose Anthropic client is a stub."""
    def _make(message=None, raises=None, beta_raises=None, **env):
        env.setdefault("ADVISOR_BACKEND", "api")
        env.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
        srv = server(**env)
        recorder = []
        client = FakeClient(recorder, message, raises, beta_raises)
        monkeypatch.setattr(srv, "client", lambda: client)
        return srv, recorder
    return _make


# --------------------------------------------------------------------------
# Request shape
# --------------------------------------------------------------------------

def test_requests_are_streamed(api):
    """Non-streaming requests with a large max_tokens hit the SDK HTTP timeout;
    the SDKs require streaming above ~8k output tokens."""
    srv, recorder = api()
    srv._consult(question="q")
    assert recorder, "no request was issued"


def test_system_prompt_and_question_are_sent(api):
    srv, recorder = api()
    srv._consult(question="WHY-IS-IT-BROKEN")
    sent = recorder[-1]
    assert "advisor" in sent["system"].lower()
    assert "WHY-IS-IT-BROKEN" in sent["messages"][0]["content"]


def test_context_is_wrapped_in_a_tag(api):
    srv, recorder = api()
    srv._consult(question="q", context="CTX-BODY")
    content = recorder[-1]["messages"][0]["content"]
    assert "<context>" in content and "CTX-BODY" in content


def test_opus_5_gets_adaptive_thinking_and_effort(api):
    srv, recorder = api()
    srv._consult(question="q", model="deep", effort="xhigh")
    sent = recorder[-1]
    assert sent["model"] == "claude-opus-5"
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "xhigh"}


def test_haiku_is_sent_without_effort_or_thinking(api):
    srv, recorder = api()
    srv._consult(question="q", model="fast", effort="high")
    sent = recorder[-1]
    assert "output_config" not in sent
    assert "thinking" not in sent


def test_footer_names_the_api_account_as_the_payer(api):
    srv, _ = api()
    assert "API ACCOUNT" in srv._consult(question="q")


def test_footer_reports_the_model_that_answered(api):
    srv, _ = api()
    assert "claude-opus-5" in srv._consult(question="q", model="deep")


# --------------------------------------------------------------------------
# Refusal fallbacks
# --------------------------------------------------------------------------

def test_opus_5_requests_server_side_fallbacks(api):
    """A refused advisor call should be rescued rather than returned empty."""
    srv, recorder = api()
    srv._consult(question="q", model="deep")
    sent = recorder[-1]
    assert sent.get("fallbacks")
    assert any("server-side-fallback" in b for b in sent.get("betas", []))


def test_fallbacks_are_dropped_if_the_sdk_rejects_them(api):
    """An older anthropic SDK has no `fallbacks` kwarg; degrade, do not fail."""
    srv, recorder = api(beta_raises=TypeError("unexpected keyword 'fallbacks'"))
    assert "stub answer" in srv._consult(question="q", model="deep")
    assert "fallbacks" not in recorder[-1]


def test_sonnet_does_not_request_fallbacks(api):
    srv, recorder = api()
    srv._consult(question="q", model="balanced")
    assert "fallbacks" not in recorder[-1]


def test_a_refusal_is_reported_clearly(api):
    srv, _ = api(message=FakeMessage(
        text="", stop_reason="refusal",
        stop_details=types.SimpleNamespace(category="cyber", explanation="nope")))
    answer = srv._consult(question="q")
    assert "refus" in answer.lower()
    assert "cyber" in answer


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

def test_missing_api_key_is_explained_before_any_request(server, no_claude):
    srv = server(ADVISOR_BACKEND="api")
    with pytest.raises(RuntimeError) as exc:
        srv._consult(question="q")
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_capability_drift_retries_without_effort_and_thinking(api):
    """A model that rejects the current parameter set must still answer."""
    err = anthropic.BadRequestError.__new__(anthropic.BadRequestError)
    Exception.__init__(err, "unsupported parameter")
    srv, recorder = api(raises=err, beta_raises=err)
    assert "stub answer" in srv._consult(question="q", model="claude-opus-4-6",
                                         effort="high")
    assert "output_config" not in recorder[-1]
    assert "thinking" not in recorder[-1]


def test_empty_response_is_explained_not_returned_blank(api):
    srv, _ = api(message=FakeMessage(text="", stop_reason="max_tokens"))
    answer = srv._consult(question="q")
    assert "max_tokens" in answer or "ADVISOR_MAX_TOKENS" in answer
