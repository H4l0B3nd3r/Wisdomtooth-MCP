"""Claude Advisor MCP Server.

Exposes Claude as an *escalation* advisor over MCP. Other agents (Kilo Code,
Cursor, Cline, custom agents) call it when they are stuck -- after their own
attempts and doc lookups (e.g. Context7) have not resolved the problem.

Billing, in one line: by default the server uses the user's Claude
subscription via the local Claude Code CLI, and only falls back to
pay-per-token API credits when the subscription is not usable. Every answer
says which account paid for it.

Run:
    claude-advisor-mcp            # auto: subscription first, API as fallback
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Optional

import anthropic
from anthropic import Anthropic

# ---------------------------------------------------------------------------
# MCP SDK compatibility
# ---------------------------------------------------------------------------
# mcp 2.0 renamed FastMCP to MCPServer and moved it to mcp.server.mcpserver.
# Both spellings expose the same decorator surface we use, so support each --
# a fresh `uv tool install` gets 2.x while existing installs are still on 1.x.
try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _ServerClass
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError
    _MCP_MAJOR = 2
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _ServerClass
    from mcp.server.fastmcp.exceptions import ToolError as _ToolError
    _MCP_MAJOR = 1

from mcp.types import ToolAnnotations


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
    __version__ = _pkg_version("claude-advisor-mcp")
except Exception:  # running from source without install
    __version__ = "0.4.0-dev"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Five layers, highest priority first:
#   1. per-call tool arguments        (model=, effort=, max_tokens=)
#   2. runtime overrides              (the advisor_configure tool)
#   3. environment variables          (ADVISOR_*, set by the MCP client)
#   4. a JSON config file             (ADVISOR_CONFIG, else ~/.claude-advisor/
#                                      config.json) -- machine-wide defaults
#   5. built-in defaults
# ADVISOR_LOCK=1 freezes layers 3-5 and rejects 1-2, for hard cost control.

DEFAULT_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".claude-advisor",
                                   "config.json")

# Config-file key -> the environment variable that overrides it.
CONFIG_KEYS = {
    "backend": "ADVISOR_BACKEND",
    "model": "ADVISOR_MODEL",
    "effort": "ADVISOR_EFFORT",
    "max_tokens": "ADVISOR_MAX_TOKENS",
    "timeout": "ADVISOR_TIMEOUT",
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
        print(f"[claude-advisor] ignoring {path}: {exc}", file=sys.stderr)
        return {}
    unknown = set(data) - set(CONFIG_KEYS)
    if unknown:
        print(f"[claude-advisor] unknown keys in {path}: {sorted(unknown)}",
              file=sys.stderr)
    return data


_FILE_CONFIG = _load_config_file()


def _setting(key: str, default=None):
    """Environment first, then the config file, then the built-in default."""
    env_value = os.environ.get(CONFIG_KEYS[key])
    if env_value not in (None, ""):
        return env_value
    if key in _FILE_CONFIG and _FILE_CONFIG[key] is not None:
        return _FILE_CONFIG[key]
    return default


def _flag(key: str, default: bool) -> bool:
    value = _setting(key)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


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

TIMEOUT = int(_setting("timeout", 180))

# In "auto" mode only, a consult that fails because the subscription's headless
# quota is exhausted may be retried on API credits. Ignored for an explicit
# "claude-code" backend: choosing the subscription is a billing decision, and
# quietly moving the user onto paid credits is not the server's call.
FALLBACK_TO_API = _flag("fallback_to_api", True)

# Optional hard spend cap handed to the CLI (`--max-budget-usd`).
MAX_BUDGET_USD = _setting("max_budget_usd")

# Roughly how long an answer may be, in words. The advisor's reply is fed back
# into the CALLING model's context, which for a local 4B-30B model may be 8k-32k
# tokens in total -- an unbounded Opus answer can consume all of it. `max_tokens`
# cannot help on the subscription backend (the CLI exposes no such flag), so the
# budget is expressed in the system prompt, which both backends honour.
# 0 disables it, for callers with a large context window.
ANSWER_BUDGET = max(0, int(_setting("answer_budget", 600)))

# Expose only the tools an agent actually needs (`ask_claude`, `advisor_status`)
# and hide the operator tools. Every schema is charged against the calling
# model's context on every turn, and a longer tool list measurably degrades
# tool-selection accuracy in small models.
MINIMAL_TOOLS = _flag("minimal_tools", False)
ESSENTIAL_TOOLS = ("ask_claude", "advisor_status")


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
        print(f"[claude-advisor] ignoring tier overrides: {exc}", file=sys.stderr)


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
MAX_TOKENS = min(int(_setting("max_tokens", 16000)), MAX_TOKENS_CEILING)

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
        f"\nLength: keep the answer under roughly {budget} words. Your reply is "
        "inserted into the context window of the agent that asked, which may be "
        "small. Lead with the recommendation, keep code to the minimum that "
        "makes it concrete, and drop restatement of the question. If the "
        "problem genuinely cannot be answered that briefly, give the decisive "
        "part and say what you left out.\n")


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
        headroom = {"high": 24000, "xhigh": 32000, "max": 48000}.get(effort or "")
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
            print(f"[claude-advisor] ignoring system prompt file {path}: {exc}",
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

_server_kwargs = dict(name="claude-advisor", instructions=WHEN_TO_USE)
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
    return os.path.join(os.path.expanduser("~"), ".claude-advisor",
                        "credentials.json")


def _read_credentials() -> dict:
    path = _credentials_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # a broken store must never stop the server
        print(f"[claude-advisor] ignoring {path}: {exc}", file=sys.stderr)
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
MAX_CONTEXT_CHARS = int(os.environ.get("ADVISOR_MAX_CONTEXT_CHARS", "60000"))

# NSFW word scrubbing: word-boundary only (never mangles class/assert/shell/
# cocktail), case-preserving, SFW replacements. Backstop to the agent-side
# scrub mandated in .kilocode/rules. Disable: ADVISOR_NSFW_SCRUB=0.
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
            print(f"[claude-advisor] ignoring ADVISOR_NSFW_EXTRA_JSON: {exc}",
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
    if _NSFW_RE is None or os.environ.get("ADVISOR_NSFW_SCRUB", "1") == "0":
        return text
    return _NSFW_RE.sub(lambda m: _match_case(_NSFW_MAP[m.group(0).lower()],
                                              m.group(0)), text)


def _truncate(text: str) -> str:
    if len(text) <= MAX_CONTEXT_CHARS:
        return text
    head = text[: int(MAX_CONTEXT_CHARS * 0.7)]
    tail = text[-int(MAX_CONTEXT_CHARS * 0.25):]
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
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout_s)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        raise subprocess.TimeoutExpired(cmd, timeout_s, output=out, stderr=err)


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
        print(f"[claude-advisor] could not probe `claude --help`: {exc}",
              file=sys.stderr)
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
    """Write the system prompt beside the workdir and return its path.

    Rewritten per consult because `extra_system` differs by tool, and kept out
    of the workdir itself so the CLI never sees it as project content.
    """
    path = os.path.join(os.path.expanduser("~"), ".claude-advisor",
                        "system-prompt.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(system)
    return path


def _workdir() -> str:
    """A dedicated empty directory for consults.

    Keeps Claude Code from loading CLAUDE.md and project state from whatever
    cwd the MCP client happened to use, and avoids first-run trust prompts.
    """
    path = os.path.join(os.path.expanduser("~"), ".claude-advisor", "workdir")
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
    if "--system-prompt-file" in features:
        cmd += ["--system-prompt-file", _system_prompt_file(system)]
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

    try:
        result = _run_claude(cmd, _child_env(), _workdir(), TIMEOUT, user_content)
    except subprocess.TimeoutExpired as exc:
        tail = (exc.stderr or "")
        tail = tail.decode(errors="replace") if isinstance(tail, bytes) else tail
        raise AdvisorError(
            f"claude CLI produced no answer within {TIMEOUT}s and was killed. "
            "Most likely causes, in order: (1) Claude Code is not logged in -- "
            "run `claude` in a terminal, then `/login` with the claude.ai "
            "(Pro/Max) account; (2) a first-run onboarding/trust prompt is "
            "blocking -- run `claude` interactively once on this machine; "
            "(3) network issues. Do NOT retry in a loop; surface this to the "
            "user. stderr tail: " + tail[-400:]
        ) from exc

    blob = ((result.stderr or "") + " " + (result.stdout or "")).lower()

    answer, payload = _parse_cli_output(result.stdout or "")
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

    max_tokens here can reach 48k on high-effort adaptive-thinking models, and
    the SDKs require streaming above roughly 8k output tokens or the request
    trips the HTTP timeout.
    """
    with messages_api.stream(**kwargs) as stream:
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
    if BACKEND in ("claude-code", "api"):
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
    while time.time() < deadline:
        time.sleep(_LOGIN_POLL_SECONDS)
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


def _consult(question: str, context: str = "", extra_system: str = "",
             model: str = "", effort: str = "", max_tokens: int = 0,
             scrub_context: bool = True) -> str:
    """One stateless consult, on whichever backend is active."""
    system = (ADVISOR_SYSTEM_PROMPT + ("\n" + extra_system if extra_system else "")
              + _answer_budget_instruction())
    question = _scrub_nsfw(question)
    context = _sanitize(context, scrub=scrub_context) if context else context
    user_content = question if not context else (
        f"<context>\n{context}\n</context>\n\n{question}")

    model = _resolve_model(model)
    effort = _resolve_effort(effort)
    backend = _active_backend()

    if backend == "unavailable":
        raise AdvisorError(_NO_CREDENTIALS)

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
            answer = _consult_api(system, user_content, model, effort, max_tokens)
            return answer + (
                f"\n\n---\n[advisor: {model} · billed to API ACCOUNT "
                "(pay-per-token) — the Claude subscription's headless usage "
                "limit was exhausted, so this consult fell back to API credits. "
                "Tell the user.]")
        # The CLI has no max_tokens flag, so say so rather than let the caller
        # believe a cap was applied.
        note = (" · max_tokens ignored (not settable on this backend)"
                if max_tokens and not LOCKED else "")
        alias = _cli_model(model)
        return answer + (
            f"\n\n---\n[advisor: claude-code/{alias} · billed to SUBSCRIPTION"
            + (f" · effort={effort}" if effort else "") + note + "]")

    answer = _consult_api(system, user_content, model, effort, max_tokens)
    return answer + (f"\n\n---\n[advisor: {model} · billed to API ACCOUNT"
                     + (f" · effort={effort}" if effort else "")
                     + f" · max_tokens={_resolve_max_tokens(max_tokens)}]")


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


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool(title="Ask Claude (escalation)", annotations=CONSULT_ANNOTATIONS)
async def ask_claude(question: str, context: str, attempts_so_far: str,
                     model: str = "", effort: str = "",
                     max_tokens: int = 0) -> str:
    """ESCALATION: Ask Claude for expert advice when you are stuck.

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
        model: Pick based on question complexity. "deep" (Opus, default) for
            architecture, subtle cross-system behavior, and debugging that has
            resisted earlier attempts; "balanced" (Sonnet) for ordinary
            stuck-on-implementation problems; "fast" (Haiku) for quick sanity
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
    """
    return await asyncio.to_thread(
        _consult,
        question,
        context=f"{context}\n\n<attempts_so_far>\n{attempts_so_far}\n</attempts_so_far>",
        model=model,
        effort=effort,
        max_tokens=max_tokens,
    )


@tool(title="Review code with Claude", annotations=CONSULT_ANNOTATIONS)
async def review_code(code: str, concern: str = "general quality",
                      model: str = "", effort: str = "",
                      max_tokens: int = 0) -> str:
    """ESCALATION: Have Claude review code you are unsure about.

    WHEN TO USE: after implementing something non-trivial where you have
    residual doubt — subtle concurrency/async logic, security-sensitive code,
    tricky edge cases — or when your implementation works but you suspect a
    better approach exists. Not for routine code you are confident in.

    Args:
        code: The code to review (include the language if not obvious).
        concern: What to focus on — e.g. "security", "performance",
            "correctness of this async logic", or "general quality".
        model: "deep" (default) for security-critical or subtle concurrency
            code, "balanced" for a routine review, "fast" for a sanity pass.
            Full model IDs also accepted.
        effort: "low"/"medium"/"high"/"xhigh"/"max" - scale with how subtle
            the code is. Ignored on models without effort support.
        max_tokens: Cap the review length for this call (0 = configured
            default). Applies to the API backend only.
    """
    return await asyncio.to_thread(
        _consult,
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
    )


@tool(title="Compare approaches with Claude", annotations=CONSULT_ANNOTATIONS)
async def compare_approaches(problem: str, options: str, criteria: str = "",
                             model: str = "", effort: str = "",
                             max_tokens: int = 0) -> str:
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
        model: "deep" (default) for high-stakes, hard-to-reverse architectural
            choices; "balanced" for everyday decisions.
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
    return await asyncio.to_thread(_consult, question, model=model, effort=effort,
                                   max_tokens=max_tokens)


CREDENTIAL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True,
    openWorldHint=True,
)


@tool(title="Connect a Claude subscription account",
      annotations=CREDENTIAL_ANNOTATIONS)
async def advisor_login(force: bool = False, wait_seconds: int = 180) -> str:
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
    return await asyncio.to_thread(_login, force=force,
                                   wait_seconds=wait_seconds)


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


@tool(title="Check advisor credentials", annotations=LOCAL_ANNOTATIONS)
async def advisor_auth_check() -> str:
    """Diagnose login/credential readiness WITHOUT spending tokens.

    Run this FIRST whenever a consult fails, hangs, or before the initial
    smoke test. Reports which account would be billed, the CLI's login state,
    and any auth-hijacking env vars. It cannot log in for you — OAuth requires
    a human in a terminal.
    """
    return await asyncio.to_thread(_auth_report)


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
    }[backend]
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
        f"tool surface: {'minimal (ask_claude, advisor_status)' if MINIMAL_TOOLS else 'full (10 tools)'}",
        f"timeout: {TIMEOUT}s",
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
        return (f"[claude-advisor v{__version__}] backend={backend} → consults run "
                "through the local Claude Code CLI and bill your Claude "
                "SUBSCRIPTION (Pro/Max). ANTHROPIC_API_KEY is stripped from the "
                "subprocess so it cannot silently switch to API billing.")
    if backend == "api":
        return (f"[claude-advisor v{__version__}] backend={backend} → consults use "
                "ANTHROPIC_API_KEY and bill your DEVELOPER CONSOLE account per "
                "token (NOT your Pro/Max subscription).")
    return (f"[claude-advisor v{__version__}] no usable credentials — every "
            "consult will fail until you log in Claude Code (`claude` → "
            "`/login`) or set ANTHROPIC_API_KEY. Run advisor_auth_check.")


def main() -> None:
    print(_billing_banner(), file=sys.stderr)
    if TRANSPORT == "http":
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
