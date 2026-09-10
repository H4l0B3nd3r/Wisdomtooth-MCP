"""Wisdomtooth MCP Server.

Exposes a frontier model as an *escalation* advisor over MCP. Other agents
(Kilo Code, Cursor, Cline, custom agents) call it when they are stuck -- after
their own attempts and doc lookups (e.g. Context7) have not resolved the
problem.

Claude is the model behind it today and stays the default. The name is
model-neutral on purpose: the backend layer (`_consult_claude_code`,
`_consult_api`) is the seam another provider would slot into. Nothing in the
tool surface assumes Anthropic -- but nothing else is implemented yet either,
so every consult currently goes to Claude.

Billing, in one line: by default the server uses the user's Claude
subscription via the local Claude Code CLI, and only falls back to
pay-per-token API credits when the subscription is not usable. Every answer
says which account paid for it.

Run:
    wisdomtooth-mcp            # auto: subscription first, API as fallback
"""

import asyncio
import contextvars
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import anthropic
from anthropic import Anthropic

# ---------------------------------------------------------------------------
# MCP SDK compatibility
# ---------------------------------------------------------------------------
# mcp 2.0 renamed FastMCP to MCPServer and moved it to mcp.server.mcpserver.
# Both spellings expose the same decorator surface we use, so support each --
# a fresh `uv tool install` gets 2.x while existing installs are still on 1.x.
try:  # mcp >= 2.0
    from mcp.server.mcpserver import Context, MCPServer as _ServerClass
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError
    _MCP_MAJOR = 2
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP as _ServerClass
    from mcp.server.fastmcp.exceptions import ToolError as _ToolError
    _MCP_MAJOR = 1

from mcp.types import Annotations, ResourceLink, TextContent, ToolAnnotations


class AdvisorError(_ToolError, RuntimeError):
    """A failure whose text the calling agent must actually see.

    The SDK treats an arbitrary exception as a server crash and replaces its
    message with a generic "Error executing tool ..." -- which would hide every
    diagnostic this server produces, including the login instructions that are
    the whole point of a good auth failure. `ToolError` is the deliberate,
    message-preserving channel; RuntimeError is kept in the bases so callers
    can catch this without importing the MCP SDK.
    """


class AdvisorInputError(AdvisorError, ValueError):
    """A bad tool argument, where the message IS the fix.

    Same reasoning as `AdvisorError`: a bare `ValueError` would be swallowed by
    the SDK and the caller would be told only that something went wrong, not
    what to pass instead. `ValueError` stays in the bases because that is what
    a bad argument is.
    """

try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("wisdomtooth-mcp")
except Exception:  # running from source without install
    __version__ = "0.0.0+source"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Five layers, highest priority first:
#   1. per-call tool arguments        (model=, effort=, max_tokens=)
#   2. runtime overrides              (the advisor_configure tool)
#   3. environment variables          (ADVISOR_*, set by the MCP client)
#   4. a JSON config file             (ADVISOR_CONFIG, else ~/.wisdomtooth/
#                                      config.json) -- machine-wide defaults
#   5. built-in defaults
# ADVISOR_LOCK=1 freezes layers 3-5 and rejects 1-2, for hard cost control.

def _state_dir() -> str:
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


DEFAULT_CONFIG_PATH = os.path.join(_state_dir(), "config.json")

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
    "progress_interval": "ADVISOR_PROGRESS_INTERVAL",
    "max_context_chars": "ADVISOR_MAX_CONTEXT_CHARS",
    "max_budget_usd": "ADVISOR_MAX_BUDGET_USD",
    "fallback_to_api": "ADVISOR_FALLBACK_TO_API",
    "lock": "ADVISOR_LOCK",
    "nsfw_scrub": "ADVISOR_NSFW_SCRUB",
    "transport": "ADVISOR_TRANSPORT",
    "host": "ADVISOR_HOST",
    "port": "ADVISOR_PORT",
    "claude_bin": "ADVISOR_CLAUDE_BIN",
    "tiers": "ADVISOR_TIERS_JSON",
    "system_prompt": "ADVISOR_SYSTEM_PROMPT",
    "system_prompt_file": "ADVISOR_SYSTEM_PROMPT_FILE",
    "system_prompt_extra": "ADVISOR_SYSTEM_PROMPT_EXTRA",
    "answer_budget": "ADVISOR_ANSWER_BUDGET",
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
}


def _load_config_file() -> dict:
    path = os.environ.get("ADVISOR_CONFIG") or DEFAULT_CONFIG_PATH
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("top level must be an object")  # caught just below
    except Exception as exc:  # a broken config must never stop the server
        print(f"[wisdomtooth] ignoring {path}: {exc}", file=sys.stderr)
        return {}
    unknown = set(data) - set(CONFIG_KEYS)
    if unknown:
        print(f"[wisdomtooth] unknown keys in {path}: {sorted(unknown)}",
              file=sys.stderr)
    return data


_FILE_CONFIG = _load_config_file()


_PRESET_VALUES: dict = {}  # filled once ADVISOR_PRESET is resolved, below


def _setting(key: str, default=None):
    """Environment first, then the config file, then the caller preset, then
    the built-in default."""
    env_value = os.environ.get(CONFIG_KEYS[key])
    if env_value not in (None, ""):
        return env_value
    if key in _FILE_CONFIG and _FILE_CONFIG[key] is not None:
        return _FILE_CONFIG[key]
    if key in _PRESET_VALUES:
        return _PRESET_VALUES[key]
    return default


def _flag(key: str, default: bool) -> bool:
    value = _setting(key)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


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
PRESET = str(_setting("preset", DEFAULT_PRESET)).strip().lower()
if PRESET not in PRESETS:
    print(f"[wisdomtooth] unknown ADVISOR_PRESET {PRESET!r}; using "
          f"{DEFAULT_PRESET!r}. Valid: {', '.join(PRESETS)}", file=sys.stderr)
    PRESET = DEFAULT_PRESET
_PRESET_VALUES.update(PRESETS[PRESET])


# Backend -- WHO GETS BILLED
# "auto"        -> (default) use the local `claude` CLI when it is installed
#                  and logged in, which draws on the Claude SUBSCRIPTION
#                  (Pro/Max); otherwise use ANTHROPIC_API_KEY.
# "claude-code" -> always the CLI. Never silently spends API credits.
# "api"         -> always the direct Anthropic API. Bills the DEVELOPER
#                  CONSOLE account per token, not the subscription.
BACKEND = str(_setting("backend", "auto")).lower()

# Transport: "stdio" (client launches us on demand -- default) or "http"
# (persistent server, e.g. inside a Docker container).
TRANSPORT = str(_setting("transport", "stdio")).lower()
HTTP_HOST = str(_setting("host", "127.0.0.1"))
HTTP_PORT = int(_setting("port", 8484))

# How long a consult may run before it is killed. One flat limit cannot fit
# both a yes/no sanity check and a whole-app redesign plan: a consult's run time
# grows with what it is sent, how long it may answer, and how hard it is asked
# to think. So ADVISOR_TIMEOUT is the floor and each consult earns more on top
# of it, up to ADVISOR_TIMEOUT_MAX -- see `_consult_timeout`.
# ADVISOR_TIMEOUT_SCALE=0 restores a flat ADVISOR_TIMEOUT.
TIMEOUT = int(_setting("timeout", 300))
TIMEOUT_SCALE = max(0.0, float(_setting("timeout_scale", 1.0)))
TIMEOUT_MAX = max(TIMEOUT, int(_setting("timeout_max", 3600)))

# Seconds between keep-alive progress notifications while a consult runs. MCP
# clients abort a tool call that stays silent past their own request timeout
# (Kilo: the per-server `timeout`, 300s in our configs) but restart that clock
# on every progress notification. The heartbeat is what lets a long consult
# outlive the client's limit without anyone editing the client's config, so it
# must stay well under that limit.
PROGRESS_INTERVAL = max(1, int(_setting("progress_interval", 15)))

# In "auto" mode only, a consult that fails because the subscription's headless
# quota is exhausted may be retried on API credits. Ignored for an explicit
# "claude-code" backend: choosing the subscription is a billing decision, and
# quietly moving the user onto paid credits is not the server's call.
FALLBACK_TO_API = _flag("fallback_to_api", True)

# Optional hard spend cap handed to the CLI (`--max-budget-usd`).
MAX_BUDGET_USD = _setting("max_budget_usd")

# The ceiling on an answer's length, in words, normally set through the caller
# preset above. The prompt presents it as a ceiling, not a target, because
# every word lands in the CALLING model's context and is billed against the
# user's plan limits. `max_tokens` cannot help on the subscription backend (the
# CLI exposes no such flag), so the budget is expressed in the system prompt,
# which both backends honour. 0 removes it.
ANSWER_BUDGET = max(0, int(_setting("answer_budget", 2000)))

# Expose only the tools an agent actually needs (`ask_wisdomtooth`, `advisor_status`)
# and hide the operator tools. Every schema is charged against the calling
# model's context on every turn, and a longer tool list measurably degrades
# tool-selection accuracy in small models.
MINIMAL_TOOLS = _flag("minimal_tools", False)
ESSENTIAL_TOOLS = ("ask_wisdomtooth", "advisor_status")

# Save every consult to a file the user can open. An MCP server cannot draw
# anything in its client's window -- the tool result is the only thing it can
# put in front of a human, and the client decides how, or whether, to render
# it. Editors collapse it, a small local model paraphrases it away, and a
# context trim eventually deletes it. A file survives all three.
SAVE_CONSULTS = _flag("save_consults", True)
# Keep the newest N transcripts, so an advisor in daily use does not grow a
# directory forever. 0 keeps everything.
CONSULT_KEEP = max(0, int(_setting("consult_keep", 200)))

# The usage ledger: one JSON line per consult -- tokens, API-equivalent cost,
# duration -- so the user can see what the advisor spends against their plan's
# 5-hour and weekly limits, and so the caps below have something to count.
# Shared by every server process on the machine.
USAGE_LOG = _flag("usage_log", True)

# Optional hard caps, checked before a consult starts. 0 = no cap. Only
# consults that reached the model count; repeats and refusals are free. The
# dollar cap uses the API-rate estimate on both backends.
MAX_CONSULTS_PER_HOUR = max(0, int(_setting("max_consults_per_hour", 0)))
MAX_CONSULTS_PER_5H = max(0, int(_setting("max_consults_per_5h", 0)))
MAX_CONSULTS_PER_WEEK = max(0, int(_setting("max_consults_per_week", 0)))
MAX_USD_PER_DAY = max(0.0, float(_setting("max_usd_per_day", 0)))

# Minutes during which an identical consult returns the earlier answer instead
# of paying for it again. A looping agent re-asks word for word; the rules ask
# it not to, and this makes it free when it does anyway. 0 disables.
REPEAT_WINDOW_S = max(0.0, float(_setting("repeat_window", 30)) * 60)

# Where donations go. Empty until the maintainer sets it, and nothing is shown
# anywhere while it is empty. It appears only where a person reads -- the
# startup banner on stderr and the saved transcripts -- never in a tool result,
# which lands in the calling model's context and is the user's to spend.
SUPPORT_URL = ""
SHOW_SUPPORT = _flag("show_support", True)


# ---------------------------------------------------------------------------
# Model & effort configuration
# ---------------------------------------------------------------------------
# Tier aliases let the calling agent pick by intent instead of model strings.
# Remap or extend them with ADVISOR_TIERS_JSON / the config file's "tiers" key,
# e.g. {"deep": "claude-fable-5-1", "cheapest": "claude-haiku-4-5"}.
MODEL_TIERS = {
    "fast": "claude-haiku-4-5",     # cheap sanity checks
    "balanced": "claude-sonnet-5",  # everyday escalations
    "deep": "claude-opus-5",        # default: this is an escalation path
}


def _apply_tier_overrides() -> None:
    raw = _setting("tiers")
    if not raw:
        return
    try:
        custom = raw if isinstance(raw, dict) else json.loads(raw)
        MODEL_TIERS.update({str(k).lower(): str(v) for k, v in custom.items()})
    except Exception as exc:
        print(f"[wisdomtooth] ignoring tier overrides: {exc}", file=sys.stderr)


_apply_tier_overrides()

# Claude Code CLI model aliases for the subscription backend.
CLAUDE_CODE_ALIASES = {"fast": "haiku", "balanced": "sonnet", "deep": "opus"}

VALID_EFFORT = ("low", "medium", "high", "xhigh", "max")

# Per-model-family request capabilities, matched by longest prefix.
#   thinking: "adaptive" -> send {"type": "adaptive"}; budget_tokens is a 400
#             None       -> send no thinking config at all
#   effort:   the levels the model accepts, or () for none
_ADAPTIVE_EFFORT = ("low", "medium", "high", "xhigh", "max")
MODEL_CAPS = {
    "claude-fable-5":  ("adaptive", _ADAPTIVE_EFFORT),
    "claude-mythos-5": ("adaptive", _ADAPTIVE_EFFORT),
    "claude-opus-5":   ("adaptive", _ADAPTIVE_EFFORT),
    "claude-opus-4-8": ("adaptive", _ADAPTIVE_EFFORT),
    "claude-opus-4-7": ("adaptive", _ADAPTIVE_EFFORT),
    "claude-opus-4-6": ("adaptive", _ADAPTIVE_EFFORT),
    "claude-sonnet-5": ("adaptive", _ADAPTIVE_EFFORT),
    "claude-sonnet-4-6": ("adaptive", _ADAPTIVE_EFFORT),
    # Opus 4.5 has effort but predates xhigh/max, and predates adaptive thinking.
    "claude-opus-4-5": (None, ("low", "medium", "high")),
    # Haiku rejects `effort` outright.
    "claude-haiku-4-5": (None, ()),
}
# Longest first so "claude-opus-4-8" is never shadowed by a shorter neighbour.
_CAP_PREFIXES = sorted(MODEL_CAPS, key=len, reverse=True)

# Models that accept the server-side refusal `fallbacks` parameter, so a
# declined consult is rescued on another model instead of returning nothing.
FALLBACK_CAPABLE_PREFIXES = ("claude-opus-5", "claude-fable-5", "claude-mythos-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _tier_or_id(value: str) -> str:
    return MODEL_TIERS.get(str(value).lower(), str(value))


# The model/effort/max_tokens defaults accept a tier alias ("deep") or a full ID.
# `balanced` rather than `deep`: the caller is typically a small local model that
# escalates often, and putting every one of those on Opus exhausts a Pro plan's
# headless quota quickly. `deep` remains one argument away, per call.
DEFAULT_MODEL = _tier_or_id(_setting("model", "balanced"))

_raw_effort = str(_setting("effort") or "").lower()
DEFAULT_EFFORT = _raw_effort if _raw_effort in VALID_EFFORT else None  # None = API default

# 128k is the current output ceiling on the Opus/Sonnet/Fable families; asking
# for more is a 400, not a longer answer.
MAX_TOKENS_CEILING = 128000
MAX_TOKENS = min(int(_setting("max_tokens", 64000)), MAX_TOKENS_CEILING)

# Set ADVISOR_LOCK=1 to ignore per-call and runtime model/effort/token choices
# (hard cost control: the configured defaults always win).
LOCKED = _flag("lock", False)

# Layer 2: runtime overrides from the advisor_configure tool. Editing the MCP
# client's env requires restarting the server entry; this does not.
_OVERRIDES: dict = {}


def _effective_model() -> str:
    return _tier_or_id(_OVERRIDES.get("model", DEFAULT_MODEL))


def _effective_effort() -> Optional[str]:
    return _OVERRIDES.get("effort", DEFAULT_EFFORT)


def _effective_max_tokens() -> int:
    return min(int(_OVERRIDES.get("max_tokens", MAX_TOKENS)), MAX_TOKENS_CEILING)


def _effective_answer_budget() -> int:
    return int(_OVERRIDES.get("answer_budget", ANSWER_BUDGET))


def _answer_budget_instruction() -> str:
    """The only length control that works on the subscription backend.

    `max_tokens` is an API-only parameter; the Claude Code CLI has no
    equivalent flag, so on that backend the system prompt is the sole lever.
    """
    budget = _effective_answer_budget()
    if budget <= 0:
        return ""
    return (
        f"\nLength: keep the answer under roughly {budget} words -- a ceiling, "
        "not a target. Size the answer to what the question actually needs; "
        "most consults need a small fraction of the ceiling, and every word is "
        "billed against the user's usage limits and inserted into the context "
        "window of the agent that asked. Lead with the recommendation, keep "
        "code to the minimum that makes it concrete, and drop restatement of "
        "the question. Go long only when the problem genuinely demands it, such "
        "as a full design or migration plan. If even the ceiling is not enough, "
        "give the decisive part and say what you left out.\n")


# Adaptive thinking at high effort and above spends much of a consult reasoning
# before the first answer token appears.
_EFFORT_TIME_FACTOR = {"high": 1.5, "xhigh": 2.0, "max": 2.5}
# An unlimited answer (budget 0) is sized as if it were budgeted this many words.
_UNBUDGETED_WORDS = 4000


def _consult_timeout(prompt_chars: int, effort: Optional[str]) -> int:
    """Seconds this consult may run, sized to the work it asks for.

    ADVISOR_TIMEOUT, plus ~10s per 1,000 characters sent (system prompt,
    context and question) and ~0.06s per word of answer budget, times a factor
    for high effort, capped at ADVISOR_TIMEOUT_MAX. A 40k-character planning
    consult with a 10,000-word budget at effort=high gets ~32 minutes; a short
    question with a 600-word budget ~6. The default 64,000-word ceiling sizes
    every consult at the cap, since an answer that long can legitimately take
    most of an hour.
    """
    if not TIMEOUT_SCALE:
        return TIMEOUT
    words = _effective_answer_budget() or _UNBUDGETED_WORDS
    extra = (prompt_chars / 1000 * 10 + words * 0.06) * TIMEOUT_SCALE
    seconds = (TIMEOUT + extra) * _EFFORT_TIME_FACTOR.get(effort or "", 1.0)
    return int(min(seconds, TIMEOUT_MAX))


def _resolve_model(model: str = "") -> str:
    if LOCKED or not model:
        return _effective_model()
    return _tier_or_id(model)


def _resolve_effort(effort: str = "") -> Optional[str]:
    if LOCKED or not effort:
        return _effective_effort()
    effort = str(effort).lower()
    return effort if effort in VALID_EFFORT else _effective_effort()


def _resolve_max_tokens(max_tokens: int = 0) -> int:
    if LOCKED or not max_tokens:
        return _effective_max_tokens()
    return max(1, min(int(max_tokens), MAX_TOKENS_CEILING))


def _caps(model: str):
    for prefix in _CAP_PREFIXES:
        if model.startswith(prefix):
            return MODEL_CAPS[prefix]
    # A model released after this table was written: send nothing optional, so
    # an unknown ID degrades to a plain request instead of a 400.
    return (None, ())


def _cli_model(model: str) -> str:
    """Map a tier alias or model ID onto what `claude --model` expects."""
    if model.lower() in CLAUDE_CODE_ALIASES:
        return CLAUDE_CODE_ALIASES[model.lower()]
    for tier, model_id in MODEL_TIERS.items():
        if model == model_id:
            return CLAUDE_CODE_ALIASES[tier]
    return model  # full model names are accepted by the CLI as-is


def _supports_fallbacks(model: str) -> bool:
    return model.startswith(FALLBACK_CAPABLE_PREFIXES)


def _build_kwargs(model: str, effort: Optional[str], max_tokens: int = 0) -> dict:
    """Assemble the Messages API request body for one consult."""
    thinking, allowed_effort = _caps(model)
    kwargs: dict = {"model": model, "max_tokens": _resolve_max_tokens(max_tokens)}

    if thinking == "adaptive":
        kwargs["thinking"] = {"type": "adaptive"}

    if effort and allowed_effort:
        level = effort if effort in allowed_effort else allowed_effort[-1]
        kwargs["output_config"] = {"effort": level}

    # On adaptive-thinking models max_tokens covers thinking AND the answer, so
    # give the answer headroom at the levels that think hardest.
    if thinking == "adaptive":
        # Floors above the 64k default; `max` reaches MAX_TOKENS_CEILING.
        headroom = {"high": 80000, "xhigh": 96000,
                    "max": MAX_TOKENS_CEILING}.get(effort or "")
        if headroom:
            kwargs["max_tokens"] = max(kwargs["max_tokens"], headroom)
    return kwargs


BUILTIN_SYSTEM_PROMPT = """\
You are an expert technical advisor being consulted by another AI agent.
The agent is stuck: it has already attempted the task itself and consulted
documentation tools without success. Treat this as an escalation, not a
first question.

Guidelines:
- Be direct and concise. Lead with your recommendation, then justify it.
- Because the agent is stuck, question its framing: the most valuable advice
  is often identifying a wrong assumption in what it has already tried.
- If the question is ambiguous, state your assumptions explicitly rather
  than asking clarifying questions (the caller cannot easily reply).
- Flag risks, edge cases, or better alternatives the agent may have missed.
- If you are uncertain, say so and explain what would resolve the uncertainty.
- Never fabricate APIs, flags, or library behavior. If unsure, say "verify this".
- You have no tools and cannot read the caller's files. Answer from the context
  you were given plus your own knowledge; if something decisive is missing, name
  it and say what you would conclude either way.
"""


def _build_system_prompt() -> str:
    """The advisor persona.

    Replace it wholesale (ADVISOR_SYSTEM_PROMPT / _FILE) to make the advisor a
    domain specialist, or extend it (ADVISOR_SYSTEM_PROMPT_EXTRA) to add house
    rules without losing the escalation framing.
    """
    prompt = BUILTIN_SYSTEM_PROMPT
    path = _setting("system_prompt_file")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                prompt = fh.read()
        except Exception as exc:
            print(f"[wisdomtooth] ignoring system prompt file {path}: {exc}",
                  file=sys.stderr)
    inline = _setting("system_prompt")
    if inline:
        prompt = str(inline)
    extra = _setting("system_prompt_extra")
    if extra:
        prompt = prompt.rstrip() + "\n\n" + str(extra) + "\n"
    return prompt


ADVISOR_SYSTEM_PROMPT = _build_system_prompt()

WHEN_TO_USE = """\
WHEN TO USE THIS SERVER -- it is an ESCALATION path, not a first resort:
USE when ALL of the following are true:
  1. You are having genuine difficulty -- implementing code, understanding a
     framework, platform, operating system, build system, or protocol.
  2. You have already made at least 1-2 serious attempts yourself.
  3. Documentation tools (e.g. Context7, official docs, web search) have not
     helped significantly, OR the problem is judgment-based (architecture,
     tradeoffs, debugging strategy) where docs don't apply.
DO NOT USE for: questions you can answer yourself, simple syntax lookups,
things Context7/docs would answer directly, or trivial decisions.
ALWAYS include in `context`: what you tried, exact errors, and what the docs
said -- the advisor is stateless and sees nothing else.
"""

_server_kwargs = dict(name="wisdomtooth", instructions=WHEN_TO_USE)
if _MCP_MAJOR >= 2:
    _server_kwargs["version"] = __version__
mcp = _ServerClass(**_server_kwargs)

# Consults read nothing and change nothing locally, but they do reach an
# external service and are not idempotent -- clients use these hints to decide
# what needs an approval prompt.
CONSULT_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=False,
    openWorldHint=True,
)
LOCAL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    openWorldHint=False,
)


def tool(title: str, annotations: ToolAnnotations):
    """Register a function as an MCP tool, honouring ADVISOR_MINIMAL_TOOLS.

    A hidden tool is still a normal module-level function -- the operator can
    reach it through `advisor_status`'s sibling report or by turning minimal
    mode off, and the test suite calls the underlying helpers directly. Only
    the advertised schema shrinks.
    """
    def decorate(fn):
        if MINIMAL_TOOLS and fn.__name__ not in ESSENTIAL_TOOLS:
            return fn
        return mcp.tool(title=title, annotations=annotations)(fn)
    return decorate

_client: Optional[Anthropic] = None


def client() -> Anthropic:
    global _client
    if _client is None:
        # Bounded timeout so a stuck request errors out instead of hanging the
        # MCP tool call (the SDK default is much longer).
        _client = Anthropic(timeout=float(TIMEOUT))
    return _client


def _api_credentials_present() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # `ant auth login` stores an OAuth profile the SDK picks up with no env var
    # set, so an unset ANTHROPIC_API_KEY does not mean "no credentials".
    return os.path.isdir(os.path.join(os.path.expanduser("~"), ".config",
                                      "anthropic"))


# ---------------------------------------------------------------------------
# Subscription credentials (OAuth)
# ---------------------------------------------------------------------------
# OAuth needs a human at a browser, so the server cannot mint a credential on
# its own. What it can do is drive the official CLI flow and then *keep* the
# result, so connecting an account is a one-time act rather than a hand-edit of
# the MCP client's JSON followed by a server restart.

def _credentials_path() -> str:
    return os.path.join(_state_dir(), "credentials.json")


def _read_credentials() -> dict:
    path = _credentials_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # a broken store must never stop the server
        print(f"[wisdomtooth] ignoring {path}: {exc}", file=sys.stderr)
        return {}


def _oauth_token() -> Optional[str]:
    """The subscription token to hand the CLI, env first, then the store."""
    return (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            or _read_credentials().get("claude_code_oauth_token") or None)


def _set_oauth_token(token: str) -> str:
    """Persist a `claude setup-token` credential and apply it immediately."""
    token = (token or "").strip()
    if not token:
        raise AdvisorInputError("no token given. Run `claude setup-token` in a "
                         "terminal and pass the value it prints.")
    if len(token.split()) > 1 or token.startswith("claude "):
        raise AdvisorInputError(
            "that looks like a command, not a token. Run `claude setup-token` "
            "in a terminal and pass only the token it prints back.")

    path = _credentials_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = _read_credentials()
    data["claude_code_oauth_token"] = token
    # Create with restrictive permissions *before* writing, so the secret is
    # never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows has no POSIX modes; the user profile dir is the guard

    _invalidate_backend()
    return (f"Subscription token saved to {path} (owner-only) and applied to "
            "this server immediately — no restart needed. Consults will now "
            "bill the Claude subscription. Run advisor_status to confirm.")


def _clear_oauth_token() -> str:
    """Forget the stored token. Does not touch the CLI's own login state."""
    path = _credentials_path()
    data = _read_credentials()
    had = data.pop("claude_code_oauth_token", None)
    if data:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    elif os.path.isfile(path):
        os.remove(path)
    _invalidate_backend()
    if not had:
        return ("No advisor-stored subscription token to remove. If the CLI "
                "itself is logged in, run `claude auth logout` in a terminal.")
    return (f"Removed the stored subscription token from {path}. The Claude "
            "Code CLI's own login (if any) is untouched — run "
            "`claude auth logout` in a terminal to sign out completely.")


# ---------------------------------------------------------------------------
# Outbound-content safety: redact obvious secrets, cap runaway context
# ---------------------------------------------------------------------------
SECRET_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"), "[REDACTED:anthropic-key]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "[REDACTED:api-key]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED:github-pat]"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "[REDACTED:github-token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-key-id]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED:slack-token]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED:google-key]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED:private-key]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*[\'\"]?[^\s\'\"]{8,}"), "\\1=[REDACTED]"),
]
MAX_CONTEXT_CHARS = int(_setting("max_context_chars", 60000))

# NSFW word scrubbing: word-boundary only (never mangles class/assert/shell/
# cocktail), case-preserving, SFW replacements. Off by default -- it rewrites
# the user's text, which should be their choice. Enable: ADVISOR_NSFW_SCRUB=1.
# Extend: ADVISOR_NSFW_EXTRA_JSON=/path/to/{"word":"replacement"} file.
# Note: `review_code` deliberately skips this -- see _consult(scrub_context=).
NSFW_REPLACEMENTS = {
    "fuck": "fudge", "fucking": "fudging", "fucked": "fudged", "fucker": "fudger",
    "motherfucker": "troublemaker", "shit": "shoot", "shitty": "lousy",
    "bullshit": "nonsense", "ass": "rear", "asshole": "jerk", "bitch": "grump",
    "bitches": "grumps", "bastard": "rascal", "damn": "darn", "goddamn": "gosh-darn",
    "dick": "jerk", "cock": "rooster", "pussy": "wimp", "cunt": "meanie",
    "piss": "pee", "pissed": "annoyed", "crap": "junk", "whore": "scoundrel",
    "slut": "scoundrel", "tits": "chest", "boobs": "chest", "porn": "adult-media",
    "hell": "heck",
}


def _load_nsfw_map() -> dict:
    mapping = dict(NSFW_REPLACEMENTS)
    extra = os.environ.get("ADVISOR_NSFW_EXTRA_JSON")
    if extra and os.path.isfile(extra):
        try:
            with open(extra, encoding="utf-8") as fh:
                mapping.update({str(k).lower(): str(v) for k, v in json.load(fh).items()})
        except Exception as exc:  # a bad user file must not kill the server
            print(f"[wisdomtooth] ignoring ADVISOR_NSFW_EXTRA_JSON: {exc}",
                  file=sys.stderr)
    return mapping


_NSFW_MAP = _load_nsfw_map()
_NSFW_RE = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, _NSFW_MAP), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
) if _NSFW_MAP else None


def _match_case(replacement: str, original: str) -> str:
    if original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _scrub_nsfw(text: str) -> str:
    if _NSFW_RE is None or not _flag("nsfw_scrub", False):
        return text
    return _NSFW_RE.sub(lambda m: _match_case(_NSFW_MAP[m.group(0).lower()],
                                              m.group(0)), text)


def _truncate(text: str, limit: Optional[int] = None) -> str:
    limit = MAX_CONTEXT_CHARS if limit is None else max(int(limit), 200)
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25):]
    dropped = len(text) - len(head) - len(tail)
    return (head + f"\n\n[...advisor-server truncated {dropped} chars; pass "
            "focused excerpts instead of whole files...]\n\n" + tail)


def _sanitize(text: str, scrub: bool = True) -> str:
    """Redact secrets, optionally scrub words, then cap the size.

    Secret redaction is never optional -- `scrub` only controls the word
    substitution, which must not run over code that is being reviewed verbatim.
    """
    for pattern, repl in SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    if scrub:
        text = _scrub_nsfw(text)
    return _truncate(text)


# ---------------------------------------------------------------------------
# Files the server reads itself
# ---------------------------------------------------------------------------
# A small caller that pastes a 40k-character file into `context` spends that
# much of its own window before the advisor sees a byte. `context_files` lets
# it pass paths instead. Reading stays inside allowed folders -- ADVISOR_FILE_ROOTS,
# else the server's working directory unless that is the home folder or a drive
# root -- and credential-shaped files are refused before they are opened.
# Everything read still goes through `_sanitize`.

_MAX_CONTEXT_FILES = 20
# The server's own state directory (stored token, transcripts) is refused by
# location in `_read_context_files`, not by name here.
_DENY_DIRS = {".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker", ".git"}
_DENY_FILE = re.compile(
    r"^(\.env(\..+)?|\.netrc|\.npmrc|\.pypirc|\.git-credentials"
    r"|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|credentials(\.json)?"
    r"|.+\.(pem|key|p12|pfx|kdbx|jks|keystore))$", re.IGNORECASE)
_SAFE_SUFFIXES = (".example", ".sample", ".template")


def _too_broad(path: str) -> bool:
    real = os.path.normcase(os.path.realpath(path))
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    return real == home or os.path.dirname(real) == real


def _file_roots() -> list:
    raw = _setting("file_roots")
    if raw:
        items = raw if isinstance(raw, list) else str(raw).split(os.pathsep)
    else:
        cwd = os.getcwd()
        items = [] if _too_broad(cwd) else [cwd]
    return [os.path.realpath(os.path.expanduser(str(p).strip()))
            for p in items if str(p).strip()]


def _inside(path: str, root: str) -> bool:
    try:
        return (os.path.commonpath([os.path.normcase(path), os.path.normcase(root)])
                == os.path.normcase(root))
    except ValueError:  # different drives
        return False


def _refused(parts: list) -> bool:
    if any(p.lower() in _DENY_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    return (not name.lower().endswith(_SAFE_SUFFIXES)
            and _DENY_FILE.match(name) is not None)


def _read_context_files(paths) -> str:
    """Read the requested files into `<file>` blocks for the context."""
    if isinstance(paths, str):
        paths = [paths]
    paths = [str(p).strip() for p in paths if str(p).strip()]
    roots = _file_roots()
    if not roots:
        raise AdvisorInputError(
            "context_files is unavailable: the server's working directory "
            f"({os.getcwd()}) is the home folder or a drive root, which is too "
            "broad to expose. Paste the relevant excerpts into `context` "
            "instead, or ask the user to set ADVISOR_FILE_ROOTS to the project "
            "folder.")
    blocks, notes = [], []
    for raw in paths[:_MAX_CONTEXT_FILES]:
        candidate = raw if os.path.isabs(raw) else os.path.join(roots[0], raw)
        real = os.path.realpath(candidate)
        root = next((r for r in roots if _inside(real, r)), None)
        if root is None:
            notes.append(f"{raw}: refused, outside the allowed folders")
            continue
        rel = os.path.relpath(real, root)
        if (_inside(real, os.path.realpath(_state_dir()))
                or _refused(rel.replace("\\", "/").split("/"))):
            notes.append(f"{raw}: refused, looks like a credential file")
            continue
        if not os.path.isfile(real):
            notes.append(f"{raw}: not found")
            continue
        try:
            with open(real, "rb") as fh:
                data = fh.read(MAX_CONTEXT_CHARS * 4 + 1)
        except OSError as exc:
            notes.append(f"{raw}: unreadable ({exc.__class__.__name__})")
            continue
        if b"\0" in data[:8192]:
            notes.append(f"{raw}: skipped, binary")
            continue
        blocks.append(f'<file path="{rel}">\n'
                      + data.decode("utf-8", errors="replace") + "\n</file>")
    if len(paths) > _MAX_CONTEXT_FILES:
        notes.append(f"only the first {_MAX_CONTEXT_FILES} files were read")
    if not blocks:
        raise AdvisorInputError(
            "None of context_files could be attached: "
            + "; ".join(notes or ["no paths given"])
            + ". Allowed folders: " + ", ".join(roots) + ".")
    if notes:
        blocks.append("<file_notes>\n" + "\n".join(notes) + "\n</file_notes>")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------
# Consults are stateless by design, so continuing one means re-sending it. The
# transcript already holds exactly what was sent and what came back, so a
# follow-up names that file and the server does the re-sending -- the caller
# neither pastes the old exchange nor pays for it in its own context.

_SENT_HEADING = "## Sent to Claude\n\n"
_ANSWER_HEADING = "\n## Claude's answer\n\n"
_SUPPORT_MARK = "\n---\n\n*Wisdomtooth is free and open source."
_TRANSCRIPT_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _load_previous_consult(ref: str) -> str:
    if not SAVE_CONSULTS:
        raise AdvisorInputError(
            "follow_up_of needs saved consults, and ADVISOR_SAVE_CONSULTS=0 is "
            "set. Re-send the earlier question and answer in `context` instead.")
    name = str(ref).strip().strip("`").replace("\\", "/").rsplit("/", 1)[-1]
    if not name.endswith(CONSULT_SUFFIX):
        name += CONSULT_SUFFIX
    path = os.path.join(_consult_dir(), name)
    text = None
    if _TRANSCRIPT_NAME.match(name):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = None
    if text is None:
        raise AdvisorInputError(
            f"no saved consult named {name!r} in {_consult_dir()}. Pass the file "
            "name from an earlier answer's [saved: ...] line"
            + (f"; only the newest {CONSULT_KEEP} are kept." if CONSULT_KEEP
               else "."))
    head, found, answer = text.rpartition(_ANSWER_HEADING)
    if not found:
        raise AdvisorInputError(f"{name} is not a Wisdomtooth transcript.")
    sent = (head.partition(_SENT_HEADING)[2] or head).strip()
    answer = answer.split(_SUPPORT_MARK, 1)[0].strip()
    # At most half the context cap. Otherwise a large earlier consult fills
    # the cap and `_sanitize`'s truncation cuts from the middle -- the earlier
    # answer, which is what a follow-up is about. The earlier request gives
    # way first.
    budget = MAX_CONTEXT_CHARS // 2
    if len(sent) + len(answer) > budget:
        answer = _truncate(answer, int(budget * 0.6))
        sent = _truncate(sent, budget - len(answer))
    return ("<previous_consult>\n<earlier_request>\n" + sent
            + "\n</earlier_request>\n<your_earlier_answer>\n" + answer
            + "\n</your_earlier_answer>\n</previous_consult>\n"
            "This consult follows up on the one above.")


# ---------------------------------------------------------------------------
# Consult transcripts
# ---------------------------------------------------------------------------
# The advisor's answer reaches the user only as a tool result inside another
# agent's chat -- somewhere this server does not control and cannot re-open.
# So each consult is also written to a Markdown file, and that path travels
# back two ways: inside the answer footer, which every client renders because
# it is only text, and as a `resource_link` content block for the clients that
# turn one into something clickable. What is saved is what was actually sent,
# after secret redaction, so a transcript never becomes a second copy of a key
# this server just declined to transmit. Disable with ADVISOR_SAVE_CONSULTS=0.

CONSULT_SUFFIX = ".md"


def _consult_dir() -> str:
    """Where transcripts are written. Read per call rather than at import."""
    return os.path.abspath(str(_setting("consult_dir")
                               or os.path.join(_state_dir(), "consults")))


def _slug(text: str, limit: int = 48) -> str:
    """A filename-safe stub of the question, so the directory can be skimmed.

    Whitelist, not blacklist: this text comes from the caller and becomes part
    of a path, so everything outside [a-z0-9-] is dropped rather than escaped.
    Nothing that could act as a separator, a traversal, or a Windows reserved
    character survives, and the timestamp prefix keeps the result from ever
    being a bare device name like `con`.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug[:limit].strip("-") or "consult"


def _prune_consults(directory: str) -> None:
    """Keep the newest CONSULT_KEEP transcripts. Best effort, never fatal."""
    if not CONSULT_KEEP:
        return
    try:
        paths = [os.path.join(directory, name) for name in os.listdir(directory)
                 if name.endswith(CONSULT_SUFFIX)]
        if len(paths) <= CONSULT_KEEP:
            return
        for stale in sorted(paths, key=os.path.getmtime,
                            reverse=True)[CONSULT_KEEP:]:
            os.remove(stale)
    except OSError as exc:  # a racing or crowded directory is not an error
        print("[wisdomtooth] could not prune " + directory + ": " + str(exc),
              file=sys.stderr)


def _save_consult(kind: str, topic: str, sent: str, answer: str,
                  footer: str) -> Optional[str]:
    """Write one transcript; return its path, or None if nothing was written.

    Never raises. A read-only home or a full disk costs the user a transcript,
    which is a convenience -- it must not cost them the answer they just paid
    for.
    """
    if not SAVE_CONSULTS:
        return None
    directory = _consult_dir()
    stripped = topic.strip()
    heading = stripped.splitlines()[0][:120] if stripped else kind
    # The footer arrives as a rule plus the billing and usage lines; each is
    # rendered as its own line of inline code.
    lines = [line.strip() for line in footer.splitlines() if line.strip("- ")]
    support = _support_line()
    body = (
        "# " + kind + ": " + heading + "\n\n"
        "*" + time.strftime("%Y-%m-%d %H:%M:%S") + " - wisdomtooth "
        + __version__ + "*\n\n"
        + "".join("`" + line + "`  \n" for line in lines) + "\n"
        + _SENT_HEADING + sent + "\n"
        + _ANSWER_HEADING + answer + "\n"
        + (_SUPPORT_MARK + " If it saved you time, you can support it: "
           + support + "*\n" if support else ""))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        os.makedirs(directory, exist_ok=True)
        # Two consults can land in the same second; "x" mode makes the loser of
        # that race take the next name rather than overwrite the winner.
        for attempt in range(1, 50):
            tail = "" if attempt == 1 else "-" + str(attempt)
            path = os.path.join(
                directory,
                stamp + "-" + _slug(kind, 24) + "-" + _slug(topic) + tail
                + CONSULT_SUFFIX)
            try:
                with open(path, "x", encoding="utf-8") as fh:
                    fh.write(body)
                break
            except FileExistsError:
                continue
        else:  # pragma: no cover - 49 collisions inside one second
            return None
    except OSError as exc:
        print("[wisdomtooth] could not save the consult transcript to "
              + directory + ": " + str(exc), file=sys.stderr)
        return None
    _prune_consults(directory)
    return path


def _result_blocks(answer: str, saved: dict) -> list:
    """The tool result: the answer as text, plus a link to its transcript.

    Two blocks rather than one. The text is what the calling model reads and
    what every client renders; the `resource_link` offers the same file to the
    client as something it can put in front of the user directly, marked
    `audience=["user"]` because that is exactly who it is for. A client that
    ignores resource links loses nothing -- the path is in the footer too.

    The return annotation is deliberately omitted, here and on the tools that
    return this. Annotating a content-block list makes mcp 1.x derive an output
    schema and echo every block back a second time as structured JSON, while
    mcp 2.x suppresses the schema; leaving it off behaves identically on both.
    """
    blocks = [TextContent(type="text", text=answer)]
    path = saved.get("path")
    if path:
        blocks.append(ResourceLink(
            type="resource_link",
            uri=pathlib.Path(path).as_uri(),
            name=os.path.basename(path),
            description="Claude's full answer, saved so the user can read it "
                        "outside the chat",
            mimeType="text/markdown",
            annotations=Annotations(audience=["user"], priority=0.9),
        ))
    return blocks


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------
# Every consult is logged to a local JSON-lines ledger with the tokens it used,
# its API-equivalent cost and how long it took. The server cannot see the plan's
# own 5-hour and weekly meters, so this is the user's best view of what the
# advisor spends against them -- and what the optional caps count.

# USD per million tokens (input, output) at first-party API rates, matched by
# longest prefix. Used only to *estimate*: a subscription consult is not billed
# per token, and when the CLI reports its own API-equivalent figure that wins.
# An unlisted model gets no estimate rather than a wrong one.
PRICES_PER_MTOK = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_PRICE_PREFIXES = sorted(PRICES_PER_MTOK, key=len, reverse=True)
CACHE_READ_FACTOR = 0.1    # cache hits bill at ~0.1x the input rate
CACHE_WRITE_FACTOR = 1.25  # 5-minute cache writes at ~1.25x

# The record of the consult in flight. A context variable rather than a
# parameter: the backends' signatures are a contract tests and callers rely on,
# and `asyncio.to_thread` carries the variable into the worker thread.
_USAGE: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar(
    "wisdomtooth_usage", default=None)

_SESSION_RECORDS: list = []  # this process's records, used when the ledger is off
_LEDGER_LOCK = threading.Lock()
_LEDGER_MAX_BYTES = 2_000_000
_LEDGER_KEEP_DAYS = 35
_WINDOWS = (("1 h", 3600), ("5 h", 5 * 3600), ("24 h", 86400),
            ("7 d", 7 * 86400))


def _note_usage(**fields) -> None:
    """Record what a backend learned about the consult it just ran."""
    record = _USAGE.get()
    if record is not None:
        record.update({k: v for k, v in fields.items() if v is not None})


def _estimate_cost(model: str, input_tokens: int = 0, output_tokens: int = 0,
                   cache_read: int = 0, cache_write: int = 0) -> Optional[float]:
    model = _tier_or_id(model)
    for prefix in _PRICE_PREFIXES:
        if model.startswith(prefix):
            rate_in, rate_out = PRICES_PER_MTOK[prefix]
            return round((input_tokens * rate_in
                          + cache_read * rate_in * CACHE_READ_FACTOR
                          + cache_write * rate_in * CACHE_WRITE_FACTOR
                          + output_tokens * rate_out) / 1_000_000, 6)
    return None


def _cli_usage(payload: dict, model: str) -> dict:
    """Tokens and cost from `claude -p --output-format json`.

    `modelUsage` is preferred over `usage`: Claude Code's cost-tracking docs
    note that `usage` can under-report on some error results, while
    `modelUsage` and `total_cost_usd` keep the full figure.
    """
    per_model = payload.get("modelUsage")
    if isinstance(per_model, dict) and per_model:
        def total(key):
            return sum(int(v.get(key) or 0) for v in per_model.values()
                       if isinstance(v, dict))
        tokens = dict(input_tokens=total("inputTokens"),
                      output_tokens=total("outputTokens"),
                      cache_read_tokens=total("cacheReadInputTokens"),
                      cache_write_tokens=total("cacheCreationInputTokens"))
    else:
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        tokens = dict(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0))
    try:
        cost = float(payload["total_cost_usd"])
        source = "cli"
    except (KeyError, TypeError, ValueError):
        cost = _estimate_cost(model, tokens["input_tokens"],
                              tokens["output_tokens"],
                              tokens["cache_read_tokens"],
                              tokens["cache_write_tokens"])
        source = "estimate" if cost is not None else None
    return dict(tokens, cost_usd=cost, cost_source=source)


def _api_usage(message, model: str) -> dict:
    """Tokens from a Messages API response, costed at list prices."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return {}

    def get(name):
        try:
            return int(getattr(usage, name, 0) or 0)
        except (TypeError, ValueError):
            return 0
    tokens = dict(input_tokens=get("input_tokens"),
                  output_tokens=get("output_tokens"),
                  cache_read_tokens=get("cache_read_input_tokens"),
                  cache_write_tokens=get("cache_creation_input_tokens"))
    served_by = getattr(message, "model", None)
    cost = _estimate_cost(served_by if isinstance(served_by, str) else model,
                          tokens["input_tokens"], tokens["output_tokens"],
                          tokens["cache_read_tokens"], tokens["cache_write_tokens"])
    return dict(tokens, cost_usd=cost,
                cost_source="estimate" if cost is not None else None)


def _usage_record(kind: str, backend: str, model: str, effort: Optional[str],
                  status: str, user_content: str, **extra) -> dict:
    return dict(ts=round(time.time(), 3), time=time.strftime("%Y-%m-%dT%H:%M:%S"),
                kind=kind, backend=backend, model=model, effort=effort,
                status=status, prompt_chars=len(user_content), **extra)


def _usage_path() -> str:
    return os.path.abspath(str(_setting("usage_file")
                               or os.path.join(_state_dir(), "usage.jsonl")))


def _read_ledger(path: str) -> list:
    records = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # a torn line from a crash costs one record
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        pass
    return records


def _compact_ledger(path: str) -> None:
    cutoff = time.time() - _LEDGER_KEEP_DAYS * 86400
    keep = [r for r in _read_ledger(path) if float(r.get("ts", 0)) >= cutoff]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for record in keep:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    os.replace(tmp, path)


def _append_usage(record: dict) -> None:
    """Add one consult to the ledger. Never raises: accounting must not cost
    the user the answer."""
    with _LEDGER_LOCK:
        _SESSION_RECORDS.append(record)
        del _SESSION_RECORDS[:-1000]
    if not USAGE_LOG:
        return
    path = _usage_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _LEDGER_LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
            if os.path.getsize(path) > _LEDGER_MAX_BYTES:
                _compact_ledger(path)
    except OSError as exc:
        print(f"[wisdomtooth] could not write the usage ledger {path}: {exc}",
              file=sys.stderr)


def _usage_records(since: float) -> list:
    if USAGE_LOG:
        source = _read_ledger(_usage_path())
    else:
        with _LEDGER_LOCK:
            source = list(_SESSION_RECORDS)
    return [r for r in source if float(r.get("ts", 0)) >= since]


def _billed(records: list) -> list:
    """Records whose tokens were spent: successes, and failures the backend
    still reported usage for (a CLI run can fail after the model ran)."""
    return [r for r in records if r.get("status") in ("ok", "error")]


def _fmt_usd(cost: float) -> str:
    """Cents, or enough digits that a sub-cent consult does not read as free."""
    return f"${cost:.2f}" if cost >= 0.01 or not cost else f"${cost:.4f}"


def _summarise(records: list) -> dict:
    ok = [r for r in records if r.get("status") == "ok"]
    billed = _billed(records)
    costs = [float(r["cost_usd"]) for r in billed
             if isinstance(r.get("cost_usd"), (int, float))]
    times = [float(r["duration_s"]) for r in ok
             if isinstance(r.get("duration_s"), (int, float))]
    return {
        "consults": len(ok),
        "repeats": sum(1 for r in records if r.get("status") == "repeat"),
        "errors": sum(1 for r in records if r.get("status") == "error"),
        "input": sum(int(r.get("input_tokens") or 0)
                     + int(r.get("cache_read_tokens") or 0)
                     + int(r.get("cache_write_tokens") or 0) for r in billed),
        "output": sum(int(r.get("output_tokens") or 0) for r in billed),
        "cost": round(sum(costs), 4) if costs else None,
        "avg_s": sum(times) / len(times) if times else None,
    }


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _usage_footer(record: dict) -> str:
    """One line for the answer footer: what this consult used."""
    tokens_in = sum(int(record.get(k) or 0) for k in
                    ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
    tokens_out = int(record.get("output_tokens") or 0)
    if not (tokens_in or tokens_out):
        return ""
    text = (f"\n[usage: {tokens_in:,} tokens in · {tokens_out:,} out · "
            f"{record.get('duration_s', 0):g}s")
    cost = record.get("cost_usd")
    if isinstance(cost, (int, float)):
        text += f" · ≈{_fmt_usd(cost)} at API rates"
    return text + "]"


def _caps_description() -> str:
    parts = [f"{n}/{label}" for n, label in (
        (MAX_CONSULTS_PER_HOUR, "hour"), (MAX_CONSULTS_PER_5H, "5h"),
        (MAX_CONSULTS_PER_WEEK, "week")) if n]
    if MAX_USD_PER_DAY:
        parts.append(f"${MAX_USD_PER_DAY:.2f}/day")
    return ", ".join(parts)


def _check_caps() -> None:
    """Refuse a consult that would break an operator-set cap. Spends nothing."""
    caps = ((3600, MAX_CONSULTS_PER_HOUR, "ADVISOR_MAX_CONSULTS_PER_HOUR", "hour"),
            (5 * 3600, MAX_CONSULTS_PER_5H, "ADVISOR_MAX_CONSULTS_PER_5H",
             "5 hours"),
            (7 * 86400, MAX_CONSULTS_PER_WEEK, "ADVISOR_MAX_CONSULTS_PER_WEEK",
             "7 days"))
    if not MAX_USD_PER_DAY and not any(cap for _, cap, _, _ in caps):
        return
    now = time.time()
    recent = _usage_records(now - 7 * 86400)
    records = [r for r in recent if r.get("status") == "ok"]
    for span, cap, var, label in caps:
        if not cap:
            continue
        inside = sorted(float(r["ts"]) for r in records
                        if float(r["ts"]) >= now - span)
        if len(inside) >= cap:
            opens = time.strftime(
                "%H:%M", time.localtime(inside[len(inside) - cap] + span))
            raise AdvisorError(
                f"Consult cap reached: {len(inside)} consults in the last "
                f"{label}, and the limit is {cap} ({var}). No consult was made "
                f"and nothing was spent. The next slot opens around {opens}. "
                f"Tell the user; do NOT retry. They can raise or unset {var}.")
    if MAX_USD_PER_DAY:
        spent = sum(float(r.get("cost_usd") or 0) for r in _billed(recent)
                    if float(r["ts"]) >= now - 86400)
        if spent >= MAX_USD_PER_DAY:
            raise AdvisorError(
                f"Daily spend cap reached: consults in the last 24 hours come "
                f"to ≈{_fmt_usd(spent)} at API rates, and the limit is "
                f"${MAX_USD_PER_DAY:.2f} (ADVISOR_MAX_USD_PER_DAY). No consult "
                "was made. Tell the user; do NOT retry.")


def _usage_line() -> str:
    """The one-line summary advisor_status shows."""
    now = time.time()
    records = _usage_records(now - 7 * 86400)
    parts = []
    for label, span in (("5 h", 5 * 3600), ("7 d", 7 * 86400)):
        s = _summarise([r for r in records if float(r["ts"]) >= now - span])
        parts.append(f"{label}: {s['consults']} consults, "
                     f"{_fmt_tokens(s['input'])} in / {_fmt_tokens(s['output'])} out"
                     + (f", ≈{_fmt_usd(s['cost'])}" if s["cost"] is not None
                        else ""))
    return "; ".join(parts) + " (API-rate estimate)"


def _usage_report(days: int = 7) -> str:
    try:
        days = max(1, min(int(days or 7), _LEDGER_KEEP_DAYS))
    except (TypeError, ValueError):
        days = 7
    now = time.time()
    records = _usage_records(now - max(days, 7) * 86400)
    where = (_usage_path() if USAGE_LOG
             else "disabled (ADVISOR_USAGE_LOG=0), so this server process only")
    lines = [f"USAGE LEDGER: {where}", "",
             f"{'window':<8}{'consults':>9}{'repeats':>9}{'errors':>8}"
             f"{'tokens in':>11}{'tokens out':>12}{'≈ cost':>10}{'avg time':>10}"]
    for label, span in _WINDOWS:
        s = _summarise([r for r in records if float(r["ts"]) >= now - span])
        cost = _fmt_usd(s["cost"]) if s["cost"] is not None else "-"
        avg = f"{s['avg_s']:.0f}s" if s["avg_s"] is not None else "-"
        lines.append(f"{label:<8}{s['consults']:>9}{s['repeats']:>9}"
                     f"{s['errors']:>8}{_fmt_tokens(s['input']):>11}"
                     f"{_fmt_tokens(s['output']):>12}{cost:>10}{avg:>10}")
    by_model: dict = {}
    for r in records:
        if float(r["ts"]) >= now - days * 86400 and r.get("status") == "ok":
            by_model.setdefault(r.get("model") or "?", []).append(r)
    lines += ["", f"BY MODEL, last {days} day(s):"]
    if not by_model:
        lines.append("  (no consults)")
    for model, rows in sorted(by_model.items()):
        s = _summarise(rows)
        lines.append(f"  {model:<22} {s['consults']:>4} consults  "
                     f"{_fmt_tokens(s['input'])} in / {_fmt_tokens(s['output'])} out"
                     + (f"  ≈{_fmt_usd(s['cost'])}" if s["cost"] is not None
                        else ""))
    lines += [
        "",
        "CAPS: " + (_caps_description() or "none set (ADVISOR_MAX_CONSULTS_PER_"
                    "HOUR / _5H / _WEEK, ADVISOR_MAX_USD_PER_DAY)"),
        "REPEAT GUARD: " + (f"identical consults within {REPEAT_WINDOW_S / 60:g} "
                            "min return the saved answer at no cost"
                            if REPEAT_WINDOW_S else "off"),
        "",
        "Cost is what these tokens would cost at API rates. Subscription "
        "consults are not billed per token, but this is a fair proxy for how "
        "much of the plan's 5-hour and weekly allowance they use. The plan's "
        "own meter is not visible to this server."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Repeat guard
# ---------------------------------------------------------------------------

_RECENT: dict = {}
_RECENT_LOCK = threading.Lock()


def _repeat_key(*parts) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8", errors="replace") + b"\0")
    return digest.hexdigest()


def _recall(key: str) -> Optional[dict]:
    if not REPEAT_WINDOW_S:
        return None
    with _RECENT_LOCK:
        hit = _RECENT.get(key)
    if hit and time.time() - hit["ts"] <= REPEAT_WINDOW_S:
        return hit
    return None


def _remember(key: str, **entry) -> None:
    if not REPEAT_WINDOW_S:
        return
    now = time.time()
    with _RECENT_LOCK:
        for stale in [k for k, v in _RECENT.items()
                      if now - v["ts"] > REPEAT_WINDOW_S]:
            del _RECENT[stale]
        _RECENT[key] = dict(entry, ts=now)


def _support_line() -> str:
    return SUPPORT_URL if SUPPORT_URL and SHOW_SUPPORT else ""


# ---------------------------------------------------------------------------
# Subprocess plumbing for the Claude Code CLI
# ---------------------------------------------------------------------------

class UsageLimitError(AdvisorError):
    """The subscription's headless quota is exhausted. Not retryable."""


def _claude_bin() -> Optional[str]:
    return os.environ.get("ADVISOR_CLAUDE_BIN") or shutil.which("claude")


def _kill_tree(proc: "subprocess.Popen") -> None:
    """Kill the process AND its descendants.

    Plain kill() only takes out the direct child; with `cmd /c claude.cmd` the
    grandchild survives, keeps the stdout pipe open, and wedges communicate()
    forever -- which is how a "timeout" can still hang on Windows.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, stdin=subprocess.DEVNULL)
    else:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()


class _Cancellation:
    """Lets the async tool wrapper stop a consult running in a worker thread.

    `asyncio.to_thread` cannot interrupt its thread, so a client that gave up
    on a consult used to leave `claude` running -- and spending quota -- until
    it finished or timed out. The wrapper puts one of these in a context
    variable, which `to_thread` copies into the worker; `_run_claude` registers
    each process it starts, and `cancel` kills it.
    """

    def __init__(self):
        self.cancelled = False
        self.proc = None
        self._lock = threading.Lock()

    def attach(self, proc) -> bool:
        """Register `proc`; False if the consult was already cancelled."""
        with self._lock:
            self.proc = proc
            return not self.cancelled

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            proc = self.proc
        if proc is not None and proc.poll() is None:
            _kill_tree(proc)


_CANCELLATION: "contextvars.ContextVar[Optional[_Cancellation]]" = (
    contextvars.ContextVar("wisdomtooth_cancellation", default=None))


def _wrap_for_windows(cmd: list) -> list:
    """npm-installed Claude Code resolves to claude.cmd/.bat, which
    CreateProcess cannot exec directly -- route those through cmd /c."""
    if os.name == "nt" and cmd and cmd[0].lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c"] + cmd
    return cmd


def _run_claude(cmd, env=None, workdir=None, timeout_s=60, stdin_text=""):
    """Run the CLI with the prompt on stdin and a hard timeout."""
    popen_kwargs = dict(
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        env=env if env is not None else os.environ.copy(),
        cwd=workdir or os.path.expanduser("~"),
    )
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True  # own process group for killpg

    proc = subprocess.Popen(_wrap_for_windows(list(cmd)), **popen_kwargs)
    holder = _CANCELLATION.get()
    if holder is not None and not holder.attach(proc):
        _kill_tree(proc)
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        raise subprocess.TimeoutExpired(cmd, timeout_s, output=out, stderr=err)
    if holder is not None and holder.cancelled:
        raise AdvisorError("The consult was cancelled by the client, and the "
                           "claude process was stopped.")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


_FEATURE_CACHE: dict = {}


def _cli_features(claude_bin: str) -> set:
    """Which optional flags this CLI build accepts, read from `--help`.

    Claude Code updates itself, so flags come and go. Probing once per binary
    is cheaper than discovering a removed flag as a failed consult.
    """
    if claude_bin in _FEATURE_CACHE:
        return _FEATURE_CACHE[claude_bin]
    flags = set()
    try:
        result = _run_claude([claude_bin, "--help"], timeout_s=30)
        text = (result.stdout or "") + (result.stderr or "")
        for flag in ("--tools", "--system-prompt", "--effort", "--output-format",
                     "--strict-mcp-config", "--no-session-persistence",
                     "--disable-slash-commands", "--permission-prompts",
                     "--setting-sources", "--max-budget-usd"):
            if flag in text:
                flags.add(flag)
        # `--help` documents this one as "--system-prompt[-file]" rather than
        # listing it separately, so match either spelling.
        if "--system-prompt-file" in text or "--system-prompt[-file]" in text:
            flags.add("--system-prompt-file")
    except Exception as exc:  # a failed probe must not block the consult
        print(f"[wisdomtooth] could not probe `claude --help`: {exc}",
              file=sys.stderr)
    # Only a probe that found something is cached. A timed-out or broken probe
    # would otherwise pin this process to a bare command line -- no JSON
    # output, no usage figures, no `--tools ""` -- until it restarts.
    if flags:
        _FEATURE_CACHE[claude_bin] = flags
    return flags


def _child_env() -> dict:
    """The environment for the CLI subprocess.

    ANTHROPIC_API_KEY (API billing) and ANTHROPIC_AUTH_TOKEN (gateway bearer)
    are removed, because when either is present Claude Code prioritizes it and
    bills the developer API account instead of the subscription.
    CLAUDE_CODE_OAUTH_TOKEN is KEPT: it is the long-lived headless
    subscription credential minted by `claude setup-token`.
    """
    hijack = () if os.environ.get("ADVISOR_KEEP_AUTH_ENV") == "1" else (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    env = {k: v for k, v in os.environ.items() if k not in hijack}
    # Inject the stored subscription token: a GUI-launched MCP client hands us
    # a reduced environment, so inheriting it is not enough.
    token = _oauth_token()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def _system_prompt_file(system: str) -> str:
    """Write this consult's system prompt to a file of its own; return the path.

    One file per consult. A shared path is rewritten by every concurrent
    consult -- a parallel review_code, or another client's server process --
    so a CLI reading it a moment later could get another consult's prompt, or
    a half-written one. The caller deletes it once the CLI exits. Kept beside
    the workdir, not in it, so the CLI never sees it as project content.
    """
    directory = os.path.join(_state_dir(), "prompts")
    os.makedirs(directory, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="system-prompt-", suffix=".txt",
                                dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(system)
    return path


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _workdir() -> str:
    """A dedicated empty directory for consults.

    Keeps Claude Code from loading CLAUDE.md and project state from whatever
    cwd the MCP client happened to use, and avoids first-run trust prompts.
    """
    path = os.path.join(_state_dir(), "workdir")
    os.makedirs(path, exist_ok=True)
    return path


_AUTH_SIGNATURES = ("/login", "not logged in", "invalid api key", "authentication",
                    "oauth", "token expired", "credential", "unauthorized",
                    "please log in")
_LIMIT_SIGNATURES = ("usage limit", "rate limit", "quota", "limit reached",
                     "upgrade to", "resets at")

_AUTH_HELP = (
    "AUTH FAILURE from the claude CLI -- this CANNOT be fixed headlessly or by "
    "retrying; OAuth login requires a human. Tell the user to do ONE of the "
    "following on this machine: (a) open a terminal, run `claude`, then "
    "`/login`, choose the claude.ai (Pro/Max) account, and confirm "
    "`claude auth status` shows it; or (b) for durable headless auth, run "
    "`claude setup-token` once and put the resulting token in the MCP server "
    "env as CLAUDE_CODE_OAUTH_TOKEN. Also ensure no ANTHROPIC_API_KEY lingers "
    "in shell profiles. Then re-run advisor_auth_check. CLI said: "
)


def _consult_claude_code(system: str, user_content: str, model: str,
                         effort: Optional[str]) -> str:
    """Route the question through the local Claude Code CLI (headless).

    Bills subscription entitlements as long as Claude Code is logged in with a
    claude.ai account. The prompt goes on stdin, never argv: Windows caps a
    command line at ~32k characters (~8k through cmd.exe) and the context cap
    alone is 60k.
    """
    claude_bin = _claude_bin()
    if not claude_bin:
        raise AdvisorError(
            "the `claude` CLI was not found on PATH. Install Claude Code and "
            "log in with your Pro/Max account, or set ADVISOR_CLAUDE_BIN to "
            "its absolute path (GUI editors often have a reduced PATH). To "
            "use API credits instead, set ADVISOR_BACKEND=api and provide "
            "ANTHROPIC_API_KEY."
        )

    features = _cli_features(claude_bin)
    cmd = [claude_bin, "-p", "--model", _cli_model(model)]

    def add(flag, value=None):
        if flag in features:
            cmd.append(flag)
            if value is not None:
                cmd.append(value)

    add("--output-format", "json")
    # A hard flag, not a system-prompt request: the advisor has no business
    # touching the filesystem, and tool turns cost latency and quota.
    add("--tools", "")
    # Replace rather than append: the built-in coding-agent prompt is ~12k
    # tokens of framing that is wrong for an advisor and billed every call.
    # Prefer the file form -- the prompt is multi-line, and a multi-line argv
    # element is truncated at the first newline when an npm-installed CLI is
    # invoked through `cmd /c claude.cmd`.
    prompt_file = None
    if "--system-prompt-file" in features:
        prompt_file = _system_prompt_file(system)
        cmd += ["--system-prompt-file", prompt_file]
    elif "--system-prompt" in features:
        cmd += ["--system-prompt", system]
    else:  # pragma: no cover - only on a CLI too old to have either
        cmd += ["--append-system-prompt", system]
    if effort:
        add("--effort", effort)
    if MAX_BUDGET_USD:
        add("--max-budget-usd", str(MAX_BUDGET_USD))
    # Isolate from the user's global MCP servers: without this every consult
    # boots ALL user-scope servers (slow), and if THIS advisor is registered at
    # user scope the consult would recurse into itself.
    if os.environ.get("ADVISOR_NO_STRICT_MCP") != "1":
        add("--strict-mcp-config")
    add("--no-session-persistence")   # a stateless consult leaves no transcript
    add("--disable-slash-commands")
    add("--permission-prompts", "none")
    add("--setting-sources", "")      # ignore user/project/local settings

    timeout_s = _consult_timeout(len(system) + len(user_content), effort)
    try:
        result = _run_claude(cmd, _child_env(), _workdir(), timeout_s,
                             user_content)
    except subprocess.TimeoutExpired as exc:
        tail = (exc.stderr or "")
        tail = tail.decode(errors="replace") if isinstance(tail, bytes) else tail
        raise AdvisorError(
            f"claude CLI produced no answer within {timeout_s}s and was killed "
            f"(limit sized for {len(user_content):,} characters of input, an "
            f"answer budget of {_effective_answer_budget() or 'unlimited'} "
            "words" + (f" and effort={effort}" if effort else "") + "). "
            "Most likely causes, in order: (1) Claude Code is not logged in -- "
            "run `claude` in a terminal, then `/login` with the claude.ai "
            "(Pro/Max) account; (2) a first-run onboarding/trust prompt is "
            "blocking -- run `claude` interactively once on this machine; "
            "(3) network issues; (4) the consult genuinely needs longer -- the "
            f"user can raise ADVISOR_TIMEOUT_MAX (now {TIMEOUT_MAX}s) or lower "
            "ADVISOR_ANSWER_BUDGET. Do NOT retry in a loop; surface this to the "
            "user. stderr tail: " + tail[-400:]
        ) from exc
    finally:
        if prompt_file:
            _remove_quietly(prompt_file)

    blob = ((result.stderr or "") + " " + (result.stdout or "")).lower()

    answer, payload = _parse_cli_output(result.stdout or "")
    if payload:
        # Before the failure checks: a failed run can still have been billed.
        _note_usage(**_cli_usage(payload, model))
    failed = result.returncode != 0 or (payload or {}).get("is_error")

    if failed:
        detail = (answer or result.stderr or result.stdout or "").strip()[:500]
        if any(sig in blob for sig in _LIMIT_SIGNATURES):
            raise UsageLimitError(
                "The Claude subscription's headless usage limit is exhausted, so "
                "this consult could not run on the plan. This is not a bug and "
                "retrying will not help -- tell the user, and let them decide "
                "whether to wait for the reset or spend API credits. CLI said: "
                + detail)
        if any(sig in blob for sig in _AUTH_SIGNATURES):
            raise AdvisorError(_AUTH_HELP + detail)
        raise AdvisorError(
            f"claude CLI failed (exit {result.returncode}). " + detail)

    if not answer.strip():
        raise AdvisorError(
            "claude CLI exited successfully but returned an empty answer. "
            "stderr: " + (result.stderr or "").strip()[:400])
    return answer.strip()


def _parse_cli_output(stdout: str):
    """Return (answer_text, parsed_json_or_None).

    Falls back to raw stdout so a CLI whose JSON shape has changed still yields
    an answer rather than an error.
    """
    text = stdout.strip()
    if not text.startswith("{"):
        return text, None
    try:
        payload = json.loads(text)
    except ValueError:
        return text, None
    if not isinstance(payload, dict):
        return text, None
    return str(payload.get("result", "")), payload


# ---------------------------------------------------------------------------
# The Anthropic API backend
# ---------------------------------------------------------------------------

def _extract_text(message) -> str:
    return "".join(b.text for b in message.content
                   if getattr(b, "type", None) == "text")


def _stream(messages_api, **kwargs):
    """Every request is streamed.

    max_tokens here reaches 128k on high-effort adaptive-thinking models, and
    the SDKs require streaming above roughly 8k output tokens or the request
    trips the HTTP timeout.

    Events are consumed here rather than inside `get_final_message` so that a
    consult the client cancelled stops at the next event: leaving the `with`
    block closes the HTTP stream, and the model stops generating billed tokens.
    """
    holder = _CANCELLATION.get()
    with messages_api.stream(**kwargs) as stream:
        if holder is not None and hasattr(stream, "__iter__"):
            for _event in stream:
                if holder.cancelled:
                    raise AdvisorError("The consult was cancelled by the "
                                       "client, and the API request was closed.")
        return stream.get_final_message()


def _consult_api(system: str, user_content: str, model: str,
                 effort: Optional[str], max_tokens: int = 0) -> str:
    if not _api_credentials_present():
        raise AdvisorError(
            "the API backend needs credentials: ANTHROPIC_API_KEY is missing. "
            "This bills the developer Console account per token -- get a key at "
            "console.anthropic.com and set it in the MCP server config env, "
            "then restart the server entry. If the user meant to use their "
            "Claude SUBSCRIPTION instead, install Claude Code, run `claude` "
            "and `/login`, and leave ADVISOR_BACKEND unset (auto)."
        )

    kwargs = _build_kwargs(model, effort, max_tokens)
    messages = [{"role": "user", "content": user_content}]
    api = client()

    message = None
    if _supports_fallbacks(model):
        # A refused consult should be rescued on another model rather than
        # returning nothing. Degrade quietly if the installed SDK is older than
        # the parameter, or the account/platform does not offer it.
        try:
            message = _stream(api.beta.messages, system=system, messages=messages,
                              betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        except (TypeError, anthropic.BadRequestError, anthropic.NotFoundError,
                anthropic.PermissionDeniedError):
            message = None

    if message is None:
        try:
            message = _stream(api.messages, system=system, messages=messages,
                              **kwargs)
        except anthropic.AuthenticationError as exc:
            raise AdvisorError(
                "AUTH FAILURE on the API backend: ANTHROPIC_API_KEY is missing, "
                "invalid, or revoked. Verify it at console.anthropic.com and set "
                "it in the MCP server config env, then restart the server entry. "
                "If the user intended SUBSCRIPTION billing, use "
                "ADVISOR_BACKEND=claude-code (no API key needed). Do not retry "
                f"until fixed. ({exc.__class__.__name__})") from exc
        except anthropic.BadRequestError:
            # Capability drift (new or renamed models): retry as a plain request.
            kwargs.pop("output_config", None)
            kwargs.pop("thinking", None)
            message = _stream(api.messages, system=system, messages=messages,
                              **kwargs)

    _note_usage(**_api_usage(message, model))
    if getattr(message, "stop_reason", None) == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) or "unspecified"
        explanation = getattr(details, "explanation", None) or ""
        return (f"[The advisor declined to answer this request (refusal "
                f"category: {category}). {explanation} Rephrase the question "
                "around the engineering problem, or answer it yourself -- do "
                "not retry unchanged.]")

    answer = _extract_text(message)
    if not answer.strip():
        return ("[advisor returned no visible text -- the response likely hit "
                f"max_tokens ({kwargs['max_tokens']}) during extended thinking. "
                "Raise ADVISOR_MAX_TOKENS or lower effort, then retry ONCE.]")
    return answer


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------

_ACTIVE_BACKEND: Optional[str] = None


def _cli_auth_status() -> Optional[dict]:
    """`claude auth status --json`, or None if it cannot be read."""
    claude_bin = _claude_bin()
    if not claude_bin:
        return None
    try:
        result = _run_claude([claude_bin, "auth", "status", "--json"], timeout_s=30)
        text = (result.stdout or "").strip()
        return json.loads(text) if text.startswith("{") else None
    except Exception:
        return None


def _invalidate_backend() -> None:
    """Force the next `auto` resolution to re-probe.

    Connecting or disconnecting an account changes the answer, and the cached
    value would otherwise keep the server on the old billing path until it
    restarts -- which is precisely the friction the login flow exists to remove.
    """
    global _ACTIVE_BACKEND
    _ACTIVE_BACKEND = None


def _active_backend() -> str:
    """Which account pays, resolved once per process.

    Returns "claude-code", "api", or "unavailable".
    """
    global _ACTIVE_BACKEND
    if BACKEND in _BACKENDS:
        return BACKEND  # an explicit choice is never second-guessed
    if _ACTIVE_BACKEND is not None:
        return _ACTIVE_BACKEND

    status = _cli_auth_status()
    if (status and status.get("loggedIn")) or (_oauth_token() and _claude_bin()):
        _ACTIVE_BACKEND = "claude-code"
    elif _api_credentials_present():
        _ACTIVE_BACKEND = "api"
    else:
        _ACTIVE_BACKEND = "unavailable"
    return _ACTIVE_BACKEND


# ---------------------------------------------------------------------------
# Connecting an account
# ---------------------------------------------------------------------------

# Terminal emulators to try, in order, for the interactive OAuth flow. The CLI
# needs a real console it can own -- piping its stdio would strand the user
# halfway through a browser handshake with nothing to type into.
def _spawn_login_console(cmd: list) -> str:
    """Open `cmd` in a console window the user can see and interact with.

    Returns a short description of what was opened. Raises if the machine has
    no desktop to put a window on (headless server, container, plain SSH).
    """
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen(_wrap_for_windows(list(cmd)), creationflags=flags,
                         close_fds=True)
        return "a new console window"

    if sys.platform == "darwin":
        script = " ".join(f"'{part}'" for part in cmd)
        subprocess.Popen(
            ["osascript", "-e",
             f'tell application "Terminal" to do script "{script}"',
             "-e", 'tell application "Terminal" to activate'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "a new Terminal window"

    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError("no graphical session")
    for term, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"),
                       ("konsole", "-e"), ("xfce4-terminal", "-x"),
                       ("xterm", "-e")):
        if shutil.which(term):
            subprocess.Popen([term, flag] + list(cmd), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"a new {term} window"
    raise RuntimeError("no terminal emulator found")


# Seconds between "is the browser sign-in done yet?" checks. A module constant
# so tests can drive the poll loop without patching the stdlib.
_LOGIN_POLL_SECONDS = 3.0


def _describe_account(status: dict) -> str:
    return "account={} method={} plan={}".format(
        status.get("email", "?"), status.get("authMethod", "?"),
        status.get("subscriptionType", "?"))


_MANUAL_LOGIN = (
    "Connect the account manually instead — either works:\n"
    "  A. Interactive (simplest if you have a terminal on this machine):\n"
    "       claude auth login --claudeai\n"
    "     Complete the browser sign-in, then call advisor_login again to "
    "confirm.\n"
    "  B. Headless / GUI-launched editor (survives everything, recommended for "
    "a server your editor starts):\n"
    "       claude setup-token\n"
    "     Then pass the token it prints to the advisor_set_token tool. The "
    "advisor stores it privately and uses it immediately — no MCP config edit "
    "and no server restart."
)


def _login(force: bool = False, wait_seconds: int = 180) -> str:
    """Connect a Claude subscription account through the official OAuth flow."""
    if not _claude_bin():
        raise AdvisorError(
            "Connecting a Claude subscription needs the Claude Code CLI, which "
            "is not on PATH. Install it from https://claude.com/claude-code (or "
            "set ADVISOR_CLAUDE_BIN to its absolute path if it is installed but "
            "a GUI-launched editor cannot see it), then call advisor_login "
            "again. Without it the advisor can only use pay-per-token API "
            "credits via ANTHROPIC_API_KEY.")

    status = _cli_auth_status() or {}
    connected = bool(status.get("loggedIn"))
    method = status.get("authMethod")

    if connected and not force:
        if method and method != "claude.ai":
            return (
                f"The Claude Code CLI is logged in, but with '{method}' rather "
                "than a claude.ai subscription — consults would bill an API "
                "account per token, not the subscription. To switch, run "
                "`claude auth logout` in a terminal and then call advisor_login "
                "again (or advisor_login with force=true).\n"
                f"Current: {_describe_account(status)}")
        _invalidate_backend()
        return ("Already connected — nothing to do. Consults bill the Claude "
                f"subscription.\nCurrent: {_describe_account(status)}\n"
                f"Active backend: {_active_backend()}")

    cmd = [_claude_bin(), "auth", "login", "--claudeai"]
    try:
        where = _spawn_login_console(cmd)
    except Exception as exc:
        return ("Could not open a console for the sign-in flow on this machine "
                f"({exc}). This is normal on a headless server, in a container, "
                "or over plain SSH.\n\n" + _MANUAL_LOGIN)

    opened = (f"Opened {where} running `claude auth login --claudeai`. "
              "Complete the sign-in in your browser, choosing the claude.ai "
              "account whose Pro/Max subscription you want the advisor to use.")

    deadline = time.time() + max(0, int(wait_seconds))
    holder = _CANCELLATION.get()
    while time.time() < deadline:
        time.sleep(_LOGIN_POLL_SECONDS)
        if holder is not None and holder.cancelled:
            break  # the client gave up waiting; nobody reads the result
        status = _cli_auth_status() or {}
        if status.get("loggedIn"):
            _invalidate_backend()
            return (f"{opened}\n\nConnected. {_describe_account(status)}\n"
                    f"Active backend: {_active_backend()} — consults now bill "
                    "the Claude subscription. No restart needed.")

    return (f"{opened}\n\nNot connected yet after {wait_seconds}s — the sign-in "
            "is probably still open. Finish it in the browser, then call "
            "advisor_login again to confirm (it will not re-open the window "
            "once the account is connected).\n\nIf the window did not appear:\n"
            + _MANUAL_LOGIN)


_NO_CREDENTIALS = (
    "No usable Claude credentials on this machine, so the advisor cannot run.\n"
    "EASIEST FIX -- call the `advisor_login` tool. It opens the Claude sign-in "
    "on the user's desktop and connects their SUBSCRIPTION (Pro/Max), which "
    "costs nothing per token. It applies immediately: no config edit, no server "
    "restart.\n"
    "If this machine has no desktop (headless, container, SSH), have the user "
    "run `claude setup-token` in a terminal and pass the result to the "
    "`advisor_set_token` tool.\n"
    "Alternative, only if the user has no Claude subscription: set "
    "ANTHROPIC_API_KEY in the MCP server env and restart the server entry -- "
    "that bills their developer Console account per token.\n"
    "A human has to complete one of these -- do not retry the consult until "
    "they have."
)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
# The seam for other frontier providers. A backend is a name the user can put
# in ADVISOR_BACKEND, the account it bills (for the footer and advisor_status),
# a readiness check, and one function with `_consult_api`'s signature that
# returns the answer text. Launch ships the two Claude backends; a ChatGPT or
# Kimi backend registers the same way, and reports its tokens through
# `_note_usage` so the ledger, the caps and the repeat guard cover it too.

@dataclass(frozen=True)
class Backend:
    name: str
    provider: str
    billing: str
    consult: Callable[[str, str, str, Optional[str], int], str]
    available: Callable[[], bool]


_BACKENDS: dict = {}


def register_backend(backend: Backend) -> None:
    """Make `backend` selectable with ADVISOR_BACKEND=<backend.name>."""
    _BACKENDS[backend.name] = backend


# Lambdas rather than the functions themselves: they look the function up at
# call time, so a replaced module attribute (tests, embedding) is honoured.
register_backend(Backend(
    name="claude-code", provider="anthropic",
    billing="Claude SUBSCRIPTION (Pro/Max) via the local Claude Code CLI",
    consult=lambda s, u, m, e, t: _consult_claude_code(s, u, m, e),
    available=lambda: bool(_claude_bin())))
register_backend(Backend(
    name="api", provider="anthropic",
    billing="API ACCOUNT (pay-per-token via ANTHROPIC_API_KEY)",
    consult=lambda s, u, m, e, t: _consult_api(s, u, m, e, t),
    available=lambda: _api_credentials_present()))


def _consult(question: str, context: str = "", extra_system: str = "",
             model: str = "", effort: str = "", max_tokens: int = 0,
             scrub_context: bool = True, kind: str = "consult",
             saved: Optional[dict] = None,
             context_files: Optional[list] = None,
             follow_up_of: str = "") -> str:
    """One stateless consult, on whichever backend is active.

    `kind` names the calling tool, for the transcript. `saved` is an optional
    out-parameter: pass a dict and the transcript path lands in it under
    "path", so the tool wrapper can attach a `resource_link` to the same file.
    An out-parameter rather than a richer return type because the answer string
    IS this function's contract -- every caller, and most of the test suite,
    treats it as one.

    `follow_up_of` re-sends an earlier saved exchange and `context_files` are
    read by the server itself. Both land in `context`, so both pass through the
    same redaction and size cap as anything the caller pasted.
    """
    system = (ADVISOR_SYSTEM_PROMPT + ("\n" + extra_system if extra_system else "")
              + _answer_budget_instruction())
    question = _scrub_nsfw(question)
    parts = [_load_previous_consult(follow_up_of)] if follow_up_of else []
    if context:
        parts.append(context)
    if context_files:
        parts.append(_read_context_files(context_files))
    context = "\n\n".join(parts)
    context = _sanitize(context, scrub=scrub_context) if context else context
    user_content = question if not context else (
        f"<context>\n{context}\n</context>\n\n{question}")

    model = _resolve_model(model)
    effort = _resolve_effort(effort)
    backend = _active_backend()

    if backend == "unavailable":
        raise AdvisorError(_NO_CREDENTIALS)

    key = _repeat_key(kind, backend, model, effort, max_tokens, system,
                      user_content)
    hit = _recall(key)
    if hit:
        _append_usage(_usage_record(kind, backend, model, effort, "repeat",
                                    user_content, transcript=hit["path"]))
        if saved is not None:
            saved["path"] = hit["path"]
        return (hit["answer"] + hit["footer"]
                + "\n[repeat: identical to a consult at "
                + time.strftime("%H:%M", time.localtime(hit["ts"]))
                + " -- this is that answer again, at no cost. To consult "
                "afresh, change the question or add what is new to "
                "`context`.]")
    _check_caps()

    record = _usage_record(kind, backend, model, effort, "ok", user_content)
    token = _USAGE.set(record)
    started = time.monotonic()
    try:
        answer, footer = _consult_backend(system, user_content, model, effort,
                                          max_tokens, backend)
    except Exception as exc:
        record.update(status="error", error=exc.__class__.__name__,
                      duration_s=round(time.monotonic() - started, 1))
        _append_usage(record)
        raise
    finally:
        _USAGE.reset(token)
    record.update(duration_s=round(time.monotonic() - started, 1),
                  answer_chars=len(answer))
    footer += _usage_footer(record)

    path = _save_consult(kind, question, user_content, answer, footer)
    if saved is not None:
        saved["path"] = path
    if path:
        # In the footer rather than a block of its own: a client that renders
        # nothing but text is the common case, and this is the line that tells
        # the user their answer outlived the chat window.
        # Backticked: clients render the footer as Markdown, and an unquoted
        # Windows path loses its backslashes to escape processing.
        footer += ("\n[saved: `" + path + "` — the full answer is in this file; "
                   "give the user this path so they can read it outside the "
                   "chat]")
    record["transcript"] = path
    _append_usage(record)
    _remember(key, answer=answer, footer=footer, path=path)
    return answer + footer


def _consult_backend(system: str, user_content: str, model: str,
                     effort: Optional[str], max_tokens: int,
                     backend: str) -> tuple:
    """Run one consult on `backend`; return (answer, billing footer)."""
    if backend not in ("claude-code", "api"):
        spec = _BACKENDS[backend]
        answer = spec.consult(system, user_content, model, effort, max_tokens)
        return answer, (f"\n\n---\n[advisor: {backend}/{model} · billed to "
                        f"{spec.billing}"
                        + (f" · effort={effort}" if effort else "") + "]")

    if backend == "claude-code":
        try:
            answer = _consult_claude_code(system, user_content, model, effort)
        except UsageLimitError:
            # Only "auto" may switch the payer, and only for an exhausted plan:
            # the user asked for whatever works. An explicit claude-code backend
            # reports the limit instead, because moving someone onto paid
            # credits is their decision, not the server's.
            if not (BACKEND == "auto" and FALLBACK_TO_API
                    and _api_credentials_present()):
                raise
            _note_usage(backend="api")
            answer = _consult_api(system, user_content, model, effort, max_tokens)
            footer = (
                f"\n\n---\n[advisor: {model} · billed to API ACCOUNT "
                "(pay-per-token) — the Claude subscription's headless usage "
                "limit was exhausted, so this consult fell back to API credits. "
                "Tell the user.]")
        else:
            # The CLI has no max_tokens flag, so say so rather than let the
            # caller believe a cap was applied.
            note = (" · max_tokens ignored (not settable on this backend)"
                    if max_tokens and not LOCKED else "")
            alias = _cli_model(model)
            footer = (
                f"\n\n---\n[advisor: claude-code/{alias} · billed to SUBSCRIPTION"
                + (f" · effort={effort}" if effort else "") + note + "]")
    else:
        answer = _consult_api(system, user_content, model, effort, max_tokens)
        footer = (f"\n\n---\n[advisor: {model} · billed to API ACCOUNT"
                  + (f" · effort={effort}" if effort else "")
                  + f" · max_tokens={_resolve_max_tokens(max_tokens)}]")

    return answer, footer


# ---------------------------------------------------------------------------
# Runtime configuration and capability discovery
# ---------------------------------------------------------------------------

_CONFIGURABLE = ("model", "effort", "max_tokens", "answer_budget")


def _configure(model: str = "", effort: str = "", max_tokens: int = 0,
               answer_budget: int = -1, reset: bool = False) -> str:
    """Apply runtime overrides and return the resulting effective settings."""
    if LOCKED:
        raise AdvisorError(
            "ADVISOR_LOCK=1 is set, so the advisor's model, effort and token "
            "budget are pinned by the operator and cannot be changed from a "
            "tool call. Ask the user to change the MCP server config (or the "
            "advisor config file) and restart the server entry.")
    if reset:
        _OVERRIDES.clear()
    if model:
        resolved = _tier_or_id(model)
        if not str(resolved).startswith("claude-"):
            raise AdvisorInputError(
                f"unknown model or tier {model!r}. Use a tier "
                f"({', '.join(sorted(MODEL_TIERS))}) or a full model ID such as "
                "'claude-opus-5'. Run advisor_models to see the catalogue.")
        _OVERRIDES["model"] = resolved
    if effort:
        if effort.lower() not in VALID_EFFORT:
            raise AdvisorInputError(f"unknown effort {effort!r}; valid levels are "
                             + ", ".join(VALID_EFFORT))
        _OVERRIDES["effort"] = effort.lower()
    if max_tokens:
        _OVERRIDES["max_tokens"] = max(1, min(int(max_tokens), MAX_TOKENS_CEILING))
    if answer_budget >= 0:
        _OVERRIDES["answer_budget"] = int(answer_budget)
    elif answer_budget != -1:
        raise AdvisorInputError(
            "answer_budget must be 0 (no limit) or a positive word count; "
            f"got {answer_budget}.")

    changed = ", ".join(f"{k}={_OVERRIDES[k]}" for k in _CONFIGURABLE
                        if k in _OVERRIDES) or "(none)"
    return (
        "Runtime overrides updated. These last until the server restarts and "
        "are still beaten by per-call model/effort/max_tokens arguments.\n"
        f"overrides: {changed}\n"
        f"effective model: {_effective_model()}\n"
        f"effective effort: {_effective_effort() or '(API default)'}\n"
        f"effective max tokens: {_effective_max_tokens()}\n"
        f"effective answer budget: "
        f"{_effective_answer_budget() or 'unlimited'} words\n"
        f"active backend: {_active_backend()}")


def _model_catalogue() -> str:
    """What the caller can pass for `model` and `effort`, and what each accepts."""
    lines = ["TIERS (pass one of these as `model`):"]
    for tier, model_id in sorted(MODEL_TIERS.items()):
        marker = "  <- default" if model_id == _effective_model() else ""
        lines.append(f"  {tier:<10} -> {model_id}{marker}")

    lines.append("")
    lines.append("MODEL CAPABILITIES (a full model ID is also accepted):")
    lines.append(f"  {'model':<20} {'thinking':<10} effort levels")
    for prefix in sorted(MODEL_CAPS):
        thinking, effort = MODEL_CAPS[prefix]
        levels = "/".join(effort) if effort else "none (effort is rejected)"
        lines.append(f"  {prefix:<20} {thinking or 'n/a':<10} {levels}")
    lines.append("  any other ID        n/a        sent as a plain request")

    lines.append("")
    lines.append("EFFORT: " + ", ".join(VALID_EFFORT)
                 + " (higher = deeper reasoning, more tokens, more latency;"
                 " 'xhigh' suits hard coding and agentic problems)")
    lines.append(f"MAX TOKENS: per-call override up to {MAX_TOKENS_CEILING}; "
                 f"currently {_effective_max_tokens()}. Only the API backend "
                 "honours it -- the Claude Code CLI has no equivalent flag.")
    lines.append(f"ANSWER BUDGET: {_effective_answer_budget() or 'unlimited'} "
                 "words — the target length of the advice, honoured on both "
                 "backends. Change it with advisor_configure(answer_budget=N).")
    lines.append(f"BACKEND: {_active_backend()} (configured: {BACKEND})")
    if LOCKED:
        lines.append("NOTE: ADVISOR_LOCK=1 -- per-call model/effort/max_tokens "
                     "arguments are ignored.")
    return "\n".join(lines)


async def _consult_with_heartbeat(ctx: Optional[Context], *args, **kwargs):
    """Run `_consult` off the event loop, reporting progress while it works."""
    return await _with_heartbeat(ctx, _consult, *args, **kwargs)


async def _with_heartbeat(ctx: Optional[Context], fn, *args,
                          message: str = "Claude is still working", **kwargs):
    """Run blocking `fn` in a worker thread, reporting progress while it works.

    A consult is otherwise silent for its whole run, and MCP clients abort a
    silent tool call at their own request timeout -- shorter than a large
    consult, and than `advisor_login`'s default wait. A progress
    notification restarts that clock in clients that ask for progress (Kilo
    does), and is a no-op for one that sent no progress token, so the
    heartbeat runs unconditionally.
    """
    holder = _Cancellation()
    reset = _CANCELLATION.set(holder)
    try:
        # The task copies the current context, holder included, into the
        # worker thread `to_thread` starts.
        work = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    finally:
        _CANCELLATION.reset(reset)
    started = time.monotonic()
    try:
        while True:
            done, _ = await asyncio.wait({work}, timeout=PROGRESS_INTERVAL)
            if done:
                return work.result()
            if ctx is None:
                continue
            elapsed = int(time.monotonic() - started)
            try:
                await ctx.report_progress(
                    elapsed, message=f"{message} ({elapsed}s elapsed)")
            except Exception as exc:  # a lost heartbeat must not cost the answer
                print(f"[wisdomtooth] progress notification failed: {exc}",
                      file=sys.stderr)
    except asyncio.CancelledError:
        # The client gave up: stop the CLI so it stops spending quota. The
        # worker then finishes on its own; its result is discarded.
        holder.cancel()
        work.add_done_callback(lambda f: f.cancelled() or f.exception())
        raise


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool(title="Ask Wisdomtooth (escalation)", annotations=CONSULT_ANNOTATIONS)
async def ask_wisdomtooth(question: str, context: str, attempts_so_far: str,
                          model: str = "", effort: str = "",
                          max_tokens: int = 0,
                          context_files: Optional[list[str]] = None,
                          follow_up_of: str = "",
                          ctx: Optional[Context] = None):
    """ESCALATION: Ask Wisdomtooth for expert advice when you are stuck.

    WHEN TO USE — only when BOTH conditions hold:
    (a) You are having difficulty implementing code or understanding a
        framework, platform, operating system, library, or protocol; AND
    (b) Other options — Context7, official documentation, web search, and
        your own repeated attempts — have not helped significantly.
    Also appropriate for judgment calls docs can't answer: debugging strategy
    when an error makes no sense, architectural decisions, or suspected wrong
    assumptions in your approach.

    DO NOT USE as a first resort, for syntax lookups, or for anything a
    documentation tool would answer directly.

    Args:
        question: The specific question or problem you are stuck on.
        context: All relevant background — code snippets, exact error
            messages, environment/versions, constraints. The advisor is
            stateless and sees only what you pass here.
        attempts_so_far: What you have already tried and why it failed,
            including what Context7/docs/search returned (or why it wasn't
            helpful). Required — it prevents the advisor from repeating
            failed suggestions.
        model: Pick based on question complexity. "deep" (Opus) for
            architecture, subtle cross-system behavior, and debugging that has
            resisted earlier attempts; "balanced" (Sonnet, the usual default)
            for ordinary stuck-on-implementation problems; "fast" (Haiku) for quick sanity
            checks or factual confirmations. A full model ID string is also
            accepted. Leave empty for the configured default.
        effort: Reasoning effort: "low", "medium", "high", "xhigh", or "max".
            Scale with difficulty — "medium" for ordinary advice, "high" or
            "xhigh" for genuinely hard debugging and design questions, "max"
            only when a previous xhigh answer was insufficient. Ignored on
            models without effort support (e.g. "fast"/Haiku). Leave empty for
            the API default.
        max_tokens: Cap the answer length for this call (0 = configured
            default). Raise it when you expect a long design document; lower it
            for a quick yes/no. Applies to the API backend only — the Claude
            Code CLI has no equivalent flag.
        context_files: Paths of files for the server to read and attach
            itself, instead of pasting them into `context`; this saves your
            own context window. Relative paths resolve against the project
            folder; files outside it, and credential files, are refused.
        follow_up_of: To continue an earlier consult, the file name from its
            `[saved: ...]` line. The advisor then sees its earlier question and
            answer, so put only what is new in `context`.
    """
    saved: dict = {}
    answer = await _consult_with_heartbeat(
        ctx,
        question,
        context=f"{context}\n\n<attempts_so_far>\n{attempts_so_far}\n</attempts_so_far>",
        model=model,
        effort=effort,
        max_tokens=max_tokens,
        kind="ask_wisdomtooth",
        saved=saved,
        context_files=context_files,
        follow_up_of=follow_up_of,
    )
    return _result_blocks(answer, saved)


@tool(title="Review code with Claude", annotations=CONSULT_ANNOTATIONS)
async def review_code(code: str, concern: str = "general quality",
                      model: str = "", effort: str = "",
                      max_tokens: int = 0,
                      context_files: Optional[list[str]] = None,
                      ctx: Optional[Context] = None):
    """ESCALATION: Have Claude review code you are unsure about.

    WHEN TO USE: after implementing something non-trivial where you have
    residual doubt — subtle concurrency/async logic, security-sensitive code,
    tricky edge cases — or when your implementation works but you suspect a
    better approach exists. Not for routine code you are confident in.

    Args:
        code: The code to review (include the language if not obvious).
        concern: What to focus on — e.g. "security", "performance",
            "correctness of this async logic", or "general quality".
        model: "deep" for security-critical or subtle concurrency code,
            "balanced" for a routine review, "fast" for a sanity pass. Empty
            uses the configured default.
            Full model IDs also accepted.
        effort: "low"/"medium"/"high"/"xhigh"/"max" - scale with how subtle
            the code is. Ignored on models without effort support.
        max_tokens: Cap the review length for this call (0 = configured
            default). Applies to the API backend only.
        context_files: Paths of related files (callers, interfaces, tests)
            for the server to read and attach, relative to the project folder.
    """
    saved: dict = {}
    answer = await _consult_with_heartbeat(
        ctx,
        context_files=context_files,
        question=f"Review this code with a focus on: {concern}. "
        "List concrete issues in priority order, with suggested fixes.",
        context=code,
        extra_system="You are performing a code review. Be specific: cite lines "
        "or symbols, and distinguish must-fix issues from nitpicks.",
        model=model,
        effort=effort,
        max_tokens=max_tokens,
        # Reviewed code goes over verbatim. Word substitution would make the
        # advisor comment on code the caller does not have, and any
        # string-literal fix it suggested would not apply. Secrets are still
        # redacted.
        scrub_context=False,
        kind="review_code",
        saved=saved,
    )
    return _result_blocks(answer, saved)


@tool(title="Compare approaches with Claude", annotations=CONSULT_ANNOTATIONS)
async def compare_approaches(problem: str, options: str, criteria: str = "",
                             model: str = "", effort: str = "",
                             max_tokens: int = 0, ctx: Optional[Context] = None):
    """ESCALATION: Have Claude compare approaches when you can't decide.

    WHEN TO USE: you have identified 2+ viable approaches to a non-trivial
    problem (architecture, library choice, migration strategy) and the
    tradeoffs are genuinely unclear after your own analysis. Not for
    decisions with an obvious answer.

    Args:
        problem: The problem being solved.
        options: The candidate approaches, described one per line.
        criteria: Optional decision criteria (e.g. "must be low-latency,
            team knows Python, ships this week").
        model: "deep" for high-stakes, hard-to-reverse architectural
            choices; "balanced" for everyday decisions. Empty uses the
            configured default.
        effort: "medium" for routine tradeoffs, "high"/"xhigh"/"max" for
            decisions with long-term consequences.
        max_tokens: Cap the answer length for this call (0 = configured
            default). Applies to the API backend only.
    """
    question = (
        f"Problem: {problem}\n\nCandidate approaches:\n{options}\n"
        + (f"\nDecision criteria: {criteria}\n" if criteria else "")
        + "\nCompare the tradeoffs briefly, then commit to a single recommendation."
    )
    saved: dict = {}
    answer = await _consult_with_heartbeat(ctx, question, model=model,
                                           effort=effort, max_tokens=max_tokens,
                                           kind="compare_approaches", saved=saved)
    return _result_blocks(answer, saved)


CREDENTIAL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True,
    openWorldHint=True,
)


@tool(title="Connect a Claude subscription account",
      annotations=CREDENTIAL_ANNOTATIONS)
async def advisor_login(force: bool = False, wait_seconds: int = 180,
                        ctx: Optional[Context] = None) -> str:
    """Connect the user's Claude account so consults use their SUBSCRIPTION.

    FREE — makes no model call. Opens the official Claude Code sign-in
    (`claude auth login --claudeai`) in a console window on the user's desktop,
    waits for them to finish in the browser, and switches the advisor onto
    subscription billing immediately — no config edit, no server restart.

    Call this when `advisor_status` reports the backend as `api` or
    `unavailable` and the user would rather spend their Pro/Max subscription
    than pay-per-token API credits. If the account is already connected it
    reports that and changes nothing.

    On a headless machine, in a container, or over SSH there is no desktop to
    open a window on; the tool then returns the two manual commands instead —
    relay them to the user verbatim.

    Args:
        force: Re-run sign-in even if an account is already connected. Use when
            switching accounts, or when the CLI is logged in with an API key
            (which bills the Console account rather than the subscription).
        wait_seconds: How long to wait for the browser sign-in before returning
            (0 skips waiting). Returning early is not a failure — call
            advisor_login again to check.
    """
    # Heartbeat: the default wait outlasts a client's silence timeout.
    return await _with_heartbeat(ctx, _login, force=force,
                                 wait_seconds=wait_seconds,
                                 message="Waiting for the browser sign-in")


@tool(title="Save a Claude subscription token",
      annotations=CREDENTIAL_ANNOTATIONS)
async def advisor_set_token(token: str) -> str:
    """Store a long-lived subscription token so consults bill the SUBSCRIPTION.

    FREE — makes no model call. This is the headless counterpart to
    advisor_login, and the most durable option for an MCP server started by a
    GUI editor: the user runs `claude setup-token` once in a terminal, and you
    pass the token it prints here. The advisor saves it to an owner-only file
    and applies it right away — the user never has to edit their MCP client's
    JSON config or restart the server entry.

    The token is a credential: do not repeat it back to the user, log it, or
    put it anywhere other than this argument.

    Args:
        token: The token printed by `claude setup-token`, and nothing else.
    """
    return await asyncio.to_thread(_set_oauth_token, token)


@tool(title="Disconnect the stored subscription token",
      annotations=CREDENTIAL_ANNOTATIONS)
async def advisor_logout() -> str:
    """Forget the subscription token the advisor has stored.

    FREE — makes no model call. Removes only the advisor's own copy; the Claude
    Code CLI's login is left alone (`claude auth logout` in a terminal signs out
    completely). Use when switching accounts or clearing a shared machine.
    """
    return await asyncio.to_thread(_clear_oauth_token)


@tool(title="List advisor models and effort levels",
      annotations=LOCAL_ANNOTATIONS)
def advisor_models() -> str:
    """List the model tiers, model capabilities, and effort levels available.

    FREE — makes no model call. Call this when you are unsure what to pass for
    `model` or `effort`, or when the user asks which models the advisor can
    reach. It also reports which models reject `effort` and what the current
    token ceiling is.
    """
    return _model_catalogue()


@tool(title="Configure the advisor", annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True,
    openWorldHint=False))
def advisor_configure(model: str = "", effort: str = "", max_tokens: int = 0,
                      answer_budget: int = -1, reset: bool = False) -> str:
    """Change the advisor's default model, effort, or token budget.

    FREE — makes no model call. Changes apply to every later consult in this
    server process and survive until it restarts, so use it when the user says
    something like "use Sonnet from now on" or "keep answers short" instead of
    asking them to edit config and restart the MCP server.

    Per-call arguments still win over these. Refused when the operator has set
    ADVISOR_LOCK=1.

    Args:
        model: New default — a tier ("fast"/"balanced"/"deep") or a full model
            ID. Empty leaves it unchanged.
        effort: New default reasoning effort: "low", "medium", "high",
            "xhigh", or "max". Empty leaves it unchanged.
        max_tokens: New default answer cap (API backend only). 0 leaves it
            unchanged.
        answer_budget: New target answer length in words, which works on both
            backends. Lower it when the user says answers are too long or your
            own context is tight; 0 removes the limit. -1 leaves it unchanged.
        reset: Drop all runtime overrides and return to the configuration the
            server started with.
    """
    return _configure(model=model, effort=effort, max_tokens=max_tokens,
                      answer_budget=answer_budget, reset=reset)


@tool(title="Advisor usage and spend", annotations=LOCAL_ANNOTATIONS)
def advisor_usage(days: int = 7) -> str:
    """Report the advisor's consults, tokens and estimated cost over time.

    FREE — makes no model call; reads the local usage ledger. Use when the
    user asks how much the advisor has used, or before a burst of consults
    when their plan's limits are tight. Cost is an estimate at API rates; the
    subscription's own meter is not visible to this server.

    Args:
        days: How far back the per-model breakdown goes (1-35).
    """
    return _usage_report(days)


@tool(title="Check advisor credentials", annotations=LOCAL_ANNOTATIONS)
async def advisor_auth_check(ctx: Optional[Context] = None) -> str:
    """Diagnose login/credential readiness WITHOUT spending tokens.

    Run this FIRST whenever a consult fails, hangs, or before the initial
    smoke test. Reports which account would be billed, the CLI's login state,
    and any auth-hijacking env vars. It cannot log in for you — OAuth requires
    a human in a terminal.
    """
    # Two CLI probes of up to 30s each: longer than some clients stay silent.
    return await _with_heartbeat(ctx, _auth_report,
                                 message="Checking the Claude CLI")


def _auth_report() -> str:
    lines = [f"version: {__version__}",
             f"mcp sdk: {_MCP_MAJOR}.x",
             f"configured backend: {BACKEND}"]
    backend = _active_backend()
    lines.append(f"active backend: {backend}")

    claude_bin = _claude_bin()
    if claude_bin:
        lines.append(f"claude CLI: {claude_bin}")
        try:
            probe = _run_claude([claude_bin, "--version"], timeout_s=30)
            lines.append("claude version: "
                         + (probe.stdout or probe.stderr).strip()[:80])
        except Exception as exc:
            lines.append(f"claude --version failed: {exc} — CLI may be broken.")
        status = _cli_auth_status()
        if status is None:
            lines.append("claude auth status: could not be read.")
        elif status.get("loggedIn"):
            lines.append(
                "claude login: OK — account={} method={} plan={}".format(
                    status.get("email", "?"), status.get("authMethod", "?"),
                    status.get("subscriptionType", "?")))
            if status.get("authMethod") not in (None, "claude.ai"):
                lines.append(
                    "  NOTE: authMethod is not 'claude.ai', so CLI consults may "
                    "bill an API account rather than the subscription. Call "
                    "`advisor_login` with force=true to switch to a "
                    "subscription account.")
        else:
            lines.append("claude login: NOT LOGGED IN — call the `advisor_login` "
                         "tool to connect the user's subscription account "
                         "(opens the sign-in on their desktop), or have them run "
                         "`claude setup-token` and pass it to `advisor_set_token`.")
    else:
        lines.append("claude CLI: NOT FOUND on PATH — install Claude Code, or set "
                     "ADVISOR_CLAUDE_BIN to its absolute path (GUI editors often "
                     "have a reduced PATH).")

    token_source = ("environment" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                    else "stored by advisor_set_token"
                    if _read_credentials().get("claude_code_oauth_token")
                    else None)
    if token_source:
        lines.append(f"subscription token: present ({token_source}) — durable "
                     "headless auth, good.")
    else:
        lines.append("subscription token: none stored. `advisor_login` (or "
                     "`advisor_set_token`) makes subscription auth survive "
                     "restarts and reduced GUI environments.")
    lines.append("api key: " + ("present" if os.environ.get("ANTHROPIC_API_KEY")
                                else "absent"))

    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(var):
            kept = os.environ.get("ADVISOR_KEEP_AUTH_ENV") == "1"
            lines.append(
                f"note: {var} is present in the server env; it would hijack "
                "billing away from the subscription, so it is "
                + ("KEPT (ADVISOR_KEEP_AUTH_ENV=1)." if kept
                   else "stripped from claude subprocesses."))

    if backend == "unavailable":
        lines.append("")
        lines.append(_NO_CREDENTIALS)
    elif backend == "api":
        lines.append("")
        lines.append("This is billing pay-per-token API credits. If the user has "
                     "a Claude Pro/Max subscription, call `advisor_login` to "
                     "switch to it at no per-token cost.")
    return "\n".join(lines)


@tool(title="Advisor status", annotations=LOCAL_ANNOTATIONS)
def advisor_status() -> str:
    """Report the advisor's active backend, billing target, and defaults.

    FREE — makes no model call. Use when the user asks which account the
    advisor is using, or to sanity-check configuration.
    """
    backend = _active_backend()
    billing = {
        "claude-code": "Claude SUBSCRIPTION (Pro/Max) via the local Claude Code "
                       "CLI; ANTHROPIC_API_KEY is stripped from the subprocess",
        "api": "DEVELOPER API account (pay-per-token via ANTHROPIC_API_KEY) — "
               "NOT the Pro/Max subscription",
        "unavailable": "NONE — no usable credentials; run advisor_auth_check",
    }.get(backend) or (_BACKENDS[backend].billing if backend in _BACKENDS
                       else backend)
    if not SAVE_CONSULTS:
        transcripts = "disabled (ADVISOR_SAVE_CONSULTS=0)"
    elif CONSULT_KEEP:
        transcripts = f"{_consult_dir()} (newest {CONSULT_KEEP} kept)"
    else:
        transcripts = f"{_consult_dir()} (all kept)"
    lines = [
        f"version: {__version__}",
        f"mcp sdk: {_MCP_MAJOR}.x",
        f"configured backend: {BACKEND}",
        f"active backend: {backend}",
        f"billing: {billing}",
        f"default model: {DEFAULT_MODEL}",
        f"default effort: {DEFAULT_EFFORT or '(API default)'}",
        f"max tokens: {_effective_max_tokens()}",
        f"answer budget: {_effective_answer_budget() or 'unlimited'} words"
        + ("" if _effective_answer_budget() else
           " (a long answer can overflow a small caller's context)"),
        f"tool surface: {'minimal (ask_wisdomtooth, advisor_status)' if MINIMAL_TOOLS else 'full (11 tools)'}",
        f"consult transcripts: {transcripts}",
        f"usage: {_usage_line()}",
        "usage ledger: " + (_usage_path() if USAGE_LOG
                            else "disabled (ADVISOR_USAGE_LOG=0)"),
        "consult caps: " + (_caps_description() or "none"),
        "repeat guard: " + (f"{REPEAT_WINDOW_S / 60:g} min" if REPEAT_WINDOW_S
                            else "off"),
        "file roots for context_files: "
        + (", ".join(_file_roots()) or "none (set ADVISOR_FILE_ROOTS)"),
        f"timeout: {TIMEOUT}s base"
        + (f", grows with input size, answer budget and effort "
           f"(scale {TIMEOUT_SCALE:g}), cap {TIMEOUT_MAX}s" if TIMEOUT_SCALE
           else " (flat: ADVISOR_TIMEOUT_SCALE=0)"),
        f"progress heartbeat: every {PROGRESS_INTERVAL}s while a consult runs",
        f"per-call overrides: "
        f"{'LOCKED (env defaults always win)' if LOCKED else 'allowed'}",
        f"transport: {TRANSPORT}",
        "subscription token: " + (
            "set in the environment" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            else "stored by advisor_set_token"
            if _read_credentials().get("claude_code_oauth_token")
            else "none (the CLI's own login is used, if any)"),
    ]
    if BACKEND == "auto":
        lines.append("usage-limit fallback to API credits: "
                     + ("enabled" if FALLBACK_TO_API else "disabled"))
    if MAX_BUDGET_USD:
        lines.append(f"per-consult budget cap: ${MAX_BUDGET_USD}")
    return "\n".join(lines)


def _billing_banner() -> str:
    backend = _active_backend()
    if backend == "claude-code":
        return (f"[wisdomtooth v{__version__}] backend={backend} → consults run "
                "through the local Claude Code CLI and bill your Claude "
                "SUBSCRIPTION (Pro/Max). ANTHROPIC_API_KEY is stripped from the "
                "subprocess so it cannot silently switch to API billing.")
    if backend == "api":
        return (f"[wisdomtooth v{__version__}] backend={backend} → consults use "
                "ANTHROPIC_API_KEY and bill your DEVELOPER CONSOLE account per "
                "token (NOT your Pro/Max subscription).")
    return (f"[wisdomtooth v{__version__}] no usable credentials — every "
            "consult will fail until you log in Claude Code (`claude` → "
            "`/login`) or set ANTHROPIC_API_KEY. Run advisor_auth_check.")


def main() -> None:
    print(_billing_banner(), file=sys.stderr)
    support = _support_line()
    if support:
        print(f"[wisdomtooth] free and open source; support it at {support} "
              "(ADVISOR_SHOW_SUPPORT=0 hides this line)", file=sys.stderr)
    if TRANSPORT == "http":
        if HTTP_HOST not in ("127.0.0.1", "localhost", "::1"):
            # Inside a container 0.0.0.0 is normal (the port mapping is the
            # guard), so warn rather than refuse.
            print(f"[wisdomtooth] WARNING: the HTTP transport on {HTTP_HOST}:"
                  f"{HTTP_PORT} has no authentication -- anyone who can reach "
                  "it can spend this account. Keep it on 127.0.0.1, or publish "
                  "a container port only to localhost.", file=sys.stderr)
        if _MCP_MAJOR >= 2:
            mcp.run("streamable-http", host=HTTP_HOST, port=HTTP_PORT)
        else:  # pragma: no cover - mcp 1.x settings object
            mcp.settings.host = HTTP_HOST
            mcp.settings.port = HTTP_PORT
            mcp.run(transport="streamable-http")
    else:
        mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
