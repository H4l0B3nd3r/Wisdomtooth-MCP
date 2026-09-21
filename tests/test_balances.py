"""Account monitoring, and the one case where a consult waits for the user.

What each account has left comes from whichever source is real for it:

- the Claude subscription's own meter -- Claude Code streams a
  `rate_limit_event` with the 5-hour and 7-day utilization;
- a credit balance the provider reports (OpenRouter's /key);
- rate-limit headers (OpenAI and most hosted APIs), shown but never gating --
  they refill within the minute;
- an allowance the user declared, measured against the local ledger.

A consult is held for the user's confirmation IF AND ONLY IF its estimated
cost exceeds what the account has left. A low balance that still covers the
request only earns a warning line.
"""

import json
import time

import pytest

from conftest import advisors_json


def _limit(five=0.07, seven=0.18, status="allowed", resets_in=3600):
    now = int(time.time())
    return json.dumps({
        "status": status, "resetsAt": now + resets_in,
        "rateLimitType": "five_hour", "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": five, "resetsAt": now + resets_in},
            "seven_day": {"utilization": seven,
                          "resetsAt": now + 5 * 86400}}})


def _seed_ledger(usage_file, advisor, tokens, age_s=60):
    record = {"ts": time.time() - age_s, "time": "t", "kind": "consult",
              "backend": "x", "model": "m", "status": "ok",
              "input_tokens": tokens, "output_tokens": 0, "prompt_chars": 1}
    if advisor:
        record["advisor"] = advisor
    with open(usage_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# --------------------------------------------------------------------------
# The Claude plan's own meter
# --------------------------------------------------------------------------

def test_the_plan_meter_is_captured_from_the_cli_stream(server, fake_claude,
                                                        monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RATE_LIMIT", _limit(0.07, 0.18))
    srv = server(ADVISOR_BACKEND="claude-code")
    answer = srv._consult(question="q")
    assert "5 h 7% used" in answer and "7 d 18% used" in answer
    status = srv._status_report()
    assert "7% used" in status and "18% used" in status


def test_the_plan_meter_survives_a_restart(server, fake_claude, monkeypatch,
                                           accounts_file):
    monkeypatch.setenv("FAKE_CLAUDE_RATE_LIMIT", _limit(0.42, 0.5))
    server(ADVISOR_BACKEND="claude-code")._consult(question="q")
    assert accounts_file.exists()
    monkeypatch.delenv("FAKE_CLAUDE_RATE_LIMIT")
    assert "42% used" in server(ADVISOR_BACKEND="claude-code")._status_report()


def test_a_window_that_has_reset_is_not_reported_as_used(server, fake_claude,
                                                         monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RATE_LIMIT", _limit(0.9, 0.2,
                                                        resets_in=-10))
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._consult(question="q")
    assert "90% used" not in srv._status_report()


def test_the_meter_learns_what_a_percent_costs(server, fake_claude,
                                               monkeypatch):
    """Two consults, 0.01 USD each (the fake CLI's figure), moving the 5-hour
    meter from 10% to 12%: two points for a cent, so 88 points left are about
    $0.44 of API-equivalent usage."""
    srv = server(ADVISOR_BACKEND="claude-code")
    monkeypatch.setenv("FAKE_CLAUDE_RATE_LIMIT", _limit(0.10, 0.10))
    srv._consult(question="first")
    monkeypatch.setenv("FAKE_CLAUDE_RATE_LIMIT", _limit(0.12, 0.11))
    srv._consult(question="second")
    left = srv._METER.plan_remaining_usd("claude")
    assert left == pytest.approx(0.44, rel=0.05)


# --------------------------------------------------------------------------
# Holding a consult that would go over
# --------------------------------------------------------------------------

def test_a_consult_over_the_declared_allowance_is_held(server, fake_claude,
                                                       usage_file):
    _seed_ledger(usage_file, "claude", 4800)
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(claude={
                     "allowance_tokens": 5000, "allowance_window": "5h"}))
    before = len(fake_claude.calls())
    with pytest.raises(srv.OverLimitError) as exc:
        srv._consult(question="q", context="x" * 4000)
    text = str(exc.value)
    assert "confirm_over_limit" in text
    assert "nothing was sent" in text.lower()
    assert "5,000" in text and "200" in text  # the allowance and what is left
    assert "ask the user" in text.lower()
    # No prompt reached the CLI (the only calls allowed are probes).
    assert all(not c["prompt"] for c in fake_claude.calls()[before:])
    record = json.loads(usage_file.read_text().splitlines()[-1])
    assert record["status"] == "held"


def test_confirming_sends_the_held_consult(server, fake_claude, usage_file):
    _seed_ledger(usage_file, "claude", 4800)
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(claude={
                     "allowance_tokens": 5000, "allowance_window": "5h"}))
    answer = srv._consult(question="q", confirm_over_limit=True)
    assert "FAKE ANSWER" in answer
    assert "over" in answer.lower()  # the footer still says it went over


def test_a_low_balance_that_still_covers_the_consult_only_warns(
        server, fake_claude, usage_file):
    _seed_ledger(usage_file, "claude", 90000)
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(claude={
                     "allowance_tokens": 100000, "allowance_window": "day"}))
    answer = srv._consult(question="q")
    assert "FAKE ANSWER" in answer
    assert "low" in answer.lower()
    # 100k allowance, 90k used before and ~1.5k by this consult.
    assert "8% left" in answer


def test_usage_outside_the_window_does_not_count(server, fake_claude,
                                                 usage_file):
    _seed_ledger(usage_file, "claude", 4900, age_s=6 * 3600)
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(claude={
                     "allowance_tokens": 5000, "allowance_window": "5h"}))
    assert "FAKE ANSWER" in srv._consult(question="q")


def test_ledger_records_from_before_advisors_count_as_claude(
        server, fake_claude, usage_file):
    _seed_ledger(usage_file, None, 4900)  # a 0.10.0 record: no `advisor`
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(claude={
                     "allowance_tokens": 5000, "allowance_window": "5h"}))
    with pytest.raises(srv.OverLimitError):
        srv._consult(question="q")


def test_an_exhausted_plan_window_holds_the_consult(server, fake_claude,
                                                    monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RATE_LIMIT", _limit(1.0, 0.6,
                                                        status="rejected"))
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._consult(question="first")  # this one reports the full meter
    with pytest.raises(srv.OverLimitError) as exc:
        srv._consult(question="second")
    assert "5 h" in str(exc.value) and "100%" in str(exc.value)


def test_a_busy_plan_without_a_learned_rate_is_not_held(server, fake_claude,
                                                        monkeypatch):
    """Without a measured cost per percent the server cannot say a consult
    would go over, and it must not guess: holding needs evidence."""
    monkeypatch.setenv("FAKE_CLAUDE_RATE_LIMIT", _limit(0.95, 0.5))
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._consult(question="first")
    assert "FAKE ANSWER" in srv._consult(question="second")


def test_a_plan_near_its_limit_holds_a_consult_that_would_exceed_it(
        server, fake_claude, monkeypatch):
    srv = server(ADVISOR_BACKEND="claude-code")
    # Learn ~1 point per cent (0.01 USD per consult on the fake CLI).
    monkeypatch.setenv("FAKE_CLAUDE_RATE_LIMIT", _limit(0.97, 0.5))
    srv._consult(question="a")
    monkeypatch.setenv("FAKE_CLAUDE_RATE_LIMIT", _limit(0.995, 0.5))
    srv._consult(question="b")
    # 0.5 points left is about $0.002; a 60k-character consult on Opus costs
    # far more than that.
    with pytest.raises(srv.OverLimitError):
        srv._consult(question="c", context="y" * 60000, model="deep")


def test_an_openrouter_credit_balance_is_checked(server, fake_openai):
    fake_openai.key_info = {"limit": 10.0, "limit_remaining": 0.0001,
                            "usage": 9.9999, "limit_reset": None}
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(orr={
        "provider": "openrouter", "base_url": fake_openai.base_url,
        "api_key": "k", "model": "some/model", "prices": [3.0, 15.0]}))
    with pytest.raises(srv.OverLimitError) as exc:
        srv._consult(question="q", context="z" * 8000, advisor="orr")
    assert "$0.0001" in str(exc.value) or "credit" in str(exc.value).lower()
    assert fake_openai.chats() == []


def test_an_ample_openrouter_balance_is_shown(server, fake_openai,
                                              fake_claude):
    fake_openai.key_info = {"limit": 10.0, "limit_remaining": 5.0,
                            "usage": 5.0, "limit_reset": None}
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(orr={
        "provider": "openrouter", "base_url": fake_openai.base_url,
        "api_key": "k", "model": "some/model", "prices": [3.0, 15.0]}))
    answer = srv._consult(question="q", advisor="orr")
    assert "FAKE OPENAI ANSWER" in answer
    assert "$5.00" in srv._status_report()


def test_rate_limit_headers_are_reported_but_never_hold(server, fake_openai,
                                                         fake_claude):
    fake_openai.headers = {"x-ratelimit-limit-tokens": "30000",
                           "x-ratelimit-remaining-tokens": "10",
                           "x-ratelimit-reset-tokens": "2s"}
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(gpt={
        "provider": "openai-compatible", "base_url": fake_openai.base_url,
        "model": "m"}))
    srv._consult(question="q", advisor="gpt")
    # 10 tokens/min left would never cover the next one, but it refills.
    assert "FAKE OPENAI ANSWER" in srv._consult(question="q2", advisor="gpt")
    assert "10 of 30,000 tokens/min" in srv._status_report()


def test_compare_holds_everything_when_one_account_would_go_over(
        server, fake_claude, fake_openai, usage_file):
    _seed_ledger(usage_file, "gpt", 990)
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(gpt={
                     "provider": "openai-compatible",
                     "base_url": fake_openai.base_url, "model": "m",
                     "allowance_tokens": 1000, "allowance_window": "day"}))
    before = len(fake_claude.calls())
    with pytest.raises(srv.OverLimitError) as exc:
        srv._multi(question="q", advisors=["claude", "gpt"])
    assert "gpt" in str(exc.value)
    assert fake_openai.chats() == []
    assert all(not c["prompt"] for c in fake_claude.calls()[before:])
    text = srv._multi(question="q", advisors=["claude", "gpt"],
                        confirm_over_limit=True)
    assert "FAKE OPENAI ANSWER" in text


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------

def test_usage_report_breaks_down_by_advisor_and_shows_balances(
        server, fake_claude, fake_openai, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RATE_LIMIT", _limit(0.3, 0.4))
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(gpt={
                     "provider": "openai-compatible",
                     "base_url": fake_openai.base_url, "model": "m",
                     "allowance_tokens": 1000000,
                     "allowance_window": "month"}))
    srv._consult(question="q")
    srv._consult(question="q", advisor="gpt")
    report = srv._usage_report()
    assert "BY ADVISOR" in report
    assert "claude" in report and "gpt" in report
    assert "BALANCES" in report
    assert "30% used" in report
    assert "of 1,000,000" in report


def test_the_estimate_grows_with_the_request(server):
    srv = server()
    spec = srv._ADVISORS["claude"]
    small = srv._estimate_tokens(spec, "sys", "q", 0, None)
    big = srv._estimate_tokens(spec, "sys", "q" * 40000, 0, None)
    hard = srv._estimate_tokens(spec, "sys", "q", 0, "max")
    assert big > small + 9000
    assert hard > small
