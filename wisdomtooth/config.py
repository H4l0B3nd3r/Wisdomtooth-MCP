"""Configuration: one frozen `Settings` object, loaded from explicit inputs.

Five layers, highest priority first:
  1. per-call tool arguments        (model=, effort=, max_tokens=)
  2. runtime overrides              (the advisor_configure tool)
  3. environment variables          (ADVISOR_*, set by the MCP client)
  4. a JSON config file             (ADVISOR_CONFIG, else ~/.wisdomtooth/
                                     config.json) -- machine-wide defaults
  5. the caller preset, then the built-in defaults
This module covers layers 3-5. ADVISOR_LOCK=1 freezes them and makes the
server reject layers 1-2, for hard cost control.

`load_settings` takes the environment and the config file as arguments, so a
configuration can be built and checked without importing the server. A value
that cannot be parsed falls back to its default with a warning: one typo in
an env var must not stop the server from starting, because an MCP client
reports that only as a server that will not connect.
"""

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

from .models import MAX_TOKENS_CEILING, VALID_EFFORT


def state_dir() -> str:
    """The server's own directory under $HOME.

    0.8.0 renamed it from `.claude-advisor` to `.wisdomtooth`. An install that
    predates the rename keeps its stored OAuth token and config file, so the
    old directory still wins when it is the only one present -- a rebrand must
    not silently sign the user out. Every writer goes through here, so the two
    never end up half-populated.
    """
    home = os.path.expanduser("~")
    current = os.path.join(home, ".wisdomtooth")
    if not os.path.isdir(current):
        legacy = os.path.join(home, ".claude-advisor")
        if os.path.isdir(legacy):
            return legacy
    return current


def default_config_path() -> str:
    return os.path.join(state_dir(), "config.json")


# Config-file key -> the environment variable that overrides it.
CONFIG_KEYS = {
    "preset": "ADVISOR_PRESET",
    "backend": "ADVISOR_BACKEND",
    "model": "ADVISOR_MODEL",
    "effort": "ADVISOR_EFFORT",
    "max_tokens": "ADVISOR_MAX_TOKENS",
    "timeout": "ADVISOR_TIMEOUT",
    "timeout_scale": "ADVISOR_TIMEOUT_SCALE",
    "timeout_max": "ADVISOR_TIMEOUT_MAX",
    "idle_timeout": "ADVISOR_IDLE_TIMEOUT",
    "progress_interval": "ADVISOR_PROGRESS_INTERVAL",
    "max_context_chars": "ADVISOR_MAX_CONTEXT_CHARS",
    "max_budget_usd": "ADVISOR_MAX_BUDGET_USD",
    "lock": "ADVISOR_LOCK",
    "nsfw_scrub": "ADVISOR_NSFW_SCRUB",
    "transport": "ADVISOR_TRANSPORT",
    "host": "ADVISOR_HOST",
    "port": "ADVISOR_PORT",
    "http_token": "ADVISOR_HTTP_TOKEN",
    "http_no_auth": "ADVISOR_HTTP_NO_AUTH",
    "claude_bin": "ADVISOR_CLAUDE_BIN",
    "tiers": "ADVISOR_TIERS_JSON",
    "system_prompt": "ADVISOR_SYSTEM_PROMPT",
    "system_prompt_file": "ADVISOR_SYSTEM_PROMPT_FILE",
    "system_prompt_extra": "ADVISOR_SYSTEM_PROMPT_EXTRA",
    "answer_budget": "ADVISOR_ANSWER_BUDGET",
    "trim_answers": "ADVISOR_TRIM_ANSWERS",
    "minimal_tools": "ADVISOR_MINIMAL_TOOLS",
    "save_consults": "ADVISOR_SAVE_CONSULTS",
    "consult_dir": "ADVISOR_CONSULT_DIR",
    "consult_keep": "ADVISOR_CONSULT_KEEP",
    "usage_log": "ADVISOR_USAGE_LOG",
    "usage_file": "ADVISOR_USAGE_FILE",
    "max_consults_per_hour": "ADVISOR_MAX_CONSULTS_PER_HOUR",
    "max_consults_per_5h": "ADVISOR_MAX_CONSULTS_PER_5H",
    "max_consults_per_week": "ADVISOR_MAX_CONSULTS_PER_WEEK",
    "max_usd_per_day": "ADVISOR_MAX_USD_PER_DAY",
    "repeat_window": "ADVISOR_REPEAT_WINDOW",
    "file_roots": "ADVISOR_FILE_ROOTS",
    "show_support": "ADVISOR_SHOW_SUPPORT",
    # Advisors other than Claude, and which one a plain consult goes to.
    "advisors": "ADVISOR_ADVISORS_JSON",
    "default_advisor": "ADVISOR_DEFAULT_ADVISOR",
    "advisors_file": "ADVISOR_ADVISORS_FILE",
    "accounts_file": "ADVISOR_ACCOUNTS_FILE",
}

# Caller presets: one setting instead of several, sized by the CALLING model's
# context window, because the advisor's answer lands in that window. Anything
# set explicitly (env or config file) still wins over the preset.
#   small  -- up to ~32k context: 7B-30B local models
#   medium -- ~32k-200k: large local models and most hosted models (default)
#   large  -- 200k+: Claude Code, Codex and similar frontier callers
PRESETS = {
    "small": {"answer_budget": 600, "minimal_tools": True},
    "medium": {"answer_budget": 2000, "minimal_tools": False},
    "large": {"answer_budget": 64000, "minimal_tools": False},
}
DEFAULT_PRESET = "medium"

_TRUE = ("1", "true", "yes", "on")


def _stderr(message: str) -> None:
    print("[wisdomtooth] " + message, file=sys.stderr)


def load_config_file(path: Optional[str] = None,
                     warn: Callable[[str], None] = _stderr) -> dict:
    path = path or os.environ.get("ADVISOR_CONFIG") or default_config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("top level must be an object")  # caught just below
    except Exception as exc:  # a broken config must never stop the server
        warn(f"ignoring {path}: {exc}")
        return {}
    unknown = set(data) - set(CONFIG_KEYS)
    if unknown:
        warn(f"unknown keys in {path}: {sorted(unknown)}")
    return data


class _Source:
    """Reads one key through the environment, file and preset layers."""

    def __init__(self, environ: Mapping, file_config: Mapping,
                 warn: Callable[[str], None]):
        self.environ = environ
        self.file_config = file_config
        self.preset_values: dict = {}
        self.warn = warn

    def raw(self, key: str):
        value = self.environ.get(CONFIG_KEYS[key])
        if value not in (None, ""):
            return value
        if self.file_config.get(key) is not None:
            return self.file_config[key]
        return self.preset_values.get(key)

    def text(self, key: str, default: str = "") -> str:
        value = self.raw(key)
        return default if value is None else str(value).strip()

    def flag(self, key: str, default: bool) -> bool:
        value = self.raw(key)
        if value is None:
            return default
        return str(value).strip().lower() in _TRUE

    def _parse(self, key, default, kind, what):
        value = self.raw(key)
        if value is None:
            return default
        try:
            return kind(float(str(value).strip()))
        except (TypeError, ValueError, OverflowError):
            self.warn(f"ignoring {CONFIG_KEYS[key]}={value!r}: not {what}; "
                      f"using {default}")
            return default

    def integer(self, key: str, default: int) -> int:
        return self._parse(key, default, int, "a whole number")

    def number(self, key: str, default: float) -> float:
        return self._parse(key, default, float, "a number")


@dataclass(frozen=True)
class Settings:
    """Everything the server reads once, at startup."""
    preset: str = DEFAULT_PRESET
    preset_values: Mapping = field(default_factory=dict)
    # Backend -- WHO GETS BILLED
    # "auto"        -> (default) the Anthropic API when credentials exist
    #                  (ANTHROPIC_API_KEY or an SDK profile); otherwise the
    #                  user's own Claude Code install, if it is signed in.
    # "claude-code" -> always the user's Claude Code install.
    # "api"         -> always the Anthropic API, billed per token.
    backend: str = "auto"
    # "stdio" (the client launches the server on demand) or "http" (a
    # persistent server, e.g. in a container).
    transport: str = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = 8484
    # Bearer token every HTTP request must carry. Without one the server
    # refuses to bind anywhere but loopback, unless http_no_auth says an
    # outside guard (a proxy, a firewall) protects the port.
    http_token: str = ""
    http_no_auth: bool = False
    # Wall-clock limit on a consult: `timeout` is the floor and each consult
    # earns more for its size, answer budget and effort, up to `timeout_max`.
    timeout: int = 300
    timeout_scale: float = 1.0
    timeout_max: int = 3600
    # Kill a streaming CLI that has sent nothing for this many seconds. A
    # healthy consult streams something every few seconds even while it
    # thinks, so this catches a hang in minutes rather than at the wall clock.
    idle_timeout: float = 300.0
    # Seconds between keep-alive progress notifications. Clients restart
    # their request timeout on each one, so it must stay well under it.
    progress_interval: int = 15
    max_budget_usd: Optional[str] = None
    # The answer's length ceiling, in words. 0 removes it.
    answer_budget: int = 2000
    # Return only the lead of an answer that runs far past the budget, when
    # the whole answer is saved to a transcript the caller can read.
    trim_answers: bool = True
    minimal_tools: bool = False
    save_consults: bool = True
    consult_keep: int = 200
    usage_log: bool = True
    max_consults_per_hour: int = 0
    max_consults_per_5h: int = 0
    max_consults_per_week: int = 0
    max_usd_per_day: float = 0.0
    repeat_window_s: float = 1800.0
    show_support: bool = True
    max_context_chars: int = 60000
    model: str = "balanced"
    default_effort: Optional[str] = None
    max_tokens: int = 64000
    locked: bool = False
    tiers: Mapping = field(default_factory=dict)
    # "claude" unless the user picked another advisor.
    default_advisor: str = "claude"


def _tiers(raw, warn) -> dict:
    if not raw:
        return {}
    try:
        custom = raw if isinstance(raw, dict) else json.loads(raw)
        return {str(k).lower(): str(v) for k, v in custom.items()}
    except Exception as exc:
        warn(f"ignoring tier overrides: {exc}")
        return {}


def load_settings(environ: Optional[Mapping] = None,
                  file_config: Optional[Mapping] = None,
                  warn: Optional[Callable[[str], None]] = None) -> Settings:
    environ = os.environ if environ is None else environ
    warn = warn or _stderr
    if file_config is None:
        file_config = load_config_file(environ.get("ADVISOR_CONFIG"), warn)
    src = _Source(environ, file_config, warn)

    preset = src.text("preset", DEFAULT_PRESET).lower()
    if preset not in PRESETS:
        warn(f"unknown ADVISOR_PRESET {preset!r}; using {DEFAULT_PRESET!r}. "
             f"Valid: {', '.join(PRESETS)}")
        preset = DEFAULT_PRESET
    src.preset_values = dict(PRESETS[preset])

    effort = src.text("effort").lower()
    if effort and effort not in VALID_EFFORT:
        warn(f"ignoring ADVISOR_EFFORT={effort!r}; valid levels are "
             + ", ".join(VALID_EFFORT))
    timeout = max(1, src.integer("timeout", 300))
    return Settings(
        preset=preset,
        preset_values=dict(PRESETS[preset]),
        backend=src.text("backend", "auto").lower(),
        transport=src.text("transport", "stdio").lower(),
        http_host=src.text("host", "127.0.0.1"),
        http_port=src.integer("port", 8484),
        http_token=src.text("http_token"),
        http_no_auth=src.flag("http_no_auth", False),
        timeout=timeout,
        timeout_scale=max(0.0, src.number("timeout_scale", 1.0)),
        timeout_max=max(timeout, src.integer("timeout_max", 3600)),
        idle_timeout=max(0.0, src.number("idle_timeout", 300.0)),
        progress_interval=max(1, src.integer("progress_interval", 15)),
        max_budget_usd=src.raw("max_budget_usd"),
        answer_budget=max(0, src.integer("answer_budget", 2000)),
        trim_answers=src.flag("trim_answers", True),
        minimal_tools=src.flag("minimal_tools", False),
        save_consults=src.flag("save_consults", True),
        consult_keep=max(0, src.integer("consult_keep", 200)),
        usage_log=src.flag("usage_log", True),
        max_consults_per_hour=max(0, src.integer("max_consults_per_hour", 0)),
        max_consults_per_5h=max(0, src.integer("max_consults_per_5h", 0)),
        max_consults_per_week=max(0, src.integer("max_consults_per_week", 0)),
        max_usd_per_day=max(0.0, src.number("max_usd_per_day", 0.0)),
        repeat_window_s=max(0.0, src.number("repeat_window", 30.0)) * 60,
        show_support=src.flag("show_support", True),
        max_context_chars=max(1, src.integer("max_context_chars", 60000)),
        model=src.text("model", "balanced") or "balanced",
        default_effort=effort if effort in VALID_EFFORT else None,
        max_tokens=max(1, min(src.integer("max_tokens", 64000),
                              MAX_TOKENS_CEILING)),
        locked=src.flag("lock", False),
        tiers=_tiers(src.raw("tiers"), warn),
        default_advisor=src.text("default_advisor", "claude").lower()
        or "claude",
    )
