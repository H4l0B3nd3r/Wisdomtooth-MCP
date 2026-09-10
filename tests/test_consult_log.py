"""Consult transcripts: the copy of the answer that outlives the chat.

The advisor's answer lands as a tool result inside another agent's chat, and
what happens to it there is not up to this server -- editors collapse it, small
local models paraphrase it, context trims delete it. These tests pin the
mechanism that survives all three: a Markdown file on disk, its path quoted in
the answer footer, and a `resource_link` block pointing at the same file.
"""

import urllib.parse
import urllib.request
from pathlib import Path

from mcp import types

from test_mcp_protocol import _call, advisor_session


def _path_of(uri) -> Path:
    """The local path a file:// URI names, percent-decoding included."""
    return Path(urllib.request.url2pathname(
        urllib.parse.urlparse(str(uri)).path))


def _audience(annotations) -> list:
    """Audience as plain strings; the SDKs disagree on enum vs str."""
    return [getattr(a, "value", a) for a in (annotations.audience or [])]


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def test_a_consult_writes_a_transcript(server, fake_claude, consult_dir):
    srv = server(ADVISOR_BACKEND="claude-code")
    answer = srv._consult(question="WHY-IS-THE-BUILD-RED", context="CTX-BODY",
                          kind="ask_wisdomtooth")

    files = list(consult_dir.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    # Both halves of the exchange, so the file stands on its own.
    assert "WHY-IS-THE-BUILD-RED" in text
    assert "CTX-BODY" in text
    assert "FAKE ANSWER" in text
    # And the answer the caller got points at it.
    assert str(files[0]) in answer


def test_the_filename_carries_the_tool_and_the_question(server, fake_claude,
                                                        consult_dir):
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._consult(question="Why does the DataGrid flicker?", kind="ask_wisdomtooth")

    name = next(consult_dir.glob("*.md")).name
    assert "ask-wisdomtooth" in name
    assert "why-does-the-datagrid-flicker" in name


def test_a_hostile_question_cannot_escape_the_directory(server, fake_claude,
                                                        consult_dir):
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._consult(question="../../../../etc/passwd C:\\Windows\\system32 <>|",
                 kind="ask_wisdomtooth")

    written = list(consult_dir.glob("*.md"))
    assert len(written) == 1
    # Nothing outside the directory, and nothing but the whitelist in the name.
    assert written[0].parent == consult_dir
    assert set(written[0].stem) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")


def test_secrets_are_redacted_in_the_transcript(server, fake_claude, consult_dir):
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._consult(question="is this key ok?",
                 context="ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnop12345")

    text = next(consult_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "sk-ant-abcdefghijklmnop12345" not in text
    assert "REDACTED" in text


def test_two_consults_with_one_question_do_not_overwrite(server, fake_claude,
                                                         consult_dir):
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._consult(question="same question")
    srv._consult(question="same question")

    assert len(list(consult_dir.glob("*.md"))) == 2


def test_saving_can_be_turned_off(server, fake_claude, consult_dir):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_SAVE_CONSULTS="0")
    answer = srv._consult(question="q")

    assert not consult_dir.exists()
    assert "[saved:" not in answer
    # The billing footer is untouched by the switch.
    assert "billed to SUBSCRIPTION" in answer


def test_a_failed_write_costs_the_transcript_not_the_answer(server, fake_claude,
                                                            tmp_path):
    # A file where the directory should be: makedirs raises, and the consult
    # has to carry on regardless -- the answer is what the user paid for.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    srv = server(ADVISOR_BACKEND="claude-code",
                 ADVISOR_CONSULT_DIR=str(blocker / "consults"))

    answer = srv._consult(question="q")

    assert "FAKE ANSWER" in answer
    assert "[saved:" not in answer


def test_old_transcripts_are_pruned(server, fake_claude, consult_dir):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_CONSULT_KEEP="3")
    for i in range(5):
        srv._consult(question=f"question number {i}")

    assert len(list(consult_dir.glob("*.md"))) == 3


def test_keep_zero_means_keep_everything(server, fake_claude, consult_dir):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_CONSULT_KEEP="0")
    for i in range(4):
        srv._consult(question=f"question number {i}")

    assert len(list(consult_dir.glob("*.md"))) == 4


def test_the_saved_path_is_reported_to_the_caller(server, fake_claude,
                                                  consult_dir):
    srv = server(ADVISOR_BACKEND="claude-code")
    saved: dict = {}
    srv._consult(question="q", saved=saved)

    assert Path(saved["path"]).exists()
    assert Path(saved["path"]).parent == consult_dir


def test_status_reports_where_transcripts_go(server, fake_claude, consult_dir):
    srv = server(ADVISOR_BACKEND="claude-code")
    assert str(consult_dir) in srv.advisor_status()

    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_SAVE_CONSULTS="0")
    assert "disabled" in srv.advisor_status()


# --------------------------------------------------------------------------
# What the client actually receives
# --------------------------------------------------------------------------

async def test_ask_wisdomtooth_returns_a_resource_link_beside_the_answer(fake_claude):
    async with advisor_session(fake_claude) as session:
        result, text = await _call(session, "ask_wisdomtooth", {
            "question": "why is it broken",
            "context": "some code",
            "attempts_so_far": "read the docs",
        })

    assert "FAKE ANSWER" in text
    links = [b for b in result.content if isinstance(b, types.ResourceLink)]
    assert len(links) == 1, [b.type for b in result.content]

    link = links[0]
    assert str(link.uri).startswith("file:///")
    assert str(link.uri).endswith(".md")
    # Marked as the human's copy, which is the whole point of the second block.
    assert _audience(link.annotations) == ["user"]
    # And it names a file that exists, holding the answer.
    assert "FAKE ANSWER" in _path_of(link.uri).read_text(encoding="utf-8")


async def test_every_consult_tool_links_its_transcript(fake_claude):
    calls = {
        "ask_wisdomtooth": {"question": "q", "context": "c", "attempts_so_far": "a"},
        "review_code": {"code": "print(1)", "concern": "security"},
        "compare_approaches": {"problem": "p", "options": "a\nb"},
    }
    async with advisor_session(fake_claude) as session:
        for name, args in calls.items():
            result, _ = await _call(session, name, args)
            links = [b for b in result.content
                     if isinstance(b, types.ResourceLink)]
            assert len(links) == 1, name
            assert name.replace("_", "-") in links[0].name, name


async def test_a_consult_tool_advertises_no_output_schema(fake_claude):
    """Content blocks and a structured-output schema do not mix.

    Annotating the return type would make mcp 1.x derive a schema and echo
    every block back a second time as JSON; the unannotated return keeps both
    SDK majors on the plain content path.
    """
    async with advisor_session(fake_claude) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}

    for name in ("ask_wisdomtooth", "review_code", "compare_approaches"):
        schema = getattr(tools[name], "outputSchema", None) or getattr(
            tools[name], "output_schema", None)
        assert schema is None, f"{name} advertises {schema}"


async def test_no_resource_link_when_saving_is_off(fake_claude):
    async with advisor_session(fake_claude, ADVISOR_SAVE_CONSULTS="0") as session:
        result, text = await _call(session, "ask_wisdomtooth", {
            "question": "q", "context": "c", "attempts_so_far": "a",
        })

    assert "FAKE ANSWER" in text
    assert not [b for b in result.content if isinstance(b, types.ResourceLink)]
