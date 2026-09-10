"""Effort levels, including `xhigh`, and per-model clamping."""

import pytest


def test_xhigh_is_a_valid_level(server):
    """`xhigh` shipped with Opus 4.7 and is the sweet spot for agentic work."""
    assert "xhigh" in server().VALID_EFFORT


def test_valid_effort_is_the_full_current_set(server):
    assert set(server().VALID_EFFORT) == {"low", "medium", "high", "xhigh", "max"}


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_each_level_resolves(server, level):
    assert server()._resolve_effort(level) == level


def test_effort_is_case_insensitive(server):
    assert server()._resolve_effort("XHigh") == "xhigh"


def test_unknown_effort_falls_back_to_default(server):
    srv = server(ADVISOR_EFFORT="high")
    assert srv._resolve_effort("turbo") == "high"


def test_empty_effort_uses_env_default(server):
    assert server(ADVISOR_EFFORT="max")._resolve_effort("") == "max"


def test_no_env_default_means_api_default(server):
    assert server()._resolve_effort("") is None


def test_lock_ignores_per_call_effort(server):
    srv = server(ADVISOR_EFFORT="low", ADVISOR_LOCK="1")
    assert srv._resolve_effort("max") == "low"


def test_invalid_env_default_is_rejected_not_forwarded(server):
    """A typo in ADVISOR_EFFORT must not be sent to the API as-is."""
    assert server(ADVISOR_EFFORT="ludicrous")._resolve_effort("") is None
