"""Outbound content hygiene: secret redaction, size caps, word scrubbing.

Everything the agent passes leaves the machine, so this is the last line of
defence -- but it must not corrupt the technical content it is protecting.
"""

import json

import pytest

SECRETS = [
    ("sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAA", "anthropic-key"),
    ("ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "github-token"),
    ("github_pat_AAAAAAAAAAAAAAAAAAAAAA", "github-pat"),
    ("AKIAIOSFODNN7EXAMPLE", "aws-key-id"),
    ("xoxb-123456789012-abcdefghijkl", "slack-token"),
    ("AIzaSyA12345678901234567890123456789012", "google-key"),
]


@pytest.mark.parametrize("secret,label", SECRETS)
def test_secrets_are_redacted(server, secret, label):
    out = server()._sanitize("token is " + secret + " ok")
    assert secret not in out
    assert label in out


def test_private_key_blocks_are_redacted(server):
    blob = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n"
            "-----END RSA PRIVATE KEY-----")
    assert "MIIEabc" not in server()._sanitize(blob)


def test_assignment_style_secrets_are_redacted(server):
    out = server()._sanitize('password = "hunter2hunter2"')
    assert "hunter2hunter2" not in out


def test_oversized_context_is_truncated_with_a_visible_marker(server):
    srv = server(ADVISOR_MAX_CONTEXT_CHARS="1000")
    out = srv._sanitize("A" * 5000)
    assert len(out) < 5000
    assert "truncated" in out


def test_head_and_tail_are_both_kept(server):
    srv = server(ADVISOR_MAX_CONTEXT_CHARS="1000")
    out = srv._sanitize("HEAD" + "x" * 5000 + "TAIL")
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")


def test_content_under_the_cap_is_untouched(server):
    text = "def f():\n    return 1\n"
    assert server()._sanitize(text) == text


# --------------------------------------------------------------------------
# Word scrubbing must not damage code
# --------------------------------------------------------------------------

SAFE_TOKENS = [
    "class Foo:", "assert x == 1", "shell=True", "a cocktail of errors",
    "Bass.play()", "hello", "passthrough", "massive", "classic",
    "brasserie", "Cassandra", "assess", "grassland", "embarrass",
]


@pytest.mark.parametrize("text", SAFE_TOKENS)
def test_scrubber_leaves_ordinary_code_and_prose_alone(server, text):
    assert server()._scrub_nsfw(text) == text


def test_scrubbing_is_case_preserving(server):
    srv = server()
    assert srv._scrub_nsfw("Damn").istitle()
    assert srv._scrub_nsfw("DAMN").isupper()


def test_scrubbing_can_be_disabled(server):
    srv = server(ADVISOR_NSFW_SCRUB="0")
    assert srv._scrub_nsfw("damn") == "damn"


def test_extra_wordlist_is_loaded(server, tmp_path):
    path = tmp_path / "extra.json"
    path.write_text(json.dumps({"frobnicate": "adjust"}), encoding="utf-8")
    srv = server(ADVISOR_NSFW_EXTRA_JSON=str(path))
    assert srv._scrub_nsfw("frobnicate the widget") == "adjust the widget"


def test_a_broken_extra_wordlist_does_not_kill_the_server(server, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    srv = server(ADVISOR_NSFW_EXTRA_JSON=str(path))
    assert srv._scrub_nsfw("hello") == "hello"


def test_code_under_review_is_never_word_scrubbed(server, fake_claude):
    """review_code exists to critique the exact bytes the caller has on disk.

    Substituting words inside it makes the advisor comment on code that does
    not exist, and any string-literal fix it suggests will not apply.
    """
    srv = server(ADVISOR_BACKEND="claude-code")
    code = 'raise RuntimeError("damn, the shard is unreachable")'
    srv._consult(question="review", context=code, scrub_context=False)
    assert "damn" in fake_claude.last["prompt"]


def test_secrets_are_still_redacted_in_unscrubbed_code(server, fake_claude):
    """Opting out of word scrubbing must not opt out of secret redaction."""
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._consult(question="review",
                 context='KEY = "sk-ant-api03-BBBBBBBBBBBBBBBBBBBB"',
                 scrub_context=False)
    assert "sk-ant-api03-BBBB" not in fake_claude.last["prompt"]
