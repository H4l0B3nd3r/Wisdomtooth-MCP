"""Advisors that are another vendor's coding-agent CLI, run headless.

Codex, Gemini CLI, Antigravity, Kilo, OpenCode, Qwen Code and GitHub Copilot
each answer a one-shot prompt from the terminal. An advisor on one of them
uses the user's own install, with whatever sign-in or key the user gave that
CLI; this server never signs in to anything.

Every preset follows the rules the Claude CLI backend does:
  - the prompt, system instructions included, goes on stdin. Windows caps a
    command line, and an npm `.cmd` shim cuts an argument at its first newline;
  - the CLI runs in its read-only mode, in an empty folder, because these are
    agents that can edit files and run commands, and an advisor only answers;
  - output is read line by line, so a CLI that stops talking is caught by the
    idle limit, and the heartbeat can say what it is doing.

The flags and output formats are the ones each CLI documented or printed when
probed in September 2026 (codex 0.155, gemini 0.60, agy 1.2, kilo 7.8,
qwen 0.15, copilot 0.0.421).
"""

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

# The environment marker a CLI advisor runs under. A Wisdomtooth server that
# starts with it set was launched by an advisor's own MCP config, and refuses
# to consult: otherwise an advisor could ask Wisdomtooth, which asks the
# advisor, without end.
NESTED_ENV = "WISDOMTOOTH_NESTED"

_CHARS_PER_WORD = 6

# Words in a failed run's output that mean "sign in first".
_AUTH_WORDS = ("not logged in", "log in", "login", "sign in", "unauthorized",
               "unauthenticated", "authenticat", "api key", "401", "403",
               "credential")


def stdin_text(system: str, user_content: str) -> str:
    """The prompt these CLIs read. None of them takes a system prompt the
    server can pass safely, so the advisor persona goes first, marked off."""
    return ("<instructions>\n" + system.strip() + "\n</instructions>\n\n"
            + user_content)


# --------------------------------------------------------------------------
# Output readers
# --------------------------------------------------------------------------

@dataclass
class Outcome:
    answer: str
    usage: dict = field(default_factory=dict)
    error: Optional[str] = None


class TextReader:
    """A CLI that prints its answer as plain text."""

    def __init__(self):
        self.lines: list = []
        self.phase = "waiting for the first token"
        self.chars = 0

    def feed(self, line: str) -> None:
        self.lines.append(line)
        self.write(len(line))

    def write(self, chars: int) -> None:
        self.phase = "writing"
        self.chars += chars

    @property
    def note(self) -> str:
        if self.phase == "writing" and self.chars:
            return (f"writing, ~{max(1, self.chars // _CHARS_PER_WORD):,} "
                    "words so far")
        return self.phase

    def stdout(self) -> str:
        return "".join(self.lines)

    def outcome(self, returncode: int, stderr: str) -> Outcome:
        text = self.stdout().strip()
        if returncode != 0:
            return Outcome("", error=(stderr.strip() or text
                                      or f"exit {returncode}"))
        return Outcome(text)


class JsonLinesReader(TextReader):
    """A CLI that prints one JSON event per line. Subclasses read events."""

    def __init__(self):
        super().__init__()
        self.parts: list = []
        self.usage: dict = {}
        self.error: Optional[str] = None
        self.final: Optional[str] = None

    def feed(self, line: str) -> None:
        self.lines.append(line)
        try:
            event = json.loads(line)
        except ValueError:
            return  # a banner or a log line
        if isinstance(event, dict):
            self.event(event)

    def event(self, event: dict) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def text(self, chunk: str) -> None:
        if chunk:
            self.parts.append(chunk)
            self.write(len(chunk))

    def outcome(self, returncode: int, stderr: str) -> Outcome:
        answer = (self.final if self.final is not None
                  else "".join(self.parts)).strip()
        if self.error:
            return Outcome(answer, self.usage, self.error)
        if returncode != 0 and not answer:
            return Outcome("", self.usage, stderr.strip()
                           or f"exit {returncode}")
        return Outcome(answer, self.usage)


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _message(value) -> str:
    """An error's text, whether a string or a nested object."""
    if isinstance(value, dict):
        for key in ("message", "data", "error"):
            if key in value:
                return _message(value[key])
        return json.dumps(value)[:500]
    return str(value or "")


class CodexReader(JsonLinesReader):
    """`codex exec --json`: item.completed agent_message, turn.completed."""

    def event(self, event: dict) -> None:
        kind = event.get("type")
        item = event.get("item") or {}
        if kind in ("item.started", "item.updated") and \
                item.get("type") == "reasoning":
            self.phase = "thinking"
        elif kind == "item.completed" and item.get("type") == "agent_message":
            self.text(item.get("text") or "")
            self.final = item.get("text") or ""
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
            cached = _int(usage.get("cached_input_tokens"))
            self.usage = dict(
                input_tokens=max(0, _int(usage.get("input_tokens")) - cached),
                cache_read_tokens=cached,
                output_tokens=_int(usage.get("output_tokens")))
        elif kind in ("turn.failed", "error"):
            self.error = _message(event.get("error") or event.get("message"))


class AntigravityReader(JsonLinesReader):
    """`agy --output-format stream-json`: step_update deltas, then result."""

    def event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "step_update":
            step = event.get("step_update") or {}
            if step.get("step_type") == "agent_response":
                self.text(step.get("text_delta") or "")
        elif kind == "result":
            result = event.get("result") or {}
            usage = result.get("usage") or {}
            self.usage = dict(
                input_tokens=_int(usage.get("input_tokens")),
                cache_read_tokens=_int(usage.get("cache_read_tokens")),
                output_tokens=_int(usage.get("output_tokens"))
                + _int(usage.get("thinking_tokens")))
            if str(result.get("status", "")).upper() != "SUCCESS":
                self.error = _message(result.get("error")) or \
                    f"status {result.get('status')}"
            else:
                self.final = result.get("response") or ""


class KiloReader(JsonLinesReader):
    """`kilo run --format json` (and OpenCode): text parts, step_finish."""

    def event(self, event: dict) -> None:
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "text":
            self.text(part.get("text") or "")
        elif kind == "step_finish":
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            add = dict(input_tokens=_int(tokens.get("input")),
                       cache_read_tokens=_int(cache.get("read")),
                       cache_write_tokens=_int(cache.get("write")),
                       output_tokens=_int(tokens.get("output"))
                       + _int(tokens.get("reasoning")))
            for key, value in add.items():
                self.usage[key] = self.usage.get(key, 0) + value
            if isinstance(part.get("cost"), (int, float)):
                self.usage["cost_usd"] = (self.usage.get("cost_usd", 0.0)
                                          + float(part["cost"]))
        elif kind == "error":
            self.error = _message(event.get("error"))


class GeminiReader(JsonLinesReader):
    """`gemini --output-format stream-json`: message deltas, then result."""

    def event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "message" and event.get("role") == "assistant":
            self.text(event.get("content") or "")
        elif kind == "result":
            stats = event.get("stats") or {}
            self.usage = dict(input_tokens=_int(stats.get("input_tokens")),
                              output_tokens=_int(stats.get("output_tokens")))
            if str(event.get("status", "success")).lower() != "success":
                self.error = _message(event.get("error")) or "the run failed"


class QwenReader(JsonLinesReader):
    """`qwen --output-format stream-json`: Claude Code's event shapes."""

    def event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "stream_event":
            delta = (event.get("event") or {}).get("delta") or {}
            if delta.get("type") == "text_delta":
                self.write(len(delta.get("text") or ""))
            elif delta.get("type") == "thinking_delta":
                self.phase = "thinking"
        elif kind == "result":
            usage = event.get("usage") or {}
            self.usage = dict(
                input_tokens=_int(usage.get("input_tokens")),
                cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
                output_tokens=_int(usage.get("output_tokens")))
            text = str(event.get("result") or "")
            # A failed request can come back as a "successful" result whose
            # text is the error.
            if event.get("is_error") or text.startswith("[API Error"):
                self.error = text or _message(event.get("error"))
            else:
                self.final = text


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------

def _opt(flag: str, value: Optional[str]) -> list:
    return [flag, value] if value else []


# Qwen Code's read tools, excluded so an advisor cannot read the machine's
# files even in plan mode.
_QWEN_TOOLS = ("read_file", "list_directory", "glob", "grep_search",
               "web_fetch", "agent", "skill", "todo_write", "ask_user_question",
               "exit_plan_mode", "task_stop", "send_message")


@dataclass(frozen=True)
class CliPreset:
    label: str
    binary: str
    # (model or "", effort or None) -> the arguments after the binary.
    argv: Callable[[str, Optional[str]], list]
    reader: Callable[[], TextReader]
    # How the prompt text is framed on stdin.
    frame: Callable[[str], str] = lambda text: text
    efforts: tuple = ()
    tiers: Mapping = field(default_factory=dict)
    billing: str = ""
    # What the user must do before it works, for advisor_status and errors.
    setup: str = ""


PRESETS = {
    "codex": CliPreset(
        label="Codex CLI (OpenAI)", binary="codex",
        argv=lambda m, e: (["exec", "--json", "--skip-git-repo-check",
                            "--ephemeral", "--sandbox", "read-only"]
                           + _opt("-m", m)
                           + (["-c", f"model_reasoning_effort={e}"] if e else [])
                           + ["-"]),
        reader=CodexReader,
        efforts=("minimal", "low", "medium", "high", "xhigh"),
        billing="your own Codex CLI sign-in (ChatGPT plan or OpenAI API key)",
        setup="install Codex (`npm install -g @openai/codex`) and sign in "
              "by running `codex` once"),
    "gemini-cli": CliPreset(
        label="Gemini CLI (Google)", binary="gemini",
        argv=lambda m, e: (["--output-format", "stream-json",
                            "--approval-mode", "plan",
                            "--allowed-mcp-server-names", "none"]
                           + _opt("-m", m) + ["-p", ""]),
        reader=GeminiReader,
        billing="your own Gemini CLI setup (GEMINI_API_KEY or Vertex AI)",
        setup="install Gemini CLI and set GEMINI_API_KEY. Google no longer "
              "accepts the personal Google-account login from this CLI; "
              "use Antigravity for that"),
    "antigravity": CliPreset(
        label="Antigravity CLI (Google)", binary="agy",
        argv=lambda m, e: (["--input-format", "stream-json",
                            "--output-format", "stream-json", "--print=",
                            "--sandbox", "--disable-slash-commands"]
                           + _opt("--model", m) + _opt("--effort", e)),
        reader=AntigravityReader,
        frame=lambda text: json.dumps(
            {"event": "user", "message": {"content": text}}) + "\n",
        efforts=("low", "medium", "high"),
        tiers={"fast": "gemini-3.8-flash-low",
               "balanced": "gemini-3.8-flash-medium",
               "deep": "gemini-3.1-pro-high"},
        billing="your own Antigravity sign-in",
        setup="install Antigravity and sign in by running `agy` once"),
    "kilo": CliPreset(
        label="Kilo Code CLI", binary="kilo",
        argv=lambda m, e: (["run", "--format", "json", "--agent", "ask",
                            "--pure"] + _opt("-m", m) + _opt("--variant", e)),
        reader=KiloReader,
        efforts=("minimal", "low", "medium", "high", "max"),
        billing="your own Kilo providers (whatever `kilo auth` holds)",
        setup="install Kilo (`npm install -g @kilocode/cli`) and add a "
              "provider with `kilo auth login`; models are provider/model"),
    "opencode": CliPreset(
        label="OpenCode", binary="opencode",
        argv=lambda m, e: (["run", "--format", "json", "--agent", "plan"]
                           + _opt("-m", m) + _opt("--variant", e)),
        reader=KiloReader,
        efforts=("minimal", "low", "medium", "high", "max"),
        billing="your own OpenCode providers",
        setup="install OpenCode and add a provider with `opencode auth login`; "
              "models are provider/model"),
    "qwen": CliPreset(
        label="Qwen Code", binary="qwen",
        argv=lambda m, e: (["--output-format", "stream-json",
                            "--include-partial-messages",
                            "--approval-mode", "plan",
                            "--allowed-mcp-server-names", "none"]
                           + [a for tool in _QWEN_TOOLS
                              for a in ("--exclude-tools", tool)]
                           + _opt("-m", m) + ["-p", ""]),
        reader=QwenReader,
        billing="your own Qwen Code setup (`qwen auth`)",
        setup="install Qwen Code and configure it with `qwen auth`"),
    "copilot": CliPreset(
        label="GitHub Copilot CLI", binary="copilot",
        argv=lambda m, e: (["--silent", "--no-custom-instructions",
                            "--disable-builtin-mcps", "--no-auto-update",
                            "--no-color", "--deny-tool", "shell",
                            "--deny-tool", "write"] + _opt("--model", m)),
        reader=TextReader,
        billing="your own GitHub Copilot subscription",
        setup="install the GitHub Copilot CLI and sign in with "
              "`copilot login`"),
    "cli": CliPreset(
        label="command-line tool", binary="",
        argv=lambda m, e: [], reader=TextReader,
        billing="the tool's own account",
        setup="set `command` to the executable; it must read the prompt on "
              "stdin and print the answer"),
}


def looks_like_auth(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _AUTH_WORDS)


def clamp_effort(preset: CliPreset, effort: Optional[str]) -> Optional[str]:
    """The effort to pass, clamped to the preset's levels, or None."""
    if not effort or not preset.efforts:
        return None
    effort = effort.lower()
    if effort in preset.efforts:
        return effort
    order = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    rank = order.index(effort) if effort in order else len(order) - 1
    lower = [e for e in preset.efforts if order.index(e) <= rank]
    return lower[-1] if lower else preset.efforts[0]


# Model names reach the command line; keep them to characters that are never
# shell syntax (cmd.exe runs npm-installed CLIs).
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]*$")
