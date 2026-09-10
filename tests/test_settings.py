"""Settings are one frozen object, loaded without importing the server.

`wisdomtooth.config.load_settings` takes the environment and config file as
arguments, so a test can build any configuration directly; the server module
binds its constants from the object it loads at import.
"""

from dataclasses import FrozenInstanceError

import pytest

from wisdomtooth.config import CONFIG_KEYS, load_settings


def test_settings_are_one_frozen_object():
    settings = load_settings(environ={}, file_config={})
    with pytest.raises(FrozenInstanceError):
        settings.timeout = 1


def test_defaults():
    s = load_settings(environ={}, file_config={})
    assert s.preset == "medium"
    assert s.backend == "auto"
    assert s.answer_budget == 2000
    assert s.timeout == 300
    assert s.idle_timeout == 300
    assert s.http_port == 8484
    assert s.trim_answers is True
    assert s.http_token == ""


def test_environment_beats_file_beats_preset_beats_default():
    s = load_settings(environ={"ADVISOR_PRESET": "small"},
                      file_config={"answer_budget": 900})
    assert s.answer_budget == 900       # file beats preset
    assert s.minimal_tools is True      # preset beats default
    s = load_settings(environ={"ADVISOR_ANSWER_BUDGET": "50"},
                      file_config={"answer_budget": 900})
    assert s.answer_budget == 50        # environment beats file


def test_a_malformed_number_falls_back_with_a_warning():
    warnings = []
    s = load_settings(environ={"ADVISOR_TIMEOUT": "five minutes",
                               "ADVISOR_PORT": "80a"},
                      file_config={}, warn=warnings.append)
    assert s.timeout == 300
    assert s.http_port == 8484
    assert any("ADVISOR_TIMEOUT" in w for w in warnings)
    assert any("ADVISOR_PORT" in w for w in warnings)


def test_the_server_still_starts_with_a_malformed_number(server):
    """A typo in one env var used to crash the import, so the MCP client saw
    only a server that would not connect."""
    srv = server(ADVISOR_TIMEOUT="abc", ADVISOR_MAX_USD_PER_DAY="lots")
    assert srv.TIMEOUT == 300
    assert srv.MAX_USD_PER_DAY == 0


def test_unknown_values_are_reported():
    warnings = []
    s = load_settings(environ={"ADVISOR_PRESET": "tiny",
                               "ADVISOR_EFFORT": "ultra"},
                      file_config={}, warn=warnings.append)
    assert s.preset == "medium"
    assert s.default_effort is None
    assert any("ADVISOR_PRESET" in w for w in warnings)
    assert any("ADVISOR_EFFORT" in w for w in warnings)


def test_the_timeout_cap_never_undercuts_the_base():
    s = load_settings(environ={"ADVISOR_TIMEOUT": "900",
                               "ADVISOR_TIMEOUT_MAX": "60"}, file_config={})
    assert s.timeout_max == 900


def test_the_server_binds_its_constants_from_the_settings(server):
    srv = server(ADVISOR_TIMEOUT="77", ADVISOR_IDLE_TIMEOUT="45")
    assert srv.SETTINGS.timeout == srv.TIMEOUT == 77
    assert srv.SETTINGS.idle_timeout == srv.IDLE_TIMEOUT == 45


def test_the_new_settings_can_live_in_the_config_file():
    for key in ("idle_timeout", "http_token", "http_no_auth", "trim_answers"):
        assert key in CONFIG_KEYS
    s = load_settings(environ={}, file_config={"idle_timeout": 90,
                                               "trim_answers": False})
    assert s.idle_timeout == 90
    assert s.trim_answers is False
