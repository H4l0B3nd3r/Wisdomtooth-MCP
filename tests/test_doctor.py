"""`wisdomtooth-mcp doctor`: check the setup and print a config to paste.

Most setup mistakes are a config typed by hand into the wrong shape for the
client, so the doctor prints each client's exact shape, pointed at the
executable that is actually installed.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from wisdomtooth import doctor

PKG_ROOT = Path(__file__).resolve().parents[1]
CMD = r"C:\Users\me\.local\bin\wisdomtooth-mcp.exe"
ENV = {"ADVISOR_PRESET": "small"}


def test_every_client_gets_a_config_with_the_environment():
    for client in doctor.CLIENTS:
        text = doctor.client_config(client, CMD, ENV)
        assert "wisdomtooth" in text, client
        assert "ADVISOR_PRESET" in text, client


def test_the_mcpservers_clients_get_valid_json():
    for client in ("claude-desktop", "cursor", "cline"):
        entry = json.loads(doctor.client_config(client, CMD, ENV))[
            "mcpServers"]["wisdomtooth"]
        assert entry["command"] == CMD
        assert entry["env"] == ENV


def test_kilo_and_opencode_get_their_own_shape():
    for client in ("kilo", "opencode"):
        entry = json.loads(doctor.client_config(client, CMD, ENV))[
            "mcp"]["wisdomtooth"]
        assert entry["type"] == "local"
        assert entry["command"] == [CMD]
        assert entry["environment"] == ENV
        assert entry["enabled"] is True


def test_codex_gets_valid_toml_even_with_windows_paths():
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads(doctor.client_config("codex", CMD, ENV))
    entry = data["mcp_servers"]["wisdomtooth"]
    assert entry["command"] == CMD
    assert entry["env"] == ENV
    assert entry["tool_timeout_sec"] >= 600  # consults outlast Codex's default


def test_claude_code_gets_a_command_line():
    line = doctor.client_config("claude-code", CMD, ENV)
    assert line.startswith("claude mcp add")
    assert "--env ADVISOR_PRESET=small" in line
    assert line.rstrip().endswith("-- " + '"' + CMD + '"')


def run_cli(*args, **env):
    child = os.environ.copy()
    child.update({"PYTHONPATH": str(PKG_ROOT), "PYTHONIOENCODING": "utf-8"})
    child.pop("ANTHROPIC_API_KEY", None)
    child.update(env)
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; from wisdomtooth.server import main; "
         "sys.exit(main(sys.argv[1:]))", *args],
        capture_output=True, text=True, encoding="utf-8", env=child,
        timeout=120)


def test_doctor_runs_from_the_command_line(fake_claude):
    result = run_cli("doctor", "--client", "kilo",
                     ADVISOR_BACKEND="claude-code",
                     ADVISOR_CLAUDE_BIN=str(fake_claude.path),
                     FAKE_CLAUDE_LOG=str(fake_claude.log))
    assert result.returncode == 0, result.stderr
    assert "active backend: claude-code" in result.stdout
    assert '"mcp"' in result.stdout


def test_version_flag():
    from wisdomtooth import server
    result = run_cli("--version")
    assert result.returncode == 0
    assert server.__version__ in result.stdout


def test_an_unknown_command_is_an_error():
    result = run_cli("frobnicate")
    assert result.returncode == 2
    assert "doctor" in result.stderr
