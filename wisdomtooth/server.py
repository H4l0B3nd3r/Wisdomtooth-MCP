"""Wisdomtooth MCP Server.

Exposes a frontier model as an *escalation* advisor over MCP. Other agents
(Kilo Code, Cursor, Cline, custom agents) call it when they are stuck -- after
their own attempts and doc lookups (e.g. Context7) have not resolved the
problem.

Claude is the model behind it today and stays the default. The name is
model-neutral on purpose: `backends.Backend` is the seam another provider
would slot into. Nothing in the tool surface assumes Anthropic -- but nothing
else is implemented yet either, so every consult currently goes to Claude.

Billing, in one line: by default the server uses the user's Claude
subscription via the local Claude Code CLI, and only falls back to
pay-per-token API credits when the subscription is not usable. Every answer
says which account paid for it.

This module is the composition root: it loads the settings, builds the MCP
server and wires the parts together. Each part lives in a module of its own
and takes what it needs as arguments:
  config       Settings, the config file, caller presets
  models       tiers, per-model capabilities, the API request body
  prompts      the advisor persona and the fixed guidance texts
  safety       secret redaction, size caps, word scrubbing, file rules
  usage        token and cost accounting, the ledger, caps and reports
  transcripts  the saved Markdown consults and their resource links
  claude_cli   running the CLI: processes, cancellation, streamed output
  backends     the provider seam
  httpauth     bearer-token auth for the HTTP transport
  doctor       the `wisdomtooth-mcp doctor` command

Run:
    wisdomtooth-mcp            # auto: subscription first, API as fallback
    wisdomtooth-mcp doctor     # check the setup, print a client config
"""

import asyncio
import contextvars
import functools
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Optional

import anthropic
from anthropic import Anthropic

# mcp 2.0 renamed FastMCP to MCPServer and moved it to mcp.server.mcpserver.
# Both spellings expose the same decorator surface we use, so support each --
# a fresh `uv tool install` gets 2.x while existing installs are still on 1.x.
try:  # mcp >= 2.0
    from mcp.server.mcpserver import Context, MCPServer as _ServerClass
    _MCP_MAJOR = 2
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP as _ServerClass
    _MCP_MAJOR = 1

from mcp.types import ToolAnnotations

from . import (claude_cli, config, doctor, httpauth, models, prompts, safety,
               transcripts, usage)
from .backends import Backend
from .claude_cli import CANCELLATION as _CANCELLATION
from .claude_cli import Cancellation as _Cancellation
from .claude_cli import kill_tree as _kill_tree  # noqa: F401 - re-exported
from .claude_cli import parse_output as _parse_cli_output
from .claude_cli import spawn_login_console as _spawn_login_console
from .claude_cli import wrap_for_windows as _wrap_for_windows  # noqa: F401
from .config import CONFIG_KEYS, PRESETS, DEFAULT_PRESET  # noqa: F401
from .errors import AdvisorError, AdvisorInputError, UsageLimitError
from .models import (CLAUDE_CODE_ALIASES, FALLBACK_BETA, MAX_TOKENS_CEILING,
                     MODEL_CAPS, VALID_EFFORT)

try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("wisdomtooth-mcp")
except Exception:  # running from source without install
    __version__ = "0.0.0+source"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Everything read at startup comes from one frozen object; see config.py for
# the layers and what each setting does. The module constants below are that
# object's values under the names the rest of this file (and its tests) use.

_state_dir = config.state_dir
DEFAULT_CONFIG_PATH = config.default_config_path()
_FILE_CONFIG = config.load_config_file()
SETTINGS = config.load_settings(file_config=_FILE_CONFIG)
_PRESET_VALUES = dict(SETTINGS.preset_values)


def _setting(key: str, default=None):
    """A setting read live, for the few that must follow the environment
    after startup (paths, prompt files): environment, file, preset, default."""
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


PRESET = SETTINGS.preset
BACKEND = SETTINGS.backend
TRANSPORT = SETTINGS.transport
HTTP_HOST = SETTINGS.http_host
HTTP_PORT = SETTINGS.http_port
HTTP_TOKEN = SETTINGS.http_token
HTTP_NO_AUTH = SETTINGS.http_no_auth
TIMEOUT = SETTINGS.timeout
TIMEOUT_SCALE = SETTINGS.timeout_scale
TIMEOUT_MAX = SETTINGS.timeout_max
IDLE_TIMEOUT = SETTINGS.idle_timeout
PROGRESS_INTERVAL = SETTINGS.progress_interval
# In "auto" mode only, a consult that fails because the subscription's headless
# quota is exhausted may be retried on API credits. Ignored for an explicit
# "claude-code" backend: choosing the subscription is a billing decision, and
# quietly moving the user onto paid credits is not the server's call.
FALLBACK_TO_API = SETTINGS.fallback_to_api
MAX_BUDGET_USD = SETTINGS.max_budget_usd
ANSWER_BUDGET = SETTINGS.answer_budget
TRIM_ANSWERS = SETTINGS.trim_answers
# Expose only the tools an agent actually needs and hide the operator tools.
# Every schema is charged against the calling model's context on every turn,
# and a longer tool list measurably degrades tool selection in small models.
MINIMAL_TOOLS = SETTINGS.minimal_tools
ESSENTIAL_TOOLS = ("ask_wisdomtooth", "advisor_status")
SAVE_CONSULTS = SETTINGS.save_consults
CONSULT_KEEP = SETTINGS.consult_keep
USAGE_LOG = SETTINGS.usage_log
MAX_CONSULTS_PER_HOUR = SETTINGS.max_consults_per_hour
MAX_CONSULTS_PER_5H = SETTINGS.max_consults_per_5h
MAX_CONSULTS_PER_WEEK = SETTINGS.max_consults_per_week
MAX_USD_PER_DAY = SETTINGS.max_usd_per_day
REPEAT_WINDOW_S = SETTINGS.repeat_window_s
MAX_CONTEXT_CHARS = SETTINGS.max_context_chars
MAX_TOKENS = SETTINGS.max_tokens
# ADVISOR_LOCK=1 ignores per-call and runtime model/effort/token choices.
LOCKED = SETTINGS.locked

# Where donations go. Empty until the maintainer sets it, and nothing is shown
# anywhere while it is empty. It appears only where a person reads -- the
# startup banner on stderr and the saved transcripts -- never in a tool result,
# which lands in the calling model's context and is the user's to spend.
SUPPORT_URL = ""
SHOW_SUPPORT = SETTINGS.show_support

MODEL_TIERS = dict(models.DEFAULT_TIERS)
MODEL_TIERS.update(SETTINGS.tiers)


def _tier_or_id(value: str) -> str:
    return MODEL_TIERS.get(str(value).lower(), str(value))


# `balanced` rather than `deep` by default: the caller is typically a small
# local model that escalates often, and putting every one of those on Opus
# exhausts a Pro plan's headless quota quickly. `deep` is one argument away.
DEFAULT_MODEL = _tier_or_id(SETTINGS.model)
DEFAULT_EFFORT = SETTINGS.default_effort  # None = the API default

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
    return prompts.answer_budget_instruction(_effective_answer_budget())


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
    question with a 600-word budget ~6. This is the backstop: a streaming CLI
    that goes quiet is stopped much sooner, by ADVISOR_IDLE_TIMEOUT.
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


_caps = models.caps
_supports_fallbacks = models.supports_fallbacks


def _cli_model(model: str) -> str:
    """Map a tier alias or model ID onto what `claude --model` expects."""
    if model.lower() in CLAUDE_CODE_ALIASES:
        return CLAUDE_CODE_ALIASES[model.lower()]
    for tier, model_id in MODEL_TIERS.items():
        if model == model_id and tier in CLAUDE_CODE_ALIASES:
            return CLAUDE_CODE_ALIASES[tier]
    return model  # full model names are accepted by the CLI as-is


def _build_kwargs(model: str, effort: Optional[str], max_tokens: int = 0) -> dict:
    """Assemble the Messages API request body for one consult."""
    return models.build_kwargs(model, effort, _resolve_max_tokens(max_tokens))


def _build_system_prompt() -> str:
    """The advisor persona.

    Replace it wholesale (ADVISOR_SYSTEM_PROMPT / _FILE) to make the advisor a
    domain specialist, or extend it (ADVISOR_SYSTEM_PROMPT_EXTRA) to add house
    rules without losing the escalation framing.
    """
    prompt = prompts.BUILTIN_SYSTEM_PROMPT
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


BUILTIN_SYSTEM_PROMPT = prompts.BUILTIN_SYSTEM_PROMPT
ADVISOR_SYSTEM_PROMPT = _build_system_prompt()
WHEN_TO_USE = prompts.WHEN_TO_USE

_server_kwargs = dict(name="wisdomtooth", instructions=WHEN_TO_USE)
if _MCP_MAJOR >= 2:
    _server_kwargs["version"] = __version__
else:
    # 1.x takes the HTTP bind address here, and derives its DNS-rebinding
    # protection from it; 2.x takes it when the HTTP app is built.
    _server_kwargs.update(host=HTTP_HOST, port=HTTP_PORT)
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
    turn minimal mode off, and the test suite calls the functions directly.
    Only the advertised schema shrinks.

    A plain `def` is registered through an async wrapper that runs it in a
    worker thread. mcp 1.x calls a sync tool on the event loop itself, so a
    status call that probes a slow CLI used to freeze every other request,
    heartbeats included. The module keeps the original function.
    """
    def decorate(fn):
        if MINIMAL_TOOLS and fn.__name__ not in ESSENTIAL_TOOLS:
            return fn
        target = fn
        if not inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def target(*args, **kwargs):
                return await asyncio.to_thread(fn, *args, **kwargs)
        mcp.tool(title=title, annotations=annotations)(target)
        return fn
    return decorate


_client: Optional[Anthropic] = None


def client() -> Anthropic:
    global _client
    if _client is None:
        # Bounded timeout so a stuck request errors out instead of hanging the
        # MCP tool call. Every request streams, so this is also the longest
        # the API may go quiet between events.
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
# Outbound-content safety
# ---------------------------------------------------------------------------

SECRET_PATTERNS = safety.SECRET_PATTERNS
_NSFW_MAP = safety.load_nsfw_map(os.environ.get("ADVISOR_NSFW_EXTRA_JSON"))
_NSFW_RE = safety.compile_words(_NSFW_MAP)


def _scrub_nsfw(text: str) -> str:
    if _NSFW_RE is None or not _flag("nsfw_scrub", False):
        return text
    return safety.scrub_words(text, _NSFW_RE, _NSFW_MAP)


def _truncate(text: str, limit: Optional[int] = None) -> str:
    limit = MAX_CONTEXT_CHARS if limit is None else max(int(limit), 200)
    return safety.truncate(text, limit)


def _sanitize(text: str, scrub: bool = True) -> str:
    """Redact secrets, optionally scrub words, then cap the size.

    Secret redaction is never optional -- `scrub` only controls the word
    substitution, which must not run over code that is being reviewed verbatim.
    """
    text = safety.redact(text)
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

def _file_roots() -> list:
    raw = _setting("file_roots")
    if raw:
        items = raw if isinstance(raw, list) else str(raw).split(os.pathsep)
    else:
        cwd = os.getcwd()
        items = [] if safety.too_broad(cwd) else [cwd]
    return [os.path.realpath(os.path.expanduser(str(p).strip()))
            for p in items if str(p).strip()]


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
    limit = safety.MAX_CONTEXT_FILES
    blocks, notes = [], []
    for raw in paths[:limit]:
        candidate = raw if os.path.isabs(raw) else os.path.join(roots[0], raw)
        real = os.path.realpath(candidate)
        root = next((r for r in roots if safety.inside(real, r)), None)
        if root is None:
            notes.append(f"{raw}: refused, outside the allowed folders")
            continue
        rel = os.path.relpath(real, root)
        if (safety.inside(real, os.path.realpath(_state_dir()))
                or safety.refused(rel.replace("\\", "/").split("/"))):
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
    if len(paths) > limit:
        notes.append(f"only the first {limit} files were read")
    if not blocks:
        raise AdvisorInputError(
            "None of context_files could be attached: "
            + "; ".join(notes or ["no paths given"])
            + ". Allowed folders: " + ", ".join(roots) + ".")
    if notes:
        blocks.append("<file_notes>\n" + "\n".join(notes) + "\n</file_notes>")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Transcripts and follow-ups
# ---------------------------------------------------------------------------
# What is saved is what was actually sent, after secret redaction, so a
# transcript never becomes a second copy of a key this server just declined to
# transmit. Disable with ADVISOR_SAVE_CONSULTS=0. A follow-up names an earlier
# transcript and the server re-sends that exchange, so the caller neither
# pastes the old exchange nor pays for it in its own context.

CONSULT_SUFFIX = transcripts.SUFFIX
_slug = transcripts.slug


def _consult_dir() -> str:
    """Where transcripts are written. Read per call rather than at import."""
    return os.path.abspath(str(_setting("consult_dir")
                               or os.path.join(_state_dir(), "consults")))


def _save_consult(kind: str, topic: str, sent: str, answer: str,
                  footer: str) -> Optional[str]:
    """Write one transcript; return its path, or None. Never raises."""
    if not SAVE_CONSULTS:
        return None
    directory = _consult_dir()
    path = transcripts.save(directory, kind, topic, transcripts.render(
        kind, topic, sent, answer, footer, __version__, _support_line()))
    if path:
        transcripts.prune(directory, CONSULT_KEEP)
    return path


def _result_blocks(answer: str, saved: dict) -> list:
    """The tool result: the answer text plus a link to its transcript.

    The return annotation is deliberately omitted, here and on the tools that
    return this. Annotating a content-block list makes mcp 1.x derive an output
    schema and echo every block back a second time as structured JSON, while
    mcp 2.x suppresses the schema; leaving it off behaves identically on both.
    """
    return transcripts.result_blocks(answer, saved.get("path"))


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
    if transcripts.NAME.match(name):
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
    parts = transcripts.split(text)
    if parts is None:
        raise AdvisorInputError(f"{name} is not a Wisdomtooth transcript.")
    sent, answer = parts
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


# An answer is trimmed for the caller only when it runs this far past the
# budget. The budget is a request to the model, not a cap -- the CLI has no
# token limit -- so modest overshoot is normal and left alone.
_TRIM_OVER = 1.5


def _trim_to_budget(answer: str, budget: int) -> Optional[str]:
    """The lead of an answer far over `budget` words, or None to keep it all.

    Cuts between paragraphs, so the caller gets whole thoughts; if that would
    leave less than half the budget, the next paragraph is cut at a word. A
    code fence left open by the cut is closed, so the rest of the reply does
    not render as code.
    """
    if len(answer.split()) <= budget * _TRIM_OVER:
        return None
    kept, count = [], 0
    for para in re.split(r"\n[ \t]*\n", answer.strip()):
        n = len(para.split())
        if count + n > budget:
            if count < budget // 2 or not kept:
                cut = list(re.finditer(r"\S+", para))[budget - count - 1]
                kept.append(para[:cut.end()] + " ...")
            break
        kept.append(para)
        count += n
    lead = "\n\n".join(kept)
    if lead.count("```") % 2:
        lead += "\n```"
    return lead


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------

# The record of the consult in flight. A context variable rather than a
# parameter: the backends' signatures are a contract tests and callers rely on,
# and `asyncio.to_thread` carries the variable into the worker thread.
_USAGE: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar(
    "wisdomtooth_usage", default=None)

_SESSION_RECORDS: list = []  # this process's records, used when the ledger is off
_LEDGER_LOCK = threading.Lock()
_usage_record = usage.usage_record
_billed = usage.billed
_fmt_usd = usage.fmt_usd
_fmt_tokens = usage.fmt_tokens
_summarise = usage.summarise
_usage_footer = usage.usage_footer


def _note_usage(**fields) -> None:
    """Record what a backend learned about the consult it just ran."""
    record = _USAGE.get()
    if record is not None:
        record.update({k: v for k, v in fields.items() if v is not None})


def _estimate_cost(model: str, input_tokens: int = 0, output_tokens: int = 0,
                   cache_read: int = 0, cache_write: int = 0) -> Optional[float]:
    return usage.estimate_cost(_tier_or_id(model), input_tokens, output_tokens,
                               cache_read, cache_write)


def _cli_usage(payload: dict, model: str) -> dict:
    return usage.cli_usage(payload, _tier_or_id(model))


def _api_usage(message, model: str) -> dict:
    return usage.api_usage(message, _tier_or_id(model))


def _usage_path() -> str:
    return os.path.abspath(str(_setting("usage_file")
                               or os.path.join(_state_dir(), "usage.jsonl")))


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
            if os.path.getsize(path) > usage.LEDGER_MAX_BYTES:
                usage.compact_ledger(path)
    except OSError as exc:
        print(f"[wisdomtooth] could not write the usage ledger {path}: {exc}",
              file=sys.stderr)


def _usage_records(since: float) -> list:
    if USAGE_LOG:
        source = usage.read_ledger(_usage_path())
    else:
        with _LEDGER_LOCK:
            source = list(_SESSION_RECORDS)
    return [r for r in source if float(r.get("ts", 0)) >= since]


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
    problem = usage.cap_violation(_usage_records(now - 7 * 86400), now, caps,
                                  MAX_USD_PER_DAY)
    if problem:
        raise AdvisorError(problem)


def _usage_line() -> str:
    now = time.time()
    return usage.status_line(_usage_records(now - 7 * 86400), now)


def _usage_report(days: int = 7) -> str:
    try:
        days = max(1, min(int(days or 7), usage.LEDGER_KEEP_DAYS))
    except (TypeError, ValueError):
        days = 7
    now = time.time()
    where = (_usage_path() if USAGE_LOG
             else "disabled (ADVISOR_USAGE_LOG=0), so this server process only")
    return usage.report(
        _usage_records(now - max(days, 7) * 86400), now, days, where,
        _caps_description() or "none set (ADVISOR_MAX_CONSULTS_PER_HOUR / "
                               "_5H / _WEEK, ADVISOR_MAX_USD_PER_DAY)",
        f"identical consults within {REPEAT_WINDOW_S / 60:g} min return the "
        "saved answer at no cost" if REPEAT_WINDOW_S else "off")


# ---------------------------------------------------------------------------
# Repeat guard
# ---------------------------------------------------------------------------
# Minutes during which an identical consult returns the earlier answer instead
# of paying for it again. A looping agent re-asks word for word; the rules ask
# it not to, and this makes it free when it does anyway.

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
# The Claude Code CLI backend
# ---------------------------------------------------------------------------

def _claude_bin() -> Optional[str]:
    import shutil
    return os.environ.get("ADVISOR_CLAUDE_BIN") or shutil.which("claude")


def _run_claude(cmd, env=None, workdir=None, timeout_s=60, stdin_text="",
                stream=False, idle_s=0):
    """Run the CLI; `stream` reads `stream-json` output with an idle limit."""
    if stream:
        return claude_cli.run_streaming(cmd, env, workdir, timeout_s,
                                        stdin_text, idle_s=idle_s)
    return claude_cli.run(cmd, env, workdir, timeout_s, stdin_text)


_FEATURE_CACHE: dict = {}
# Everything `stream-json` output needs: print mode refuses it without
# --verbose, and without partial messages a long answer arrives as one silent
# block at the end, which an idle limit would mistake for a hang.
_STREAM_FEATURES = {"--output-format", "--verbose", "--include-partial-messages",
                    "stream-json"}


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
                     "--setting-sources", "--max-budget-usd", "--verbose",
                     "--include-partial-messages"):
            if flag in text:
                flags.add(flag)
        if "stream-json" in text:
            flags.add("stream-json")
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
_AUTH_HELP = prompts.AUTH_HELP


def _stderr_tail(exc) -> str:
    tail = exc.stderr or ""
    tail = tail.decode(errors="replace") if isinstance(tail, bytes) else tail
    return tail[-400:]


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

    # Streamed where the CLI can: each line proves the consult is alive, so a
    # stall is caught by silence instead of at the wall clock, and the
    # heartbeat can say what Claude is doing.
    stream = _STREAM_FEATURES <= features
    add("--output-format", "stream-json" if stream else "json")
    if stream:
        cmd += ["--verbose", "--include-partial-messages"]
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
    run_kw = dict(stream=True, idle_s=IDLE_TIMEOUT) if stream else {}
    try:
        result = _run_claude(cmd, _child_env(), _workdir(), timeout_s,
                             user_content, **run_kw)
    except claude_cli.IdleTimeout as exc:
        raise AdvisorError(
            f"claude CLI went silent: no output for {exc.idle_s:g}s, "
            f"{exc.elapsed}s into the consult, so it was stopped. A working "
            "consult streams something every few seconds, even while it "
            "thinks, so this is a stall, not a slow answer. Likely causes: "
            "(1) a network drop or an API outage; (2) a login or first-run "
            "prompt the CLI is waiting on -- run `claude` in a terminal once; "
            "(3) a CLI fault -- check `claude --version` and update. If "
            "consults on this machine legitimately pause longer, the user can "
            f"raise ADVISOR_IDLE_TIMEOUT (now {exc.idle_s:g}s; 0 turns it "
            "off). Do NOT retry in a loop; tell the user. stderr tail: "
            + _stderr_tail(exc)) from exc
    except subprocess.TimeoutExpired as exc:
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
            "user. stderr tail: " + _stderr_tail(exc)
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
        payload = payload or {}
        if payload.get("subtype") == "error_max_budget_usd":
            cost = payload.get("total_cost_usd")
            raise AdvisorError(
                "The consult reached the per-consult spend cap "
                f"(ADVISOR_MAX_BUDGET_USD={MAX_BUDGET_USD}) and the CLI stopped "
                "it before it finished"
                + (f"; about {_fmt_usd(float(cost))} was used"
                   if isinstance(cost, (int, float)) else "")
                + ". Tell the user; they can raise or unset "
                "ADVISOR_MAX_BUDGET_USD. Do NOT retry unchanged.")
        # Error results carry an `errors` list and no `result` text.
        errors = "; ".join(str(e) for e in payload.get("errors") or [] if e)
        detail = (answer or errors or result.stderr or result.stdout
                  or "").strip()[:500]
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
# One probe at a time: the startup banner and the first tool call can both ask
# before either has an answer, and each probe spawns the CLI.
_BACKEND_LOCK = threading.Lock()


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

    Returns a registered backend name, or "unavailable".
    """
    global _ACTIVE_BACKEND
    if BACKEND in _BACKENDS:
        return BACKEND  # an explicit choice is never second-guessed
    with _BACKEND_LOCK:
        if _ACTIVE_BACKEND is None:
            status = _cli_auth_status()
            if (status and status.get("loggedIn")) or (_oauth_token()
                                                       and _claude_bin()):
                _ACTIVE_BACKEND = "claude-code"
            elif _api_credentials_present():
                _ACTIVE_BACKEND = "api"
            else:
                _ACTIVE_BACKEND = "unavailable"
        return _ACTIVE_BACKEND


# ---------------------------------------------------------------------------
# Connecting an account
# ---------------------------------------------------------------------------

# Seconds between "is the browser sign-in done yet?" checks. A module constant
# so tests can drive the poll loop without patching the stdlib.
_LOGIN_POLL_SECONDS = 3.0
_MANUAL_LOGIN = prompts.MANUAL_LOGIN
_NO_CREDENTIALS = prompts.NO_CREDENTIALS


def _describe_account(status: dict) -> str:
    return "account={} method={} plan={}".format(
        status.get("email", "?"), status.get("authMethod", "?"),
        status.get("subscriptionType", "?"))


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


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

_BACKENDS: dict = {}


def register_backend(backend: Backend) -> None:
    """Make `backend` selectable with ADVISOR_BACKEND=<backend.name>."""
    _BACKENDS[backend.name] = backend


# Lambdas rather than the functions themselves: they look the function up at
# call time, so a replaced module attribute (tests, embedding) is honoured.
register_backend(Backend(
    name="claude-code", provider="anthropic",
    billing="Claude SUBSCRIPTION (Pro/Max) via the local Claude Code CLI",
    billed_to="SUBSCRIPTION",
    consult=lambda s, u, m, e, t: _consult_claude_code(s, u, m, e),
    available=lambda: bool(_claude_bin()),
    label=lambda model: "claude-code/" + _cli_model(model),
    fallback="api", unavailable="CLI not found"))
register_backend(Backend(
    name="api", provider="anthropic",
    billing="API ACCOUNT (pay-per-token via ANTHROPIC_API_KEY)",
    billed_to="API ACCOUNT",
    consult=lambda s, u, m, e, t: _consult_api(s, u, m, e, t),
    available=lambda: _api_credentials_present(),
    honours_max_tokens=True, label=lambda model: model,
    unavailable="no credentials"))

if BACKEND != "auto" and BACKEND not in _BACKENDS:
    print(f"[wisdomtooth] ADVISOR_BACKEND={BACKEND!r} is not a built-in backend "
          f"(auto, {', '.join(_BACKENDS)}); unless code embedding the server "
          "registers it, the server behaves as `auto`.", file=sys.stderr)


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

    # The transcript keeps the whole answer; the caller may get only its lead.
    path = _save_consult(kind, question, user_content, answer, footer)
    if saved is not None:
        saved["path"] = path
    shown = answer
    budget = _effective_answer_budget()
    if path and TRIM_ANSWERS and budget:
        lead = _trim_to_budget(answer, budget)
        if lead is not None:
            kept = len(lead.split())
            shown = lead + prompts.trim_note(kept, len(answer.split()), budget)
            record["trimmed_to_words"] = kept
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
    _remember(key, answer=shown, footer=footer, path=path)
    return shown + footer


def _consult_backend(system: str, user_content: str, model: str,
                     effort: Optional[str], max_tokens: int,
                     backend: str) -> tuple:
    """Run one consult on `backend`; return (answer, billing footer)."""
    spec = _BACKENDS[backend]
    try:
        answer = spec.consult(system, user_content, model, effort, max_tokens)
    except UsageLimitError:
        # Only "auto" may switch the payer, and only for an exhausted plan:
        # the user asked for whatever works. An explicit backend reports the
        # limit instead, because moving someone onto paid credits is their
        # decision, not the server's.
        target = _BACKENDS.get(spec.fallback or "")
        if not (target and BACKEND == "auto" and FALLBACK_TO_API
                and target.available()):
            raise
        _note_usage(backend=target.name)
        answer = target.consult(system, user_content, model, effort, max_tokens)
        return answer, (
            f"\n\n---\n[advisor: {target.label_for(model)} · billed to "
            f"{target.billed_to or target.billing} (pay-per-token) — the "
            "Claude subscription's headless usage limit was exhausted, so this "
            "consult fell back to API credits. Tell the user.]")
    return answer, spec.footer(model, effort, max_tokens,
                               _resolve_max_tokens(max_tokens), LOCKED)


def _backends_line() -> str:
    return ", ".join(
        f"{name} ({'ready' if spec.available() else spec.unavailable})"
        for name, spec in _BACKENDS.items())


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

    honoured = [n for n, spec in _BACKENDS.items() if spec.honours_max_tokens]
    lines.append("")
    lines.append("EFFORT: " + ", ".join(VALID_EFFORT)
                 + " (higher = deeper reasoning, more tokens, more latency;"
                 " 'xhigh' suits hard coding and agentic problems)")
    lines.append(f"MAX TOKENS: per-call override up to {MAX_TOKENS_CEILING}; "
                 f"currently {_effective_max_tokens()}. honoured by: "
                 + (", ".join(honoured) or "none")
                 + " -- the other backends have no equivalent setting.")
    lines.append(f"ANSWER BUDGET: {_effective_answer_budget() or 'unlimited'} "
                 "words — the target length of the advice, honoured on every "
                 "backend. Change it with advisor_configure(answer_budget=N).")
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
    heartbeat runs unconditionally. When the CLI streams, the message also
    says what Claude is doing.
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
            note = holder.note
            try:
                await ctx.report_progress(
                    elapsed, message=f"{message} ({elapsed}s elapsed"
                    + (f", {note}" if note else "") + ")")
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
    return _status_report()


def _status_report() -> str:
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
        transcripts_line = "disabled (ADVISOR_SAVE_CONSULTS=0)"
    elif CONSULT_KEEP:
        transcripts_line = f"{_consult_dir()} (newest {CONSULT_KEEP} kept)"
    else:
        transcripts_line = f"{_consult_dir()} (all kept)"
    budget = _effective_answer_budget()
    lines = [
        f"version: {__version__}",
        f"mcp sdk: {_MCP_MAJOR}.x",
        f"configured backend: {BACKEND}",
        f"active backend: {backend}",
        f"billing: {billing}",
        f"backends: {_backends_line()}",
        f"default model: {DEFAULT_MODEL}",
        f"default effort: {DEFAULT_EFFORT or '(API default)'}",
        f"max tokens: {_effective_max_tokens()}",
        f"answer budget: {budget or 'unlimited'} words"
        + ("" if budget else
           " (a long answer can overflow a small caller's context)"),
        "answer trimming: " + (
            f"an answer over {budget * _TRIM_OVER:g} words returns its lead, "
            "with the rest in its transcript" if TRIM_ANSWERS and budget
            and SAVE_CONSULTS else "off"),
        f"tool surface: {'minimal (ask_wisdomtooth, advisor_status)' if MINIMAL_TOOLS else 'full (11 tools)'}",
        f"consult transcripts: {transcripts_line}",
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
        "inactivity limit: " + (
            f"{IDLE_TIMEOUT:g}s without streamed output stops a consult"
            if IDLE_TIMEOUT else "off (ADVISOR_IDLE_TIMEOUT=0)"),
        f"progress heartbeat: every {PROGRESS_INTERVAL}s while a consult runs",
        f"per-call overrides: "
        f"{'LOCKED (env defaults always win)' if LOCKED else 'allowed'}",
        f"transport: {TRANSPORT}" + (
            f" on {HTTP_HOST}:{HTTP_PORT}, " + (
                "bearer token required" if HTTP_TOKEN else "no authentication")
            if TRANSPORT == "http" else ""),
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
    if backend in _BACKENDS:
        return (f"[wisdomtooth v{__version__}] backend={backend} → "
                + _BACKENDS[backend].billing)
    return (f"[wisdomtooth v{__version__}] no usable credentials — every "
            "consult will fail until you log in Claude Code (`claude` → "
            "`/login`) or set ANTHROPIC_API_KEY. Run advisor_auth_check.")


def _announce() -> None:
    """Print the billing banner once the backend is known.

    Run in a thread: resolving `auto` runs `claude auth status`, which can take
    as long as its 30s timeout, and the MCP handshake must not wait on it --
    Kilo gives a server 30s to connect. Resolving here also warms the cache
    for the first consult.
    """
    try:
        print(_billing_banner(), file=sys.stderr)
    except Exception as exc:  # the banner is information, never a failure
        print(f"[wisdomtooth] could not resolve the backend: {exc}",
              file=sys.stderr)


def _http_app():
    app = (mcp.streamable_http_app(host=HTTP_HOST) if _MCP_MAJOR >= 2
           else mcp.streamable_http_app())
    return httpauth.BearerAuth(app, HTTP_TOKEN) if HTTP_TOKEN else app


def _serve_http() -> int:
    problem = httpauth.exposure_problem(HTTP_HOST, HTTP_TOKEN, HTTP_NO_AUTH)
    if problem:
        print("[wisdomtooth] " + problem, file=sys.stderr)
        return 2
    if not HTTP_TOKEN and not httpauth.is_loopback(HTTP_HOST):
        print(f"[wisdomtooth] WARNING: serving HTTP on {HTTP_HOST}:{HTTP_PORT} "
              "without a token (ADVISOR_HTTP_NO_AUTH=1): whatever guards this "
              "port is all that stands between it and the account.",
              file=sys.stderr)
    import uvicorn
    uvicorn.run(_http_app(), host=HTTP_HOST, port=HTTP_PORT, log_level="info")
    return 0


_USAGE_TEXT = (
    "usage: wisdomtooth-mcp            start the MCP server (stdio; HTTP with "
    "ADVISOR_TRANSPORT=http)\n"
    "       wisdomtooth-mcp doctor     check the setup and print a config for "
    "your MCP client\n"
    "       wisdomtooth-mcp --version")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        command = args[0]
        if command in ("--version", "-V", "version"):
            print(f"wisdomtooth-mcp {__version__}")
            return 0
        if command == "doctor":
            return doctor.run(args[1:], report=_auth_report,
                              version=__version__)
        if command in ("-h", "--help", "help"):
            print(_USAGE_TEXT)
            return 0
        print(f"unknown command {command!r}\n{_USAGE_TEXT}", file=sys.stderr)
        return 2

    threading.Thread(target=_announce, name="wisdomtooth-banner",
                     daemon=True).start()
    support = _support_line()
    if support:
        print(f"[wisdomtooth] free and open source; support it at {support} "
              "(ADVISOR_SHOW_SUPPORT=0 hides this line)", file=sys.stderr)
    if TRANSPORT == "http":
        return _serve_http()
    mcp.run()  # stdio transport
    return 0


if __name__ == "__main__":
    sys.exit(main())
