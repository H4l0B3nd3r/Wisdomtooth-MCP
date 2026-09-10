"""User configurability.

Five layers, highest priority first:
  1. per-call tool arguments
  2. runtime overrides set with `advisor_configure`
  3. environment variables
  4. a JSON config file
  5. built-in defaults
`ADVISOR_LOCK=1` freezes layers 3-5 and rejects 1-2.
"""

import json

import pytest


@pytest.fixture
def config_file(tmp_path):
    def _write(**values):
        path = tmp_path / "advisor.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        return str(path)
    return _write


# --------------------------------------------------------------------------
# Config file
# --------------------------------------------------------------------------

def test_config_file_sets_defaults(server, config_file):
    srv = server(ADVISOR_CONFIG=config_file(
        model="balanced", effort="low", max_tokens=4096))
    assert srv.DEFAULT_MODEL == "claude-sonnet-5"
    assert srv.DEFAULT_EFFORT == "low"
    assert srv.MAX_TOKENS == 4096


def test_environment_beats_the_config_file(server, config_file):
    """The MCP client's per-server env is the more specific setting."""
    srv = server(ADVISOR_CONFIG=config_file(model="fast"),
                 ADVISOR_MODEL="deep")
    assert srv.DEFAULT_MODEL == "claude-opus-5"


def test_config_file_may_set_the_backend(server, config_file, fake_claude):
    srv = server(ADVISOR_CONFIG=config_file(backend="api"))
    assert srv.BACKEND == "api"


def test_unknown_config_keys_are_reported_not_fatal(server, config_file):
    srv = server(ADVISOR_CONFIG=config_file(model="fast", wibble=1))
    assert srv.DEFAULT_MODEL == "claude-haiku-4-5"


def test_a_malformed_config_file_does_not_stop_the_server(server, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{oops", encoding="utf-8")
    assert server(ADVISOR_CONFIG=str(path)).DEFAULT_MODEL == "claude-sonnet-5"


def test_a_missing_config_file_is_not_an_error(server, tmp_path):
    assert server(ADVISOR_CONFIG=str(tmp_path / "nope.json")).MAX_TOKENS == 16000


# --------------------------------------------------------------------------
# Custom model tiers
# --------------------------------------------------------------------------

def test_tiers_can_be_remapped(server):
    """Lets a user point `deep` at Fable, or pin a tier to a dated snapshot."""
    srv = server(ADVISOR_TIERS_JSON=json.dumps({"deep": "claude-fable-5-1"}))
    assert srv.MODEL_TIERS["deep"] == "claude-fable-5-1"
    assert srv._resolve_model("deep") == "claude-fable-5-1"


def test_remapping_one_tier_leaves_the_others(server):
    srv = server(ADVISOR_TIERS_JSON=json.dumps({"fast": "claude-haiku-4-5"}))
    assert srv.MODEL_TIERS["balanced"] == "claude-sonnet-5"


def test_extra_tiers_can_be_added(server):
    srv = server(ADVISOR_TIERS_JSON=json.dumps({"cheapest": "claude-haiku-4-5"}))
    assert srv._resolve_model("cheapest") == "claude-haiku-4-5"


# --------------------------------------------------------------------------
# Custom advisor persona
# --------------------------------------------------------------------------

def test_system_prompt_can_be_replaced(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_SYSTEM_PROMPT="You are a laconic Rust reviewer.")
    assert "laconic Rust reviewer" in srv.ADVISOR_SYSTEM_PROMPT


def test_system_prompt_can_come_from_a_file(server, tmp_path):
    path = tmp_path / "persona.txt"
    path.write_text("Answer only in bullet points.", encoding="utf-8")
    srv = server(ADVISOR_SYSTEM_PROMPT_FILE=str(path))
    assert "bullet points" in srv.ADVISOR_SYSTEM_PROMPT


def test_extra_guidance_is_appended_to_the_builtin_prompt(server):
    srv = server(ADVISOR_SYSTEM_PROMPT_EXTRA="Always cite the crate version.")
    assert "expert technical advisor" in srv.ADVISOR_SYSTEM_PROMPT
    assert "cite the crate version" in srv.ADVISOR_SYSTEM_PROMPT


# --------------------------------------------------------------------------
# Per-call token cap
# --------------------------------------------------------------------------

def test_max_tokens_can_be_set_per_call(server):
    assert server()._build_kwargs("claude-opus-5", "low",
                                  max_tokens=5000)["max_tokens"] == 5000


def test_per_call_max_tokens_wins_over_the_env_default(server):
    srv = server(ADVISOR_MAX_TOKENS="16000")
    assert srv._build_kwargs("claude-opus-5", "low",
                             max_tokens=2000)["max_tokens"] == 2000


def test_zero_means_use_the_configured_default(server):
    srv = server(ADVISOR_MAX_TOKENS="12345")
    assert srv._build_kwargs("claude-opus-5", "low",
                             max_tokens=0)["max_tokens"] == 12345


def test_absurd_per_call_max_tokens_is_clamped(server):
    """128k is the current ceiling; a larger value is a 400, not a big answer."""
    assert server()._build_kwargs("claude-opus-5", "low",
                                  max_tokens=10_000_000)["max_tokens"] <= 128000


def test_locked_ignores_per_call_max_tokens(server):
    srv = server(ADVISOR_MAX_TOKENS="8000", ADVISOR_LOCK="1")
    assert srv._build_kwargs("claude-opus-5", "low",
                             max_tokens=64000)["max_tokens"] == 8000


# --------------------------------------------------------------------------
# Runtime reconfiguration
# --------------------------------------------------------------------------

def test_configure_changes_the_default_model(server, fake_claude):
    """Editing the client's env needs a server restart; this does not."""
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._configure(model="fast")
    srv._consult(question="q")
    assert fake_claude.flag_value("--model") == "haiku"


def test_configure_changes_effort_and_tokens(server):
    srv = server()
    srv._configure(effort="max", max_tokens=20000)
    assert srv._resolve_effort("") == "max"
    assert srv._build_kwargs("claude-opus-5", None)["max_tokens"] == 20000


def test_configure_reports_the_effective_settings(server):
    srv = server()
    report = srv._configure(model="balanced")
    assert "claude-sonnet-5" in report


def test_configure_rejects_an_unknown_model_tier(server):
    srv = server()
    with pytest.raises(ValueError):
        srv._configure(effort="ludicrous")


def test_per_call_arguments_still_beat_runtime_overrides(server):
    srv = server()
    srv._configure(model="fast")
    assert srv._resolve_model("deep") == "claude-opus-5"


def test_configure_is_refused_when_locked(server):
    srv = server(ADVISOR_LOCK="1")
    with pytest.raises(RuntimeError) as exc:
        srv._configure(model="deep")
    assert "ADVISOR_LOCK" in str(exc.value)


def test_configure_can_reset_to_the_startup_configuration(server):
    srv = server(ADVISOR_MODEL="balanced")
    srv._configure(model="fast")
    srv._configure(reset=True)
    assert srv._resolve_model("") == "claude-sonnet-5"


def test_configure_leaves_untouched_settings_alone(server):
    srv = server(ADVISOR_EFFORT="high")
    srv._configure(model="fast")
    assert srv._resolve_effort("") == "high"


# --------------------------------------------------------------------------
# Capability discovery
# --------------------------------------------------------------------------

def test_model_catalogue_lists_tiers_and_effort_support(server):
    text = server()._model_catalogue()
    for expected in ("fast", "balanced", "deep", "claude-opus-5", "xhigh"):
        assert expected in text


def test_model_catalogue_says_which_models_reject_effort(server):
    text = server()._model_catalogue()
    caps = text.split("MODEL CAPABILITIES")[1]
    haiku_line = next(l for l in caps.splitlines() if "claude-haiku-4-5" in l)
    assert "none" in haiku_line.lower()
