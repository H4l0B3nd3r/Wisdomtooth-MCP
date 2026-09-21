"""multi_advisor: one call, up to three advisors, in parallel.

Two uses: the same question to several advisors, to compare their answers, and
a different question to each, to play to each model's strengths (a technical
question to Claude, a UI/UX one to Gemini, a codebase review to ChatGPT).
"""

import json
import time

import pytest

from conftest import advisors_json


def _two_fakes(fake_openai):
    """Two OpenAI-compatible advisors on the same fake endpoint, told apart
    by model name."""
    base = fake_openai.base_url
    return advisors_json(
        gpt={"provider": "openai-compatible", "base_url": base,
             "model": "gpt-fake"},
        gem={"provider": "openai-compatible", "base_url": base,
             "model": "gem-fake"})


@pytest.fixture
def three(server, fake_claude, fake_openai):
    """claude (fake CLI) + gpt + gem (fake HTTP endpoint)."""
    return server(ADVISOR_BACKEND="claude-code",
                  ADVISOR_ADVISORS_JSON=_two_fakes(fake_openai))


def _models_asked(fake_openai):
    return sorted(r["body"]["model"] for r in fake_openai.chats())


def test_the_same_question_goes_to_every_named_advisor(three, fake_claude,
                                                       fake_openai):
    text = three._multi(question="which queue?", context="ctx",
                          attempts_so_far="tried redis",
                          advisors=["claude", "gpt", "gem"])
    assert _models_asked(fake_openai) == ["gem-fake", "gpt-fake"]
    assert "which queue?" in fake_claude.last["prompt"]
    for chat in fake_openai.chats():
        user = chat["body"]["messages"][1]["content"]
        assert "which queue?" in user and "tried redis" in user
    # One section per advisor, in the order asked.
    assert text.index("claude") < text.index("gpt") < text.index("gem")
    assert "FAKE ANSWER" in text and text.count("FAKE OPENAI ANSWER") == 2


def test_targeted_questions_send_each_advisor_its_own(three, fake_claude,
                                                      fake_openai):
    text = three._multi(context="shared ctx", targeted_questions={
        "claude": "TECHNICAL: is this lock-free queue correct?",
        "gem": "UIUX: is this settings page clear?",
        "gpt": "REVIEW: review the whole module thoroughly"})
    assert "TECHNICAL" in fake_claude.last["prompt"]
    assert "UIUX" not in fake_claude.last["prompt"]
    by_model = {c["body"]["model"]: c["body"]["messages"][1]["content"]
                for c in fake_openai.chats()}
    assert "UIUX" in by_model["gem-fake"] and "REVIEW" not in by_model["gem-fake"]
    assert "REVIEW" in by_model["gpt-fake"]
    assert all("shared ctx" in v for v in by_model.values())
    # Each section shows the question that advisor was asked.
    assert "UIUX: is this settings page clear?" in text


def test_a_targeted_question_overrides_the_shared_one_for_that_advisor(
        three, fake_claude, fake_openai):
    three._multi(question="SHARED", advisors=["claude", "gpt"],
                   targeted_questions={"gpt": "ONLY-GPT"})
    assert "SHARED" in fake_claude.last["prompt"]
    gpt = fake_openai.chats()[0]["body"]["messages"][1]["content"]
    assert "ONLY-GPT" in gpt and "SHARED" not in gpt


def test_the_advisors_run_in_parallel(server, fake_claude, fake_openai):
    fake_openai.delay = 2.0
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=_two_fakes(fake_openai))
    started = time.monotonic()
    srv._multi(question="q", advisors=["gpt", "gem"])
    assert time.monotonic() - started < 3.5  # two 2s requests, not 4s


def test_advisor_colon_model_picks_a_model_per_advisor(three, fake_openai):
    three._multi(question="q", advisors=["gpt:deep-id", "gem"])
    assert _models_asked(fake_openai) == ["deep-id", "gem-fake"]


def test_more_than_three_advisors_is_refused_before_anything_is_sent(
        server, fake_openai):
    base = fake_openai.base_url
    srv = server(ADVISOR_ADVISORS_JSON=advisors_json(**{
        n: {"provider": "openai-compatible", "base_url": base, "model": n}
        for n in ("a", "b", "c", "d")}))
    with pytest.raises(srv.AdvisorInputError) as exc:
        srv._multi(question="q", advisors=["a", "b", "c", "d"])
    assert "3" in str(exc.value)
    assert fake_openai.chats() == []


def test_one_advisor_is_not_a_comparison(three, fake_openai):
    with pytest.raises(three.AdvisorInputError) as exc:
        three._multi(question="q", advisors=["gpt"])
    assert "ask_wisdomtooth" in str(exc.value)


def test_duplicates_collapse(three, fake_openai):
    with pytest.raises(three.AdvisorInputError):
        three._multi(question="q", advisors=["gpt", "GPT", "gpt"])


def test_when_only_claude_is_connected_the_error_says_how_to_add_one(
        server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(srv.AdvisorInputError) as exc:
        srv._multi(question="q", advisors=["claude", "gemini"])
    assert "advisor_connect" in str(exc.value)
    assert "gemini" in str(exc.value)


def test_an_unready_advisor_is_refused_up_front(server, fake_claude,
                                                fake_openai):
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(
                     gpt={"provider": "openai"},  # no key
                     gem={"provider": "openai-compatible",
                          "base_url": fake_openai.base_url, "model": "g"}))
    calls = len(fake_claude.calls())
    with pytest.raises(srv.AdvisorError) as exc:
        srv._multi(question="q", advisors=["claude", "gpt", "gem"])
    assert "OPENAI_API_KEY" in str(exc.value)
    assert fake_openai.chats() == []
    assert len(fake_claude.calls()) == calls


def test_no_question_for_an_advisor_is_an_input_error(three):
    with pytest.raises(three.AdvisorInputError):
        three._multi(advisors=["claude", "gpt"])


def test_one_failure_does_not_cost_the_other_answers(server, fake_claude,
                                                     fake_openai):
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(
                     gpt={"provider": "openai-compatible",
                          "base_url": fake_openai.base_url, "model": "g"},
                     dead={"provider": "openai-compatible",
                           "base_url": "http://127.0.0.1:9/v1", "model": "d"}))
    text = srv._multi(question="q", advisors=["claude", "gpt", "dead"])
    assert "FAKE ANSWER" in text and "FAKE OPENAI ANSWER" in text
    assert "dead" in text and "FAILED" in text
    assert "127.0.0.1:9" in text


def test_when_every_advisor_fails_it_is_an_error(server, fake_claude,
                                                 monkeypatch):
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_ADVISORS_JSON=advisors_json(
                     dead={"provider": "openai-compatible",
                           "base_url": "http://127.0.0.1:9/v1", "model": "d"}))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    with pytest.raises(srv.AdvisorError) as exc:
        srv._multi(question="q", advisors=["claude", "dead"])
    assert "claude" in str(exc.value) and "dead" in str(exc.value)


def test_each_answer_is_saved_and_linked(three, consult_dir, usage_file):
    saved: dict = {}
    text = three._multi(question="q", advisors=["claude", "gpt"],
                          saved=saved)
    assert len(saved["paths"]) == 2
    for path in saved["paths"]:
        assert path in text or path.replace("\\", "/") in text.replace("\\", "/")
    records = [json.loads(line) for line in usage_file.read_text().splitlines()]
    assert sorted(r["advisor"] for r in records) == ["claude", "gpt"]
    assert {r["kind"] for r in records} == {"multi_advisor"}


def test_the_result_tells_the_agent_how_to_use_it(three):
    same = three._multi(question="q", advisors=["claude", "gpt"])
    assert "agree" in same.lower()  # guidance on weighing agreement
    targeted = three._multi(targeted_questions={"claude": "a", "gpt": "b"})
    assert "targeted" in targeted.lower()


async def test_the_tool_returns_text_and_one_link_per_answer(three):
    blocks = await three.multi_advisor(question="q",
                                         advisors=["claude", "gpt", "gem"])
    kinds = [b.type for b in blocks]
    assert kinds[0] == "text"
    assert kinds.count("resource_link") == 3


def test_status_suggests_multi_advisor_when_several_are_ready(three):
    assert "multi_advisor" in three._status_report()


def test_a_shared_tier_gives_each_advisor_its_own_model(three, fake_openai):
    three._multi(question="q", advisors=["gpt", "gem"], model="fast")
    assert _models_asked(fake_openai) == ["gem-fake", "gpt-fake"]


def _under_holder(srv, holder, fn):
    import threading
    result = {}

    def run():
        token = srv._CANCELLATION.set(holder)
        try:
            result["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - the outcome is the point
            result["error"] = exc
        finally:
            srv._CANCELLATION.reset(token)
    worker = threading.Thread(target=run)
    worker.start()
    return worker, result


def test_the_heartbeat_describes_every_advisor(three, fake_openai):
    fake_openai.answer = " ".join(["word"] * 80)
    fake_openai.tick = 0.03
    holder = three._Cancellation()
    worker, _ = _under_holder(three, holder, lambda: three._multi(
        question="q", advisors=["gpt", "gem"]))
    notes = []
    while worker.is_alive():
        if holder.note:
            notes.append(holder.note)
        time.sleep(0.05)
    assert any("gpt:" in n and "gem:" in n for n in notes), notes[-3:]


def test_cancelling_stops_every_advisor(three, fake_openai):
    fake_openai.answer = " ".join(["word"] * 400)
    fake_openai.tick = 0.05
    holder = three._Cancellation()
    worker, result = _under_holder(three, holder, lambda: three._multi(
        question="q", advisors=["gpt", "gem"]))
    time.sleep(1.0)
    holder.cancel()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert "cancelled" in str(result.get("error", "")).lower()
