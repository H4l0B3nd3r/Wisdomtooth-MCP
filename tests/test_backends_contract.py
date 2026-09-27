"""The provider seam: every backend goes through one contract.

A backend declares what it honours, and the footer, `advisor_status` and
`advisor_models` are built from those declarations -- so a third backend gets
the same treatment as the two Claude ones without touching the consult path.
"""


def test_each_backend_declares_what_it_honours(server):
    srv = server()
    api, cli = srv._BACKENDS["api"], srv._BACKENDS["claude-code"]
    assert api.honours_max_tokens is True
    assert cli.honours_max_tokens is False
    assert cli.fallback is None  # never moves the user onto paid credits
    assert api.fallback is None


def test_a_registered_backend_gets_the_same_footer(server):
    srv = server(ADVISOR_BACKEND="echo")
    srv.register_backend(srv.Backend(
        name="echo", provider="test", billing="TEST ACCOUNT (per call)",
        billed_to="TEST", consult=lambda *args: "ok",
        available=lambda: True, honours_max_tokens=True,
        label=lambda model: "echo/" + model))
    answer = srv._consult(question="q", max_tokens=123, effort="high")
    assert "[advisor: echo/" in answer
    assert "billed to TEST ·" in answer
    assert "effort=high" in answer
    assert "max_tokens=123" in answer


def test_a_backend_that_ignores_max_tokens_says_so(server):
    srv = server(ADVISOR_BACKEND="echo")
    srv.register_backend(srv.Backend(
        name="echo", provider="test", billing="TEST ACCOUNT",
        consult=lambda *args: "ok", available=lambda: True))
    assert "max_tokens ignored" in srv._consult(question="q", max_tokens=50)


def test_the_claude_footers_are_unchanged(server, fake_claude, monkeypatch):
    srv = server(ADVISOR_BACKEND="claude-code")
    answer = srv._consult(question="q", model="balanced", effort="high")
    assert "[advisor: claude-code/sonnet · billed to your own Claude Code " \
           "sign-in · effort=high]" in answer

    srv = server(ADVISOR_BACKEND="api", ANTHROPIC_API_KEY="k")
    monkeypatch.setattr(srv, "_consult_api", lambda *args: "api answer")
    answer = srv._consult(question="q", model="deep", effort="high")
    assert "[advisor: claude-opus-5 · billed to API ACCOUNT · effort=high · " \
           "max_tokens=" in answer


def test_status_lists_every_backend_and_whether_it_is_ready(server,
                                                            fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    status = srv._status_report()
    line = next(l for l in status.splitlines() if l.startswith("backends:"))
    assert "claude-code (ready)" in line
    assert "api (no credentials)" in line


def test_the_model_catalogue_reads_max_tokens_support_from_the_backends(
        server):
    catalogue = server()._model_catalogue()
    assert "honoured by: api" in catalogue
