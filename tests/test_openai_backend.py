"""The OpenAI-compatible backend: ChatGPT, Gemini, OpenRouter, LM Studio, Ollama.

Every test talks HTTP to a real loopback server (`fake_openai`), so the request
body, the auth header and the server-sent-event parsing are all on the wire.
"""

import json
import threading
import time

import pytest

from conftest import advisors_json


def _srv(server, fake_openai, **spec):
    entry = {"provider": "openai-compatible", "base_url": fake_openai.base_url,
             "model": "fake-model-a"}
    entry.update(spec)
    return server(ADVISOR_ADVISORS_JSON=advisors_json(gpt=entry))


def test_the_request_is_a_streamed_chat_completion(server, fake_openai):
    srv = _srv(server, fake_openai)
    srv._consult(question="why?", context="ctx", advisor="gpt")
    req = fake_openai.last
    assert req["path"] == "/v1/chat/completions"
    body = req["body"]
    assert body["model"] == "fake-model-a"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    system, user = body["messages"]
    assert system["role"] == "system"
    assert "expert technical advisor" in system["content"]
    assert user["role"] == "user"
    assert "<context>" in user["content"] and "why?" in user["content"]


def test_the_system_prompt_carries_the_answer_budget(server, fake_openai):
    srv = server(ADVISOR_ANSWER_BUDGET="321",
                 ADVISOR_ADVISORS_JSON=advisors_json(gpt={
                     "provider": "openai-compatible",
                     "base_url": fake_openai.base_url, "model": "m"}))
    srv._consult(question="q", advisor="gpt")
    assert "321 words" in fake_openai.last["body"]["messages"][0]["content"]


def test_the_streamed_answer_is_reassembled(server, fake_openai):
    fake_openai.answer = "line one\n\nline two with more words"
    srv = _srv(server, fake_openai)
    answer = srv._consult(question="q", advisor="gpt")
    assert answer.startswith("line one\n\nline two with more words")


def test_a_non_streaming_json_reply_is_accepted_too(server, fake_openai):
    """Some compatible servers ignore `stream` and answer in one object."""
    fake_openai.mode = "json"
    srv = _srv(server, fake_openai)
    assert "FAKE OPENAI ANSWER" in srv._consult(question="q", advisor="gpt")


def test_reasoning_text_is_not_part_of_the_answer(server, fake_openai):
    fake_openai.reasoning = "let me think about this privately"
    srv = _srv(server, fake_openai)
    answer = srv._consult(question="q", advisor="gpt")
    assert "privately" not in answer
    assert "FAKE OPENAI ANSWER" in answer


def test_the_key_is_sent_as_a_bearer_token(server, fake_openai):
    srv = _srv(server, fake_openai, api_key="sk-abc")
    srv._consult(question="q", advisor="gpt")
    assert fake_openai.last["auth"] == "Bearer sk-abc"


def test_no_key_sends_no_authorization_header(server, fake_openai):
    srv = _srv(server, fake_openai)
    srv._consult(question="q", advisor="gpt")
    assert fake_openai.last["auth"] is None


def test_secrets_are_redacted_before_they_leave(server, fake_openai):
    srv = _srv(server, fake_openai)
    srv._consult(question="q", context="key=sk-ant-api03-" + "A" * 40,
                 advisor="gpt")
    assert "AAAAAAAAAAAA" not in json.dumps(fake_openai.last["body"])


# --------------------------------------------------------------------------
# Model, effort and max_tokens
# --------------------------------------------------------------------------

def test_a_tier_maps_to_the_advisors_own_model(server, fake_openai):
    srv = _srv(server, fake_openai, tiers={"deep": "big-model"})
    srv._consult(question="q", model="deep", advisor="gpt")
    assert fake_openai.last["body"]["model"] == "big-model"


def test_effort_is_sent_as_reasoning_effort(server, fake_openai):
    srv = _srv(server, fake_openai, efforts=["low", "medium", "high"])
    srv._consult(question="q", effort="xhigh", advisor="gpt")
    assert fake_openai.last["body"]["reasoning_effort"] == "high"


def test_no_effort_is_sent_to_a_model_without_effort_support(server,
                                                             fake_openai):
    srv = _srv(server, fake_openai)  # openai-compatible: no efforts declared
    srv._consult(question="q", effort="high", advisor="gpt")
    assert "reasoning_effort" not in fake_openai.last["body"]


def test_max_tokens_uses_the_parameter_the_provider_expects(server,
                                                            fake_openai):
    srv = _srv(server, fake_openai, max_tokens_param="max_completion_tokens",
               send_max_tokens=True)
    answer = srv._consult(question="q", max_tokens=777, advisor="gpt")
    body = fake_openai.last["body"]
    assert body["max_completion_tokens"] == 777
    assert "max_tokens" not in body
    assert "max_tokens=777" in answer


def test_the_openai_preset_uses_max_completion_tokens(server):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(gpt={"provider": "openai"}))
    assert srv._ADVISORS["gpt"].max_tokens_param == "max_completion_tokens"


def test_a_local_model_gets_no_max_tokens_unless_asked(server, fake_openai):
    srv = _srv(server, fake_openai)
    srv._consult(question="q", advisor="gpt")
    body = fake_openai.last["body"]
    assert "max_tokens" not in body and "max_completion_tokens" not in body


def test_an_unsupported_optional_parameter_is_dropped_and_retried(
        server, fake_openai):
    """Compatible servers differ in what they accept; a 400 about an optional
    field must cost one retry, not the consult."""
    fake_openai.reject_params = ("reasoning_effort",)
    srv = _srv(server, fake_openai, efforts=["low", "high"])
    answer = srv._consult(question="q", effort="high", advisor="gpt")
    assert "FAKE OPENAI ANSWER" in answer
    chats = fake_openai.chats()
    assert len(chats) == 2
    assert "reasoning_effort" in chats[0]["body"]
    assert "reasoning_effort" not in chats[1]["body"]


# --------------------------------------------------------------------------
# Footer, ledger and usage
# --------------------------------------------------------------------------

def test_the_footer_names_the_advisor_and_the_account(server, fake_openai):
    srv = _srv(server, fake_openai)
    answer = srv._consult(question="q", advisor="gpt")
    assert "[advisor: gpt/fake-model-a" in answer
    assert "billed to" in answer


def test_usage_is_read_from_the_final_chunk(server, fake_openai, usage_file):
    srv = _srv(server, fake_openai)
    answer = srv._consult(question="q", advisor="gpt")
    record = json.loads(usage_file.read_text().splitlines()[-1])
    assert record["advisor"] == "gpt"
    assert record["status"] == "ok"
    assert record["input_tokens"] == 800        # prompt minus cached
    assert record["cache_read_tokens"] == 100
    assert record["output_tokens"] == 250
    assert "900 tokens in · 250 out" in answer  # the footer counts cache hits


def test_configured_prices_give_a_cost_estimate(server, fake_openai,
                                                usage_file):
    srv = _srv(server, fake_openai, prices=[2.0, 8.0])
    srv._consult(question="q", advisor="gpt")
    record = json.loads(usage_file.read_text().splitlines()[-1])
    # 800 in at $2 + 100 cached at $0.2 + 250 out at $8, per million.
    assert record["cost_usd"] == pytest.approx(0.00362, abs=1e-7)


def test_a_local_model_costs_nothing(server, fake_openai, usage_file):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(local={
        "provider": "lmstudio", "base_url": fake_openai.base_url,
        "model": "m"}))
    srv._consult(question="q", advisor="local")
    record = json.loads(usage_file.read_text().splitlines()[-1])
    assert record["cost_usd"] == 0


def test_the_repeat_guard_is_per_advisor(server, fake_claude, fake_openai):
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(gpt={
                     "provider": "openai-compatible",
                     "base_url": fake_openai.base_url, "model": "m"}))
    srv._consult(question="same", advisor="gpt")
    second = srv._consult(question="same", advisor="gpt")
    assert "[repeat:" in second
    assert len(fake_openai.chats()) == 1
    assert "[repeat:" not in srv._consult(question="same")  # claude: fresh


# --------------------------------------------------------------------------
# Failures the agent must be able to act on
# --------------------------------------------------------------------------

def test_a_rejected_key_is_an_auth_error_with_the_fix(server, fake_openai,
                                                      usage_file):
    fake_openai.mode = "auth"
    srv = _srv(server, fake_openai)
    with pytest.raises(srv.AdvisorError) as exc:
        srv._consult(question="q", advisor="gpt")
    text = str(exc.value)
    assert "gpt" in text and "key" in text.lower()
    assert "advisor_connect" in text
    record = json.loads(usage_file.read_text().splitlines()[-1])
    assert record["status"] == "error"


def test_an_exhausted_quota_is_a_usage_limit_not_a_retry(server, fake_openai):
    fake_openai.mode = "quota"
    srv = _srv(server, fake_openai)
    with pytest.raises(srv.UsageLimitError) as exc:
        srv._consult(question="q", advisor="gpt")
    assert "quota" in str(exc.value).lower()
    assert "do not retry" in str(exc.value).lower()


def test_a_rate_limit_says_when_to_come_back(server, fake_openai):
    fake_openai.mode = "ratelimit"
    srv = _srv(server, fake_openai)
    with pytest.raises(srv.AdvisorError) as exc:
        srv._consult(question="q", advisor="gpt")
    assert "7" in str(exc.value)  # retry-after


def test_an_unknown_model_is_reported_as_such(server, fake_openai):
    fake_openai.mode = "not_found"
    srv = _srv(server, fake_openai)
    with pytest.raises(srv.AdvisorError) as exc:
        srv._consult(question="q", advisor="gpt")
    assert "fake-model-a" in str(exc.value)


def test_a_server_error_carries_the_status_and_message(server, fake_openai):
    fake_openai.mode = "500"
    srv = _srv(server, fake_openai)
    with pytest.raises(srv.AdvisorError) as exc:
        srv._consult(question="q", advisor="gpt")
    assert "500" in str(exc.value) and "upstream exploded" in str(exc.value)


def test_an_endpoint_that_is_down_names_the_url(server):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(local={
        "provider": "lmstudio", "base_url": "http://127.0.0.1:9/v1",
        "model": "m"}))
    with pytest.raises(srv.AdvisorError) as exc:
        srv._consult(question="q", advisor="local")
    assert "127.0.0.1:9" in str(exc.value)
    assert "running" in str(exc.value).lower()


def test_a_silent_endpoint_is_stopped_by_the_idle_limit(server, fake_openai):
    fake_openai.mode = "hang"
    srv = server(ADVISOR_IDLE_TIMEOUT="1.5",
                 ADVISOR_ADVISORS_JSON=advisors_json(gpt={
                     "provider": "openai-compatible",
                     "base_url": fake_openai.base_url, "model": "m"}))
    started = time.monotonic()
    with pytest.raises(srv.AdvisorError) as exc:
        srv._consult(question="q", advisor="gpt")
    assert time.monotonic() - started < 20
    assert "silent" in str(exc.value).lower()


def test_a_cancelled_consult_closes_the_stream(server, fake_openai):
    fake_openai.answer = " ".join(["word"] * 400)
    fake_openai.tick = 0.05
    srv = _srv(server, fake_openai)
    holder = srv._Cancellation()
    result = {}

    def run():
        token = srv._CANCELLATION.set(holder)
        try:
            srv._consult(question="q", advisor="gpt")
        except Exception as exc:  # noqa: BLE001 - the outcome is the point
            result["error"] = exc
        finally:
            srv._CANCELLATION.reset(token)

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(1.0)
    holder.cancel()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert "cancelled" in str(result.get("error", "")).lower()


def test_progress_notes_describe_the_http_stream(server, fake_openai):
    fake_openai.answer = " ".join(["word"] * 60)
    fake_openai.tick = 0.02
    srv = _srv(server, fake_openai)
    holder = srv._Cancellation()
    notes = []
    token = srv._CANCELLATION.set(holder)
    try:
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                if holder.note:
                    notes.append(holder.note)
                time.sleep(0.05)

        watcher = threading.Thread(target=watch)
        watcher.start()
        srv._consult(question="q", advisor="gpt")
        stop.set()
        watcher.join()
    finally:
        srv._CANCELLATION.reset(token)
    assert any("writing" in n for n in notes)


def test_an_inline_think_block_is_not_part_of_the_answer(server, fake_openai,
                                                         consult_dir):
    """Found in the real e2e: LM Studio returns Qwen's reasoning inline, as a
    leading <think>...</think> in `content`, not as reasoning_content."""
    fake_openai.answer = "<think>my private plan\n\nstep two</think>Final answer here."
    srv = _srv(server, fake_openai)
    answer = srv._consult(question="q", advisor="gpt")
    assert answer.startswith("Final answer here.")
    assert "private plan" not in answer
    saved = next(consult_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "private plan" not in saved


def test_text_that_merely_mentions_think_tags_is_kept(server, fake_openai):
    fake_openai.answer = "Use a <think> tag only in prompts, never in output."
    srv = _srv(server, fake_openai)
    assert "<think> tag" in srv._consult(question="q", advisor="gpt")


def test_an_unknown_model_error_lists_what_the_endpoint_offers(server,
                                                               fake_openai):
    """Provider model IDs go out of date; the error should name the ones that
    exist, so the fix is one advisor_connect away."""
    fake_openai.mode = "not_found"
    fake_openai.models = ["fake-model-b", "fake-model-c"]
    srv = _srv(server, fake_openai)
    with pytest.raises(srv.AdvisorError) as exc:
        srv._consult(question="q", advisor="gpt")
    assert "fake-model-b" in str(exc.value)
    assert "fake-model-c" in str(exc.value)


def test_https_uses_the_operating_systems_trust_store(monkeypatch, tmp_path):
    """A python.org Python on macOS ships no CA bundle, and corporate proxies
    add their own CA to the OS store; the Anthropic SDK trusts the OS store
    for both reasons, so the other advisors must too."""
    import ssl
    import truststore
    from wisdomtooth import openai_compat
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    assert isinstance(openai_compat.ssl_context(), truststore.SSLContext)
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))  # an explicit choice wins
    ctx = openai_compat.ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert not isinstance(ctx, truststore.SSLContext)
