"""The package is split by concern, and each part works without the server.

`server.py` is the composition root: it loads the settings, builds the MCP
server and wires the parts together. The parts take what they need as
arguments, so they can be used -- and tested -- without importing it.
"""

import subprocess
import sys

import pytest

PARTS = ("config", "errors", "prompts", "models", "safety", "usage",
         "transcripts", "claude_cli", "backends", "httpauth", "doctor")


def test_every_part_imports_without_the_server():
    code = ("import sys\n"
            + "".join(f"import wisdomtooth.{p}\n" for p in PARTS)
            + "print('wisdomtooth.server' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == "False"


def test_safety_works_without_configuration():
    from wisdomtooth import safety
    assert "[REDACTED:anthropic-key]" in safety.redact(
        "key sk-ant-abcdefghijklmnop")
    assert len(safety.truncate("x" * 1000, 300)) < 1000


def test_usage_works_without_configuration():
    from wisdomtooth import usage
    assert usage.estimate_cost("claude-opus-5", 1_000_000, 0) == \
        pytest.approx(5.0)
    assert usage.fmt_usd(0.0033) == "$0.0033"


def test_the_stream_reader_tracks_progress_and_the_result():
    from wisdomtooth import claude_cli
    state = claude_cli.StreamState()
    state.feed('{"type":"stream_event","event":{"type":"content_block_start",'
               '"content_block":{"type":"thinking"}}}')
    assert state.note == "thinking"
    state.feed('{"type":"stream_event","event":{"type":"content_block_delta",'
               '"delta":{"type":"text_delta","text":"' + "word " * 60 + '"}}}')
    assert "words so far" in state.note
    state.feed('{"type":"result","result":"done","is_error":false}')
    assert state.stdout() == '{"type": "result", "result": "done", ' \
                             '"is_error": false}'
