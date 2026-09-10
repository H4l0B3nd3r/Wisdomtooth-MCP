"""An answer far over the budget is trimmed for the caller and kept whole on
disk.

The budget is only a request to the model -- the CLI has no token cap -- so an
answer can overshoot it badly, and every extra word lands in a small caller's
context. The server can cap what the caller receives: it returns the lead and
points to the saved transcript, which keeps the whole answer.
"""

import json
from pathlib import Path


def use_answer(srv, answer):
    srv.register_backend(srv.Backend(
        name="echo", provider="test", billing="TEST ACCOUNT",
        consult=lambda *args: answer, available=lambda: True))


def words(n, word="alpha"):
    return " ".join([word] * n)


def paragraphs(count=20, per=100):
    return "\n\n".join(f"Paragraph {i}. {words(per)} END{i}."
                       for i in range(count))


def body(result):
    return result.split("\n\n---\n[advisor:", 1)[0]


def test_an_answer_far_over_the_budget_is_trimmed(server):
    srv = server(ADVISOR_BACKEND="echo", ADVISOR_ANSWER_BUDGET="300")
    use_answer(srv, paragraphs())
    saved = {}
    result = srv._consult(question="q", saved=saved)
    kept = body(result)
    assert "Paragraph 0." in kept
    assert "Paragraph 19." not in kept
    assert len(kept.split()) <= 300 + 80  # the lead plus the trim note
    assert "[trimmed" in kept
    assert "saved" in kept
    full = Path(saved["path"]).read_text(encoding="utf-8")
    assert "Paragraph 19." in full


def test_the_trim_ends_on_a_paragraph_boundary(server):
    srv = server(ADVISOR_BACKEND="echo", ADVISOR_ANSWER_BUDGET="300")
    use_answer(srv, paragraphs())
    lead = body(srv._consult(question="q")).split("\n\n[trimmed", 1)[0]
    assert lead.rstrip().split()[-1].startswith("END")


def test_a_trim_never_leaves_a_code_fence_open(server):
    code = "\n".join(f"    value_{i} = compute({i})" for i in range(400))
    answer = "Use this:\n\n```python\n" + code + "\n```\n\n" + words(300)
    srv = server(ADVISOR_BACKEND="echo", ADVISOR_ANSWER_BUDGET="100")
    use_answer(srv, answer)
    kept = body(srv._consult(question="q"))
    assert "[trimmed" in kept
    assert kept.count("```") % 2 == 0


def test_a_modest_overshoot_is_returned_whole(server):
    srv = server(ADVISOR_BACKEND="echo", ADVISOR_ANSWER_BUDGET="300")
    use_answer(srv, words(400))
    assert "[trimmed" not in srv._consult(question="q")


def test_nothing_is_trimmed_without_a_saved_transcript(server):
    """Trimming is only safe when the rest can be read somewhere."""
    srv = server(ADVISOR_BACKEND="echo", ADVISOR_ANSWER_BUDGET="300",
                 ADVISOR_SAVE_CONSULTS="0")
    use_answer(srv, paragraphs())
    result = srv._consult(question="q")
    assert "[trimmed" not in result
    assert "Paragraph 19." in result


def test_trimming_can_be_turned_off(server):
    srv = server(ADVISOR_BACKEND="echo", ADVISOR_ANSWER_BUDGET="300",
                 ADVISOR_TRIM_ANSWERS="0")
    use_answer(srv, paragraphs())
    assert "Paragraph 19." in srv._consult(question="q")


def test_an_unlimited_budget_is_never_trimmed(server):
    srv = server(ADVISOR_BACKEND="echo", ADVISOR_ANSWER_BUDGET="0")
    use_answer(srv, paragraphs(count=200))
    assert "Paragraph 199." in srv._consult(question="q")


def test_a_repeat_returns_the_same_trimmed_answer(server):
    srv = server(ADVISOR_BACKEND="echo", ADVISOR_ANSWER_BUDGET="300")
    use_answer(srv, paragraphs())
    first = srv._consult(question="q")
    again = srv._consult(question="q")
    assert "[repeat" in again
    assert body(again) == body(first)


def test_the_ledger_records_the_trim(server, usage_file):
    srv = server(ADVISOR_BACKEND="echo", ADVISOR_ANSWER_BUDGET="300")
    answer = paragraphs()
    use_answer(srv, answer)
    srv._consult(question="q")
    [record] = [json.loads(line) for line in
                usage_file.read_text(encoding="utf-8").splitlines()]
    assert record["answer_chars"] == len(answer)
    assert 0 < record["trimmed_to_words"] <= 300


def test_a_tiny_budget_still_returns_some_of_the_answer(server):
    srv = server(ADVISOR_BACKEND="echo", ADVISOR_ANSWER_BUDGET="1")
    use_answer(srv, words(50))
    lead = body(srv._consult(question="q")).split("\n\n[trimmed", 1)[0]
    assert lead.strip()
