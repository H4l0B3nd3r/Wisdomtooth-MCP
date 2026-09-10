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
import os

from pathlib import Path

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


# --------------------------------------------------------------------------
# The state directory (`_state_dir`)
# --------------------------------------------------------------------------
# 0.8.0 renamed `~/.claude-advisor` to `~/.wisdomtooth`. The stored OAuth token
# lives there, so the rename must not orphan an existing install -- and the two
# directories must never both be consulted, or a login in one becomes invisible
# from the other.

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point `os.path.expanduser("~")` at a temp directory.

    All three variables are set because ntpath prefers USERPROFILE while
    posixpath reads HOME, and the suite runs on both.
    """
    for var in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(var, str(tmp_path))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    assert os.path.expanduser("~") == str(tmp_path)
    return tmp_path


def test_state_dir_is_the_new_name_on_a_fresh_machine(server, fake_home):
    assert server()._state_dir() == str(fake_home / ".wisdomtooth")


def test_state_dir_keeps_the_legacy_directory_when_it_is_the_only_one(
        server, fake_home):
    """An upgrade must not sign the user out of a token stored under the old name."""
    (fake_home / ".claude-advisor").mkdir()
    assert server()._state_dir() == str(fake_home / ".claude-advisor")


def test_state_dir_prefers_the_new_name_when_both_exist(server, fake_home):
    (fake_home / ".claude-advisor").mkdir()
    (fake_home / ".wisdomtooth").mkdir()
    assert server()._state_dir() == str(fake_home / ".wisdomtooth")


def test_every_home_path_goes_through_state_dir(server, fake_home, monkeypatch):
    """A path that calls expanduser directly would split credentials in two."""
    srv = server()
    # Undo the autouse `consult_dir` pin, so `_consult_dir()` falls back to its
    # default instead of the temp directory every other test wants.
    monkeypatch.delenv("ADVISOR_CONSULT_DIR", raising=False)
    root = srv._state_dir()
    assert srv._credentials_path().startswith(root)
    assert srv._consult_dir().startswith(root)
    assert srv._workdir().startswith(root)
    assert srv._system_prompt_file("x").startswith(root)
    assert srv.DEFAULT_CONFIG_PATH.startswith(root)


def test_state_dir_is_the_only_place_that_names_the_directory(server):
    """The guard behind the test above: one call site, so the two can't diverge.

    `_state_dir()` picking the legacy directory only helps if every reader
    honours the choice. A path that joins the name itself would send half the
    server to `~/.wisdomtooth` and half to `~/.claude-advisor`, which presents
    as an unexplained logout. Asserting on the literals rather than on
    `expanduser` because two other call sites legitimately expand `~` without
    meaning this directory: the Anthropic SDK's `~/.config/anthropic`, and the
    neutral fallback cwd for the CLI subprocess.
    """
    source = Path(server().__file__).read_text(encoding="utf-8")
    assert source.count('".wisdomtooth"') == 1
    assert source.count('".claude-advisor"') == 1
