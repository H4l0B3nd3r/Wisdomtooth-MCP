"""`wisdomtooth-mcp doctor`: check the setup and print a config to paste.

Most setup mistakes are a config typed by hand into the wrong shape for the
client, so the doctor prints each client's exact shape, pointed at the
executable that is actually installed.
"""

import argparse
import json
import shutil
import sys
from typing import Callable, Optional, Sequence

CLIENTS = ("kilo", "opencode", "claude-code", "claude-desktop", "cursor",
           "cline", "codex")

WHERE = {
    "kilo": "kilo.jsonc in the project, or your global kilo.json",
    "opencode": "opencode.json in the project, or ~/.config/opencode/"
                "opencode.json",
    "claude-code": "run this in a terminal",
    "claude-desktop": "claude_desktop_config.json (Settings > Developer > "
                      "Edit Config)",
    "cursor": "~/.cursor/mcp.json, or .cursor/mcp.json in the project",
    "cline": "cline_mcp_settings.json (Cline > MCP Servers > Configure)",
    "codex": "~/.codex/config.toml",
}

# Kilo restarts its per-request timeout on every progress notification, which
# the server sends every 15s; Codex's tool timeout is a flat limit, so it has
# to cover the longest consult outright.
_KILO_TIMEOUT_MS = 300000
_CODEX_TOOL_TIMEOUT_S = 3600


def server_command() -> tuple:
    """(command, args) that start this server on this machine."""
    exe = shutil.which("wisdomtooth-mcp")
    if exe:
        return exe, []
    return sys.executable, ["-m", "wisdomtooth.server"]


def _toml_string(value: str) -> str:
    # A JSON string is a valid TOML basic string for anything this prints.
    return json.dumps(value)


def client_config(client: str, command: str, env: dict,
                  args: Sequence[str] = ()) -> str:
    env = dict(env)
    args = list(args)
    if client in ("kilo", "opencode"):
        entry = {"type": "local", "command": [command] + args,
                 "environment": env, "enabled": True}
        if client == "kilo":
            entry["timeout"] = _KILO_TIMEOUT_MS
        doc = {"mcp": {"wisdomtooth": entry}}
        if client == "opencode":
            doc = {"$schema": "https://opencode.ai/config.json", **doc}
        return json.dumps(doc, indent=2)
    if client in ("claude-desktop", "cursor", "cline"):
        return json.dumps({"mcpServers": {"wisdomtooth": {
            "command": command, "args": args, "env": env}}}, indent=2)
    if client == "codex":
        lines = ["[mcp_servers.wisdomtooth]",
                 "command = " + _toml_string(command),
                 "args = [" + ", ".join(_toml_string(a) for a in args) + "]",
                 "startup_timeout_sec = 30",
                 f"tool_timeout_sec = {_CODEX_TOOL_TIMEOUT_S}"]
        if env:
            lines += ["", "[mcp_servers.wisdomtooth.env]"]
            lines += [f"{k} = {_toml_string(str(v))}" for k, v in env.items()]
        return "\n".join(lines) + "\n"
    if client == "claude-code":
        parts = ["claude mcp add --scope user wisdomtooth"]
        parts += [f"--env {k}={v}" if " " not in str(v) else f'--env "{k}={v}"'
                  for k, v in env.items()]
        parts.append("--")
        parts += [f'"{command}"'] + [f'"{a}"' for a in args]
        return " ".join(parts)
    raise ValueError(f"unknown client {client!r}; choose from "
                     + ", ".join(CLIENTS))


def run(argv: Sequence[str], report: Callable[[], str], version: str,
        out: Optional[Callable[[str], None]] = None) -> int:
    out = out or print
    parser = argparse.ArgumentParser(
        prog="wisdomtooth-mcp doctor",
        description="Check credentials and the CLI, then print a ready-to-"
                    "paste MCP config for your client.")
    parser.add_argument("--client", default="all",
                        choices=("all",) + CLIENTS)
    parser.add_argument("--preset", choices=("small", "medium", "large"),
                        help="caller preset to put in the config: small for "
                             "7B-30B local models, large for frontier callers")
    options = parser.parse_args(list(argv))

    out(f"wisdomtooth-mcp {version}\n")
    out(report())
    command, args = server_command()
    if args:
        out("\nnote: `wisdomtooth-mcp` is not on PATH, so the configs below "
            "start the server through this Python interpreter instead.")
    env = {"ADVISOR_PRESET": options.preset} if options.preset else {}
    clients = CLIENTS if options.client == "all" else (options.client,)
    for client in clients:
        out(f"\n--- {client}: {WHERE[client]} ---")
        out(client_config(client, command, env, args))
    return 0
