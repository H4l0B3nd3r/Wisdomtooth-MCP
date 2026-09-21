"""Choosing the advisor: Claude by default, or another provider the user set up.

An advisor is a named, configured model the server can consult -- Claude
through the existing subscription/API backends, or anything that speaks the
OpenAI chat-completions protocol: ChatGPT, Gemini, OpenRouter, or a local model
in LM Studio or Ollama. Claude stays the default unless the user says
otherwise.
"""

import json

import pytest

from conftest import advisors_json


# --------------------------------------------------------------------------
# Defaults and parsing
# --------------------------------------------------------------------------

def test_claude_is_the_only_and_default_advisor_out_of_the_box(server):
    srv = server()
    assert list(srv._ADVISORS) == ["claude"]
    assert srv._default_advisor() == "claude"


def test_advisors_come_from_the_environment(server):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(
        gpt={"provider": "openai", "api_key": "sk-test"},
        gem={"provider": "gemini", "api_key": "g-test"}))
    assert set(srv._ADVISORS) == {"claude", "gpt", "gem"}
    assert srv._default_advisor() == "claude"  # adding one never moves the default


def test_advisors_come_from_the_config_file(server, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"advisors": {
        "local": {"provider": "lmstudio", "model": "google/gemma-4-12b"}}}))
    srv = server(ADVISOR_CONFIG=str(cfg))
    assert "local" in srv._ADVISORS
    assert srv._ADVISORS["local"].base_url == "http://localhost:1234/v1"


def test_a_provider_preset_fills_in_the_endpoint_key_variable_and_tiers(server):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(
        gpt={"provider": "openai"}, gem={"provider": "gemini"}))
    gpt, gem = srv._ADVISORS["gpt"], srv._ADVISORS["gem"]
    assert gpt.base_url == "https://api.openai.com/v1"
    assert gpt.api_key_env == "OPENAI_API_KEY"
    assert set(gpt.tiers) == {"fast", "balanced", "deep"}
    assert gem.base_url.startswith("https://generativelanguage.googleapis.com/")
    assert gem.api_key_env == "GEMINI_API_KEY"


def test_explicit_fields_beat_the_preset(server):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(gpt={
        "provider": "openai", "base_url": "https://proxy.example/v1/",
        "model": "my-model", "tiers": {"deep": "my-deep-model"}}))
    gpt = srv._ADVISORS["gpt"]
    assert gpt.base_url == "https://proxy.example/v1"  # trailing slash dropped
    assert gpt.tiers["deep"] == "my-deep-model"
    assert gpt.tiers["fast"]  # the other preset tiers survive
    assert srv._advisor_model(gpt, "") == "my-model"


def test_a_broken_advisor_entry_is_skipped_with_a_warning_not_a_crash(
        server, capsys):
    srv = server(ADVISOR_ADVISORS_JSON=json.dumps({
        "ok": {"provider": "ollama", "model": "qwen3"},
        "nope": {"provider": "no-such-provider"},
        "Bad Name!": {"provider": "ollama", "model": "x"},
        "custom": {"provider": "openai-compatible"},  # no base_url or model
    }))
    assert set(srv._ADVISORS) == {"claude", "ok"}
    err = capsys.readouterr().err
    assert "no-such-provider" in err
    assert "Bad Name!" in err
    assert "custom" in err


def test_unparseable_advisor_json_leaves_claude_working(server, capsys):
    srv = server(ADVISOR_ADVISORS_JSON="{not json")
    assert list(srv._ADVISORS) == ["claude"]
    assert "ADVISOR_ADVISORS_JSON" in capsys.readouterr().err


def test_the_claude_entry_can_carry_an_allowance_but_not_a_provider(server):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(claude={
        "allowance_tokens": 500000, "allowance_window": "5h",
        "notes": "best at hard debugging"}))
    claude = srv._ADVISORS["claude"]
    assert claude.kind == "claude"
    assert claude.allowance_tokens == 500000
    assert claude.allowance_window == "5h"
    assert claude.notes == "best at hard debugging"


def test_the_default_advisor_is_configurable(server):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(
        gem={"provider": "gemini", "api_key": "k"}),
        ADVISOR_DEFAULT_ADVISOR="gem")
    assert srv._default_advisor() == "gem"


def test_an_unknown_default_advisor_falls_back_to_claude(server, capsys):
    srv = server(ADVISOR_DEFAULT_ADVISOR="ghost")
    assert srv._default_advisor() == "claude"
    assert "ghost" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Models, tiers and effort per advisor
# --------------------------------------------------------------------------

def test_tiers_resolve_per_advisor(server):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(gpt={
        "provider": "openai", "tiers": {"fast": "f", "balanced": "b",
                                        "deep": "d"}}))
    gpt = srv._ADVISORS["gpt"]
    assert srv._advisor_model(gpt, "deep") == "d"
    assert srv._advisor_model(gpt, "") == "b"   # the preset default is balanced
    assert srv._advisor_model(gpt, "exact-id") == "exact-id"


def test_effort_is_clamped_to_what_the_provider_accepts(server):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(
        gem={"provider": "gemini"}, gpt={"provider": "openai"},
        local={"provider": "ollama", "model": "m"}))
    assert srv._advisor_effort(srv._ADVISORS["gem"], "max") == "high"
    assert srv._advisor_effort(srv._ADVISORS["gpt"], "xhigh") == "xhigh"
    assert srv._advisor_effort(srv._ADVISORS["local"], "high") is None
    assert srv._advisor_effort(srv._ADVISORS["gpt"], None) is None


# --------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------

def test_a_hosted_advisor_without_a_key_is_not_ready(server):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(gpt={"provider": "openai"}))
    ready, why = srv._advisor_ready("gpt")
    assert not ready
    assert "OPENAI_API_KEY" in why


def test_a_key_in_the_named_environment_variable_makes_it_ready(
        server, monkeypatch):
    monkeypatch.setenv("MY_GPT_KEY", "sk-live")
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(gpt={
        "provider": "openai", "api_key_env": "MY_GPT_KEY"}))
    assert srv._advisor_ready("gpt") == (True, "ready")
    assert srv._advisor_key(srv._ADVISORS["gpt"]) == "sk-live"


def test_a_local_advisor_needs_no_key(server):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(
        local={"provider": "lmstudio", "model": "google/gemma-4-12b"}))
    assert srv._advisor_ready("local")[0] is True


# --------------------------------------------------------------------------
# Routing a consult
# --------------------------------------------------------------------------

def test_the_default_consult_still_goes_to_claude(server, fake_claude,
                                                  fake_openai):
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(gpt={
                     "provider": "openai-compatible",
                     "base_url": fake_openai.base_url, "model": "m"}))
    answer = srv._consult(question="q")
    assert "FAKE ANSWER" in answer
    assert fake_openai.chats() == []


def test_advisor_argument_routes_the_consult(server, fake_claude, fake_openai):
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(gpt={
                     "provider": "openai-compatible",
                     "base_url": fake_openai.base_url, "model": "m"}))
    calls_before = len(fake_claude.calls())
    answer = srv._consult(question="q", advisor="gpt")
    assert "FAKE OPENAI ANSWER" in answer
    assert "[advisor: gpt/m" in answer
    assert len(fake_claude.calls()) == calls_before


def test_a_configured_default_advisor_takes_the_plain_consult(server,
                                                              fake_openai):
    srv = server(ADVISOR_DEFAULT_ADVISOR="local",
                 ADVISOR_ADVISORS_JSON=advisors_json(local={
                     "provider": "openai-compatible",
                     "base_url": fake_openai.base_url, "model": "m"}))
    assert "FAKE OPENAI ANSWER" in srv._consult(question="q")


def test_an_unknown_advisor_is_an_input_error_naming_the_choices(server):
    srv = server()
    with pytest.raises(srv.AdvisorInputError) as exc:
        srv._consult(question="q", advisor="gemini")
    assert "claude" in str(exc.value)
    assert "advisor_connect" in str(exc.value)


def test_an_unready_advisor_says_what_is_missing(server):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(gpt={"provider": "openai"}))
    with pytest.raises(srv.AdvisorError) as exc:
        srv._consult(question="q", advisor="gpt")
    assert "OPENAI_API_KEY" in str(exc.value)


# --------------------------------------------------------------------------
# advisor_connect / advisor_disconnect / advisor_configure
# --------------------------------------------------------------------------

def test_connect_verifies_and_persists_an_advisor(server, fake_openai,
                                                  advisors_file):
    fake_openai.required_key = "sk-good"
    srv = server()
    text = srv._connect_advisor("gpt", "openai-compatible", api_key="sk-good",
                                base_url=fake_openai.base_url,
                                model="fake-model-a")
    assert "connected" in text.lower()
    assert "fake-model-a" in text
    assert "sk-good" not in text  # a credential is never echoed back
    assert "gpt" in srv._ADVISORS
    stored = json.loads(advisors_file.read_text())
    assert stored["advisors"]["gpt"]["api_key"] == "sk-good"
    # A fresh process sees it too.
    assert "gpt" in server()._ADVISORS


def test_connect_refuses_a_key_the_endpoint_rejects(server, fake_openai,
                                                    advisors_file):
    fake_openai.required_key = "sk-good"
    srv = server()
    with pytest.raises(srv.AdvisorError) as exc:
        srv._connect_advisor("gpt", "openai-compatible", api_key="sk-bad",
                             base_url=fake_openai.base_url, model="m")
    assert "key" in str(exc.value).lower()
    assert not advisors_file.exists()


def test_connect_names_a_model_the_endpoint_does_not_list(server, fake_openai):
    srv = server()
    text = srv._connect_advisor("gpt", "openai-compatible",
                                base_url=fake_openai.base_url, model="typo-model")
    assert "typo-model" in text and "fake-model-a" in text


def test_connect_rejects_a_bad_name_or_provider(server):
    srv = server()
    with pytest.raises(srv.AdvisorInputError):
        srv._connect_advisor("Bad Name", "openai", api_key="k")
    with pytest.raises(srv.AdvisorInputError) as exc:
        srv._connect_advisor("x", "grok-ish", api_key="k")
    assert "openai" in str(exc.value) and "gemini" in str(exc.value)
    with pytest.raises(srv.AdvisorInputError):
        srv._connect_advisor("claude", "openai", api_key="k")


def test_connect_can_make_the_new_advisor_the_default(server, fake_openai):
    srv = server()
    srv._connect_advisor("local", "openai-compatible",
                         base_url=fake_openai.base_url, model="fake-model-a",
                         make_default=True)
    assert srv._default_advisor() == "local"
    assert server()._default_advisor() == "local"  # persisted


def test_disconnect_forgets_the_advisor_and_its_key(server, fake_openai,
                                                    advisors_file):
    srv = server()
    srv._connect_advisor("gpt", "openai-compatible", api_key="sk-x",
                         base_url=fake_openai.base_url, model="fake-model-a",
                         make_default=True)
    text = srv._disconnect_advisor("gpt")
    assert "gpt" in text
    assert "gpt" not in srv._ADVISORS
    assert srv._default_advisor() == "claude"
    assert "sk-x" not in advisors_file.read_text()


def test_configure_switches_the_default_advisor(server, fake_openai,
                                                 fake_claude):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(local={
        "provider": "openai-compatible", "base_url": fake_openai.base_url,
        "model": "m"}))
    text = srv._configure(advisor="local")
    assert "default advisor: local" in text
    assert srv._default_advisor() == "local"
    with pytest.raises(srv.AdvisorInputError):
        srv._configure(advisor="ghost")


def test_configure_model_applies_to_the_default_advisor(server, fake_openai,
                                                         fake_claude):
    srv = server(ADVISOR_DEFAULT_ADVISOR="local",
                 ADVISOR_ADVISORS_JSON=advisors_json(local={
                     "provider": "openai-compatible",
                     "base_url": fake_openai.base_url, "model": "m"}))
    srv._configure(model="other-local-model")  # not a claude- id: fine here
    srv._consult(question="q")
    assert fake_openai.last["body"]["model"] == "other-local-model"


def test_status_lists_every_advisor_without_leaking_keys(server, fake_openai,
                                                          fake_claude):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(
        gpt={"provider": "openai", "api_key": "sk-secret-123"},
        gem={"provider": "gemini"}))
    status = srv._status_report()
    assert "default advisor: claude" in status
    assert "gpt" in status and "gem" in status
    assert "GEMINI_API_KEY" in status  # why gem is not ready
    assert "sk-secret-123" not in status


def test_models_catalogue_shows_each_advisors_tiers(server, fake_claude):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(gpt={
        "provider": "openai", "tiers": {"deep": "gpt-deep-id"}}))
    assert "gpt-deep-id" in srv._model_catalogue()


# --------------------------------------------------------------------------
# compare_approaches stays a single-advisor tool; fan-out is multi_advisor
# --------------------------------------------------------------------------

async def test_compare_approaches_asks_one_advisor(server, fake_openai):
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(gpt={
        "provider": "openai-compatible", "base_url": fake_openai.base_url,
        "model": "m"}))
    blocks = await srv.compare_approaches(problem="queue", options="a\nb",
                                          advisor="gpt")
    assert "FAKE OPENAI ANSWER" in blocks[0].text
    assert len(fake_openai.chats()) == 1


def test_compare_approaches_takes_no_advisor_list():
    import inspect
    import wisdomtooth.server as srv
    params = inspect.signature(srv.compare_approaches).parameters
    assert "advisor" in params
    assert "advisors" not in params and "targeted_questions" not in params


def test_a_tier_the_advisor_does_not_define_uses_its_default_model(server):
    """Found in the real e2e: model="fast" reached LM Studio as the model
    name "fast". An advisor without tiers must answer with its own model."""
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(
        local={"provider": "lmstudio", "model": "qwen/qwen3.8-27b"},
        gpt={"provider": "openai"}))
    local = srv._ADVISORS["local"]
    for tier in ("fast", "balanced", "deep", "DEEP"):
        assert srv._advisor_model(local, tier) == "qwen/qwen3.8-27b"
    assert srv._advisor_model(local, "other-model") == "other-model"
    assert srv._advisor_model(srv._ADVISORS["gpt"], "fast") == "gpt-5.6-luna"
