"""Request shaping for the API backend.

The rules encoded here come from the current Messages API:
  * Fable 5.x / Opus 5 / 4.8 / 4.7 / 4.6, Sonnet 5 / 4.6 -> thinking
    {"type": "adaptive"}; `budget_tokens` is rejected with a 400.
  * `output_config.effort` accepts low|medium|high|xhigh|max, and lives inside
    `output_config` -- never at the top level.
  * Opus 4.5 has effort but only low|medium|high.
  * Haiku 4.5 rejects `effort` entirely.
"""

import pytest

ADAPTIVE = ["claude-opus-5", "claude-sonnet-5", "claude-opus-4-8",
            "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
            "claude-fable-5-1", "claude-fable-5"]


@pytest.mark.parametrize("model", ADAPTIVE)
def test_adaptive_models_get_adaptive_thinking(server, model):
    kwargs = server()._build_kwargs(model, "high")
    assert kwargs["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize("model", ADAPTIVE)
def test_adaptive_models_never_get_budget_tokens(server, model):
    """`budget_tokens` is a 400 on every model in this list."""
    assert "budget_tokens" not in str(server()._build_kwargs(model, "high"))


def test_effort_goes_inside_output_config(server):
    kwargs = server()._build_kwargs("claude-opus-5", "xhigh")
    assert kwargs["output_config"] == {"effort": "xhigh"}
    assert "effort" not in kwargs


def test_haiku_gets_neither_effort_nor_thinking(server):
    kwargs = server()._build_kwargs("claude-haiku-4-5", "high")
    assert "output_config" not in kwargs
    assert "thinking" not in kwargs


def test_opus_4_5_effort_is_clamped_to_high(server):
    """Opus 4.5 predates xhigh/max; sending them is a 400."""
    assert server()._build_kwargs("claude-opus-4-5", "max")["output_config"] == {
        "effort": "high"}


def test_opus_4_5_gets_no_adaptive_thinking(server):
    assert "thinking" not in server()._build_kwargs("claude-opus-4-5", "high")


def test_unknown_model_is_treated_conservatively(server):
    """A model released after this table was written must not 400 the call."""
    kwargs = server()._build_kwargs("claude-something-9", "max")
    assert "thinking" not in kwargs
    assert "output_config" not in kwargs


def test_no_effort_means_no_output_config(server):
    assert "output_config" not in server()._build_kwargs("claude-opus-5", None)


def test_default_max_tokens_leaves_room_for_a_real_answer(server):
    """8192 was the old default; thinking tokens eat it on adaptive models."""
    assert server()._build_kwargs("claude-opus-5", None)["max_tokens"] >= 16000


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_high_effort_raises_the_token_ceiling(server, effort):
    base = server()._build_kwargs("claude-opus-5", "low")["max_tokens"]
    raised = server()._build_kwargs("claude-opus-5", effort)["max_tokens"]
    assert raised > base


def test_explicit_max_tokens_env_is_never_lowered(server):
    srv = server(ADVISOR_MAX_TOKENS="60000")
    assert srv._build_kwargs("claude-opus-5", "low")["max_tokens"] == 60000


def test_model_and_max_tokens_always_present(server):
    kwargs = server()._build_kwargs("claude-opus-5", "high")
    assert kwargs["model"] == "claude-opus-5"
    assert isinstance(kwargs["max_tokens"], int)


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5", True),
    ("claude-fable-5-1", True),
    ("claude-sonnet-5", False),
    ("claude-haiku-4-5", False),
])
def test_refusal_fallbacks_only_on_models_that_support_them(server, model, expected):
    assert server()._supports_fallbacks(model) is expected
