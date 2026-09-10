"""Fitness for the actual consumer: a small local model in a coding agent.

The advisor's answer lands back in that model's context window, and the tool
schemas are charged against it on every turn -- not just when it escalates. Both
have to be controllable, because the model on the other end may have 8k of
context, not 200k.
"""

import json

import pytest


def consult(srv, **kw):
    kw.setdefault("question", "why is this broken?")
    return srv._consult(**kw)


def system_prompt_sent(fake_claude):
    """The system prompt the CLI actually received.

    Read by the fake CLI while it ran: the server deletes each consult's
    prompt file once the CLI exits.
    """
    assert fake_claude.flag_value("--system-prompt-file"), (
        "expected the system prompt to travel as a file")
    return fake_claude.last["system_prompt"]


# --------------------------------------------------------------------------
# Answer length
# --------------------------------------------------------------------------

def test_an_answer_budget_is_requested_by_default(server, fake_claude):
    """Unbounded answers can exceed a small model's whole context window."""
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv)
    assert "words" in system_prompt_sent(fake_claude).lower()


def test_the_budget_is_configurable(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_ANSWER_BUDGET="150")
    consult(srv)
    assert "150" in system_prompt_sent(fake_claude)


def test_the_budget_can_be_switched_off(server, fake_claude):
    """A big local model with a 128k window should not be throttled."""
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_ANSWER_BUDGET="0")
    consult(srv)
    prompt = system_prompt_sent(fake_claude)
    assert "Length:" not in prompt
    assert "words" not in prompt.lower()


def test_the_budget_reaches_the_api_backend_too(server, monkeypatch, no_claude):
    """Both backends must behave the same; only the enforcement differs."""
    sent = []
    srv = server(ADVISOR_BACKEND="api", ANTHROPIC_API_KEY="sk-ant-x",
                 ADVISOR_ANSWER_BUDGET="120")
    monkeypatch.setattr(srv, "_consult_api",
                        lambda system, *a, **k: sent.append(system) or "ok")
    consult(srv)
    assert "120" in sent[0]


def test_the_budget_survives_a_config_file(server, tmp_path, fake_claude):
    path = tmp_path / "advisor.json"
    path.write_text(json.dumps({"answer_budget": 90}), encoding="utf-8")
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_CONFIG=str(path))
    consult(srv)
    assert "90" in system_prompt_sent(fake_claude)


def test_the_budget_can_be_changed_at_runtime(server, fake_claude):
    """"keep answers short" should not require a config edit and a restart."""
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._configure(answer_budget=75)
    consult(srv)
    assert "75" in system_prompt_sent(fake_claude)


def test_a_nonsense_budget_is_rejected(server, fake_claude):
    with pytest.raises(ValueError):
        server()._configure(answer_budget=-5)


def test_the_budget_is_reported(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_ANSWER_BUDGET="250")
    assert "250" in srv.advisor_status()


# --------------------------------------------------------------------------
# Tool surface
# --------------------------------------------------------------------------

def test_minimal_mode_is_off_by_default(server, fake_claude):
    assert server().MINIMAL_TOOLS is False


def test_minimal_mode_is_a_supported_setting(server, fake_claude):
    assert server(ADVISOR_MINIMAL_TOOLS="1").MINIMAL_TOOLS is True


def test_the_consult_tool_still_works_in_minimal_mode(server, fake_claude):
    """Trimming the surface must not trim the actual capability."""
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_MINIMAL_TOOLS="1")
    assert "FAKE ANSWER" in consult(srv)


def test_admin_functions_remain_callable_in_minimal_mode(server, fake_claude):
    """They are hidden from the model, not removed from the server."""
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_MINIMAL_TOOLS="1")
    assert srv._model_catalogue()
    assert srv._auth_report()


# --------------------------------------------------------------------------
# Defaults sized for frequent escalation
# --------------------------------------------------------------------------

def test_the_default_model_is_the_balanced_tier(server, fake_claude):
    """A weak local model escalates often; every consult on Opus would burn a
    Pro plan's headless quota fast. Opus stays one argument away."""
    srv = server()
    assert srv.DEFAULT_MODEL == "claude-sonnet-5"


def test_the_deep_tier_is_still_reachable(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv, model="deep")
    assert fake_claude.flag_value("--model") == "opus"


def test_the_default_can_still_be_raised_to_opus(server, fake_claude):
    assert server(ADVISOR_MODEL="deep").DEFAULT_MODEL == "claude-opus-5"
