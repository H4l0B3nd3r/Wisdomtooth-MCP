"""Model tier and capability tables track the current Claude lineup."""

import pytest

CURRENT_IDS = {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"}

# Previous-generation IDs. They must still RESOLVE (an agent may pass one
# deliberately), but must not be wired in as a tier or a default.
SUPERSEDED_IDS = {"claude-sonnet-4-6", "claude-opus-4-8", "claude-opus-4-7"}


def test_tiers_point_at_current_models(server):
    srv = server()
    assert srv.MODEL_TIERS["fast"] == "claude-haiku-4-5"
    assert srv.MODEL_TIERS["balanced"] == "claude-sonnet-5"
    assert srv.MODEL_TIERS["deep"] == "claude-opus-5"
    assert set(srv.MODEL_TIERS.values()) == CURRENT_IDS


def test_no_superseded_model_is_a_tier_or_default(server):
    srv = server()
    assert not SUPERSEDED_IDS & set(srv.MODEL_TIERS.values())
    assert srv.DEFAULT_MODEL not in SUPERSEDED_IDS


def test_default_model_is_the_balanced_tier(server):
    """The caller is typically a small local model that escalates often, so
    Opus-on-every-consult would exhaust a Pro plan's headless quota. `deep`
    stays one per-call argument away."""
    assert server().DEFAULT_MODEL == "claude-sonnet-5"


@pytest.mark.parametrize("alias,expected", [
    ("fast", "claude-haiku-4-5"),
    ("balanced", "claude-sonnet-5"),
    ("deep", "claude-opus-5"),
    ("DEEP", "claude-opus-5"),
])
def test_alias_resolution(server, alias, expected):
    assert server()._resolve_model(alias) == expected


def test_full_model_id_passes_through(server):
    srv = server()
    assert srv._resolve_model("claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert srv._resolve_model("claude-fable-5-1") == "claude-fable-5-1"


def test_empty_model_uses_default(server):
    srv = server(ADVISOR_MODEL="claude-sonnet-5")
    assert srv._resolve_model("") == "claude-sonnet-5"


def test_env_default_may_itself_be_a_tier_alias(server):
    """`ADVISOR_MODEL=deep` should work as readily as a full model ID."""
    assert server(ADVISOR_MODEL="deep").DEFAULT_MODEL == "claude-opus-5"


def test_lock_ignores_per_call_model(server):
    srv = server(ADVISOR_MODEL="claude-sonnet-5", ADVISOR_LOCK="1")
    assert srv._resolve_model("deep") == "claude-sonnet-5"


def test_cli_alias_mapping_covers_every_tier(server):
    srv = server()
    assert srv.CLAUDE_CODE_ALIASES == {
        "fast": "haiku", "balanced": "sonnet", "deep": "opus"}


@pytest.mark.parametrize("given,expected", [
    ("deep", "opus"),
    ("balanced", "sonnet"),
    ("fast", "haiku"),
    ("claude-opus-5", "opus"),        # tier model ID -> CLI alias
    ("claude-sonnet-5", "sonnet"),
    ("claude-haiku-4-5", "haiku"),
    ("claude-fable-5-1", "claude-fable-5-1"),  # unknown ID passes through
])
def test_cli_model_argument(server, given, expected):
    """The CLI takes short aliases or full model names; both must survive."""
    assert server()._cli_model(given) == expected
