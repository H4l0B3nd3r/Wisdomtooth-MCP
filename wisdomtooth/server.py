"""Wisdomtooth MCP Server.

Exposes a frontier model as an *escalation* advisor over MCP. Other agents
(Kilo Code, Cursor, Cline, custom agents) call it when they are stuck -- after
their own attempts and doc lookups (e.g. Context7) have not resolved the
problem.

Claude is the default advisor. Others can be connected alongside it --
ChatGPT, Gemini, OpenRouter, or a local model in LM Studio or Ollama, anything
that speaks the OpenAI chat-completions protocol -- and chosen per consult
with `advisor=`, or asked together with `multi_advisor`.

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
  advisors     the named advisors, provider presets, the advisor store
  openai_compat the OpenAI chat-completions client every non-Claude advisor uses
  accounts     what each connected account has left (plan meter, credit)
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
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

from . import (accounts, advisors, claude_cli, config, doctor, httpauth, models,
               openai_compat, prompts, safety, transcripts, usage)
from .backends import Backend
from .claude_cli import CANCELLATION as _CANCELLATION
from .claude_cli import Cancellation as _Cancellation
from .claude_cli import spawn_login_console as _spawn_login_console
from .config import CONFIG_KEYS
from .errors import (AdvisorError, AdvisorInputError, OverLimitError,
                     UsageLimitError)
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


ADVISOR_SYSTEM_PROMPT = _build_system_prompt()

_server_kwargs = dict(name="wisdomtooth", instructions=prompts.WHEN_TO_USE)
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


_TOOL_NAMES: list = []  # what this process advertises


def tool(title: str, annotations: ToolAnnotations):
    """Register a function as an MCP tool, honouring ADVISOR_MINIMAL_TOOLS.

    A hidden tool is still a normal module-level function -- the operator can
    turn minimal mode off, and the test suite calls the functions directly.
    Only the advertised schema shrinks.

    A plain `def` is registered through an async wrapper that runs it in a
    worker thread. mcp 1.x calls a sync tool on the event loop itself, so a
    status call that probes a slow CLI would freeze every other request,
    heartbeats included. The module keeps the original function.
    """
    def decorate(fn):
        if MINIMAL_TOOLS and fn.__name__ not in ESSENTIAL_TOOLS:
            return fn
        _TOOL_NAMES.append(fn.__name__)
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
_fmt_usd = usage.fmt_usd
_summarise = usage.summarise


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
    records = _usage_records(now - max(days, 7) * 86400)
    by_advisor: dict = {}
    for r in records:
        if float(r["ts"]) >= now - days * 86400 and r.get("status") == "ok":
            by_advisor.setdefault(r.get("advisor") or advisors.CLAUDE,
                                  []).append(r)
    sections = [f"BY ADVISOR, last {days} day(s):"]
    if not by_advisor:
        sections.append("  (no consults)")
    for name, rows in sorted(by_advisor.items()):
        s = _summarise(rows)
        sections.append(
            f"  {name:<22} {s['consults']:>4} consults  "
            f"{usage.fmt_tokens(s['input'])} in / {usage.fmt_tokens(s['output'])} out"
            + (f"  ≈{_fmt_usd(s['cost'])}" if s["cost"] is not None else ""))
    balances = _balance_lines()
    sections += ["", "BALANCES:"] + (["  " + b for b in balances] or [
        "  none reported yet"])
    return usage.report(
        records, now, days, where,
        _caps_description() or "none set (ADVISOR_MAX_CONSULTS_PER_HOUR / "
                               "_5H / _WEEK, ADVISOR_MAX_USD_PER_DAY)",
        f"identical consults within {REPEAT_WINDOW_S / 60:g} min return the "
        "saved answer at no cost" if REPEAT_WINDOW_S else "off",
        sections=sections)


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
    return _setting("claude_bin") or shutil.which("claude")


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


# What `--model` may carry. On Windows an npm-installed CLI runs through
# cmd.exe, so a model name is kept to characters that are never shell syntax.
_CLI_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]*$")

_AUTH_SIGNATURES = ("/login", "not logged in", "invalid api key", "authentication",
                    "oauth", "token expired", "credential", "unauthorized",
                    "please log in")
_LIMIT_SIGNATURES = ("usage limit", "rate limit", "quota", "limit reached",
                     "upgrade to", "resets at")


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

    cli_model = _cli_model(model)
    if not _CLI_MODEL.match(cli_model):
        raise AdvisorInputError(
            f"{model!r} is not a model name. Use a tier (fast, balanced, "
            "deep) or a model ID such as 'claude-sonnet-5'.")
    features = _cli_features(claude_bin)
    cmd = [claude_bin, "-p", "--model", cli_model]

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

    answer, payload = claude_cli.parse_output(result.stdout or "")
    if payload:
        # Before the failure checks: a failed run can still have been billed.
        _note_usage(**_cli_usage(payload, model))
        # The plan's own meter, when the CLI streamed one.
        _note_usage(rate_limit_info=payload.get("rate_limit_info"))
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
            raise AdvisorError(prompts.AUTH_HELP + detail)
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


def _api_failure(exc: "anthropic.APIError") -> AdvisorError:
    """The message the agent sees for a failed API request."""
    name = exc.__class__.__name__
    if isinstance(exc, anthropic.AuthenticationError):
        return AdvisorError(
            "AUTH FAILURE on the API backend: ANTHROPIC_API_KEY is missing, "
            "invalid, or revoked. Verify it at console.anthropic.com and set "
            "it in the MCP server config env, then restart the server entry. "
            "If the user intended SUBSCRIPTION billing, use "
            "ADVISOR_BACKEND=claude-code (no API key needed). Do not retry "
            f"until fixed. ({name})")
    if isinstance(exc, anthropic.RateLimitError):
        return AdvisorError(
            f"The Anthropic API is rate-limiting this account ({name}: {exc}). "
            "Retry at most once, after a pause; if it persists, tell the user.")
    if isinstance(exc, anthropic.APIConnectionError):
        return AdvisorError(
            f"Could not reach the Anthropic API ({name}: {exc}). Check the "
            "network or proxy. Retry at most once, then tell the user.")
    if (isinstance(exc, anthropic.InternalServerError)
            or (getattr(exc, "status_code", 0) or 0) >= 500):
        return AdvisorError(
            f"The Anthropic API failed on its side ({name}: {exc}), usually "
            "because it is overloaded. Retry at most once, after a pause, then "
            "tell the user.")
    return AdvisorError(f"The Anthropic API refused the request ({name}: "
                        f"{exc}). Do not retry it unchanged.")


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
    try:
        if _supports_fallbacks(model):
            # A refused consult should be rescued on another model rather than
            # returning nothing. Degrade quietly if the installed SDK is older
            # than the parameter, or the account/platform does not offer it.
            try:
                message = _stream(api.beta.messages, system=system,
                                  messages=messages, betas=[FALLBACK_BETA],
                                  fallbacks="default", **kwargs)
            except (TypeError, anthropic.BadRequestError,
                    anthropic.NotFoundError, anthropic.PermissionDeniedError):
                message = None
        if message is None:
            try:
                message = _stream(api.messages, system=system,
                                  messages=messages, **kwargs)
            except anthropic.BadRequestError:
                # Capability drift (new or renamed models): retry as a plain
                # request.
                kwargs.pop("output_config", None)
                kwargs.pop("thinking", None)
                message = _stream(api.messages, system=system,
                                  messages=messages, **kwargs)
    except anthropic.APIError as exc:
        raise _api_failure(exc) from exc

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
        if _ACTIVE_BACKEND is not None:
            return _ACTIVE_BACKEND
        status = _cli_auth_status()
        if (status and status.get("loggedIn")) or (_oauth_token()
                                                   and _claude_bin()):
            resolved = "claude-code"
        elif _api_credentials_present():
            resolved = "api"
        else:
            # Not cached: a user who signs in or sets a key after the server
            # started should not have to restart it.
            return "unavailable"
        _ACTIVE_BACKEND = resolved
        return resolved


# ---------------------------------------------------------------------------
# Connecting an account
# ---------------------------------------------------------------------------

# Seconds between "is the browser sign-in done yet?" checks. A module constant
# so tests can drive the poll loop without patching the stdlib.
_LOGIN_POLL_SECONDS = 3.0


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
                "or over plain SSH.\n\n" + prompts.MANUAL_LOGIN)

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
            + prompts.MANUAL_LOGIN)


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


def _prepare(question: str, context: str = "", extra_system: str = "",
             scrub_context: bool = True, context_files: Optional[list] = None,
             follow_up_of: str = "") -> tuple:
    """(system prompt, question, user content) for one consult.

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
    return system, question, user_content


def _route(spec, model: str, effort: str) -> tuple:
    """(model, effort, backend name) for a consult on `spec`, or raise."""
    if spec.kind == "claude":
        backend = _active_backend()
        if backend == "unavailable":
            raise AdvisorError(prompts.NO_CREDENTIALS)
        return _resolve_model(model), _resolve_effort(effort), backend
    ready, why = _advisor_ready(spec.name)
    if not ready:
        raise AdvisorError(f"advisor {spec.name!r} ({spec.label}) is not "
                           f"ready: {why}. Nothing was sent.")
    return _advisor_model(spec, model), _resolve_effort(effort), spec.provider


def _note_plan(spec, record: dict) -> None:
    """Hand the plan meter the CLI reported to the account meter."""
    info = record.pop("rate_limit_info", None)
    if info and spec.kind == "claude" and record.get("backend") == "claude-code":
        _METER.note_plan(spec.name, info, record.get("cost_usd"))


def _consult(question: str, context: str = "", extra_system: str = "",
             model: str = "", effort: str = "", max_tokens: int = 0,
             scrub_context: bool = True, kind: str = "consult",
             saved: Optional[dict] = None,
             context_files: Optional[list] = None,
             follow_up_of: str = "", advisor: str = "",
             confirm_over_limit: bool = False,
             held_already: bool = False) -> str:
    """One stateless consult, on the named advisor (the default if empty).

    `kind` names the calling tool, for the transcript. `saved` is an optional
    out-parameter: pass a dict and the transcript path lands in it under
    "path", so the tool wrapper can attach a `resource_link` to the same file.
    An out-parameter rather than a richer return type because the answer string
    IS this function's contract -- every caller, and most of the test suite,
    treats it as one.

    A consult that would cost more than its account has left is held
    (OverLimitError) unless `confirm_over_limit`; `held_already` says a
    multi-advisor caller has checked every account at once.
    """
    spec = _advisor_spec(advisor)
    system, question, user_content = _prepare(question, context, extra_system,
                                              scrub_context, context_files,
                                              follow_up_of)
    model, effort, backend = _route(spec, model, effort)

    key = _repeat_key(kind, spec.name, backend, model, effort, max_tokens,
                      system, user_content)
    hit = _recall(key)
    if hit:
        _append_usage(usage.usage_record(kind, backend, model, effort, "repeat",
                                    user_content, transcript=hit["path"],
                                    advisor=spec.name))
        if saved is not None:
            saved["path"] = hit["path"]
        return (hit["answer"] + hit["footer"]
                + "\n[repeat: identical to a consult at "
                + time.strftime("%H:%M", time.localtime(hit["ts"]))
                + " -- this is that answer again, at no cost. To consult "
                "afresh, change the question or add what is new to "
                "`context`.]")
    _check_caps()
    if not held_already:
        _hold_if_over([dict(spec=spec, model=model, effort=effort,
                            backend=backend, max_tokens=max_tokens,
                            system=system, user_content=user_content)],
                      confirm_over_limit, kind)

    record = usage.usage_record(kind, backend, model, effort, "ok", user_content,
                           advisor=spec.name)
    token = _USAGE.set(record)
    started = time.monotonic()
    try:
        answer, footer = _consult_backend(system, user_content, model, effort,
                                          max_tokens, backend, spec)
    except Exception as exc:
        record.update(status="error", error=exc.__class__.__name__,
                      duration_s=round(time.monotonic() - started, 1))
        _note_plan(spec, record)
        _append_usage(record)
        raise
    finally:
        _USAGE.reset(token)
    _note_plan(spec, record)
    record.update(duration_s=round(time.monotonic() - started, 1),
                  answer_chars=len(answer))
    footer += usage.usage_footer(record)
    footer += _balance_footer(spec, record, confirm_over_limit)

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
                     backend: str, advisor=None) -> tuple:
    """Run one consult on `backend`; return (answer, billing footer)."""
    if advisor is not None and advisor.kind == "openai":
        answer = _consult_openai(advisor, system, user_content, model, effort,
                                 max_tokens)
        return answer, _openai_footer(advisor, model)
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
# Advisors
# ---------------------------------------------------------------------------
# `claude` goes through the backends above. Every other advisor is an
# OpenAI-compatible endpoint the user configured or connected; see advisors.py.

def _warn(message: str) -> None:
    print("[wisdomtooth] " + message, file=sys.stderr)


def _advisors_path() -> str:
    return os.path.abspath(str(_setting("advisors_file")
                               or os.path.join(_state_dir(), "advisors.json")))


def _accounts_path() -> str:
    return os.path.abspath(str(_setting("accounts_file")
                               or os.path.join(_state_dir(), "accounts.json")))


_METER = accounts.Meter(_accounts_path)
_ADVISORS: dict = {}
_DEFAULT_ADVISOR = advisors.CLAUDE


def _load_advisors() -> None:
    """(Re)build the advisor registry from env, config file and store."""
    global _ADVISORS, _DEFAULT_ADVISOR
    store = advisors.read_store(_advisors_path(), _warn)
    _ADVISORS = advisors.load(os.environ.get("ADVISOR_ADVISORS_JSON"),
                              _FILE_CONFIG.get("advisors"),
                              store.get("advisors"), _warn)
    # An explicit setting beats the default advisor_connect stored.
    explicit = (os.environ.get("ADVISOR_DEFAULT_ADVISOR")
                or _FILE_CONFIG.get("default_advisor"))
    choice = str(explicit or store.get("default") or advisors.CLAUDE)
    choice = choice.strip().lower()
    if choice not in _ADVISORS:
        _warn(f"default advisor {choice!r} is not configured; using claude. "
              f"Configured: {', '.join(_ADVISORS)}")
        choice = advisors.CLAUDE
    _DEFAULT_ADVISOR = choice


_load_advisors()


def _default_advisor() -> str:
    name = _OVERRIDES.get("advisor") or _DEFAULT_ADVISOR
    return name if name in _ADVISORS else advisors.CLAUDE


def _advisor_choices() -> str:
    return ", ".join(_ADVISORS)


def _advisor_spec(name: str = "") -> advisors.AdvisorSpec:
    key = str(name or "").strip().lower() or _default_advisor()
    spec = _ADVISORS.get(key)
    if spec is None:
        raise AdvisorInputError(
            f"no advisor named {name!r}. Connected: {_advisor_choices()}. To "
            "add one, the user can call advisor_connect (provider openai, "
            "gemini, openrouter, lmstudio, ollama or openai-compatible) or "
            "add it to the config file's `advisors`.")
    return spec


def _advisor_key(spec: advisors.AdvisorSpec) -> str:
    return advisors.key_for(spec, os.environ)


def _advisor_ready(name: str) -> tuple:
    """(ready, why) -- why is "ready" or what is missing."""
    spec = _advisor_spec(name)
    if spec.kind == "claude":
        if _active_backend() == "unavailable":
            return False, "no usable Claude credentials (run advisor_auth_check)"
        return True, "ready"
    if spec.needs_key and not _advisor_key(spec):
        where = (f"set {spec.api_key_env} in the MCP server env, or "
                 if spec.api_key_env else "")
        return False, (f"no API key: {where}call advisor_connect with "
                       f"name={spec.name!r} and api_key")
    return True, "ready"


def _advisor_model(spec: advisors.AdvisorSpec, model: str = "") -> str:
    if spec.kind == "claude":
        return _resolve_model(model)
    if LOCKED or not model:
        model = _OVERRIDES.get("advisor_models", {}).get(spec.name, "")
    return advisors.model_for(spec, model)


def _advisor_effort(spec: advisors.AdvisorSpec,
                    effort: Optional[str]) -> Optional[str]:
    if spec.kind == "claude":
        return effort
    return advisors.effort_for(spec, effort)


def _short_billing(spec: advisors.AdvisorSpec) -> str:
    if spec.local:
        return "LOCAL MODEL (no per-token cost)"
    return {"openai": "OPENAI API ACCOUNT", "gemini": "GEMINI API ACCOUNT",
            "openrouter": "OPENROUTER CREDITS"}.get(
        spec.provider, f"{spec.name.upper()} ACCOUNT")


def _spec_cost(spec: advisors.AdvisorSpec, tokens_in: int, tokens_out: int,
               cached: int = 0) -> Optional[float]:
    if spec.prices is None:
        return None
    rate_in, rate_out = spec.prices
    return round((tokens_in * rate_in + cached * rate_in * usage.CACHE_READ_FACTOR
                  + tokens_out * rate_out) / 1_000_000, 6)


def _openai_usage(spec: advisors.AdvisorSpec, raw: dict) -> dict:
    def number(obj, key):
        try:
            return int((obj or {}).get(key) or 0)
        except (TypeError, ValueError, AttributeError):
            return 0
    prompt = number(raw, "prompt_tokens")
    cached = number(raw.get("prompt_tokens_details") if raw else None,
                    "cached_tokens")
    tokens = dict(input_tokens=max(0, prompt - cached),
                  output_tokens=number(raw, "completion_tokens"),
                  cache_read_tokens=cached, cache_write_tokens=0)
    cost = _spec_cost(spec, tokens["input_tokens"], tokens["output_tokens"],
                      cached)
    return dict(tokens, cost_usd=cost,
                cost_source="estimate" if cost is not None else None)


_THINK_BLOCK = re.compile(r"\A\s*<(think|thinking)>.*?</\1>", re.DOTALL)

# Parameters a compatible server may reject; dropped one at a time on a 400.
_OPTIONAL_PARAMS = ("reasoning_effort", "max_completion_tokens", "max_tokens",
                    "stream_options")


def _consult_openai(spec: advisors.AdvisorSpec, system: str, user_content: str,
                    model: str, effort: Optional[str], max_tokens: int) -> str:
    """One consult on an OpenAI-compatible advisor."""
    key = _advisor_key(spec)
    body = {"model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user_content}],
            "stream": True, "stream_options": {"include_usage": True}}
    sent_effort = advisors.effort_for(spec, effort)
    if sent_effort:
        body["reasoning_effort"] = sent_effort
    if max_tokens and not LOCKED:
        body[spec.max_tokens_param] = _resolve_max_tokens(max_tokens)
    elif spec.send_max_tokens:
        body[spec.max_tokens_param] = _effective_max_tokens()
    _note_usage(sent_effort=sent_effort,
                sent_max_tokens=body.get(spec.max_tokens_param))

    url = spec.base_url + "/chat/completions"
    timeout_s = _consult_timeout(len(system) + len(user_content), effort)
    holder = _CANCELLATION.get()
    dropped: list = []
    who = f"advisor {spec.name!r} ({spec.label})"
    while True:
        try:
            result = openai_compat.chat(url, key, body, timeout_s,
                                        idle_s=IDLE_TIMEOUT, holder=holder)
            break
        except openai_compat.HTTPFailure as exc:
            _METER.note_headers(spec.name, exc.headers)
            if exc.status in (400, 422):
                optional = [p for p in _OPTIONAL_PARAMS if p in body]
                named = [p for p in optional if p in exc.message]
                if optional:
                    for param in named or optional:
                        body.pop(param, None)
                        dropped.append(param)
                    continue
            raise _openai_failure(spec, exc, model) from None
        except openai_compat.Unreachable as exc:
            raise AdvisorError(
                f"Could not reach {who} at {spec.base_url} ({exc}). Is the "
                "server running and the address right? For a local model, "
                "start the server and load the model (LM Studio: `lms server "
                "start`, then load it; Ollama: `ollama serve`). Nothing was "
                "spent. Do NOT retry in a loop; tell the user.") from None
        except openai_compat.Silent as exc:
            raise AdvisorError(
                f"{who} went silent: no data for {exc.idle_s:g}s, "
                f"{exc.elapsed}s into the consult, so it was stopped. A local "
                "model still loading, or a very long prompt on slow hardware, "
                "can do this; otherwise it is a network or server stall. The "
                "user can raise ADVISOR_IDLE_TIMEOUT. Do NOT retry in a loop."
            ) from None
        except openai_compat.WallClock as exc:
            raise AdvisorError(
                f"{who} was still answering after {exc.timeout_s:g}s and was "
                "stopped (ADVISOR_TIMEOUT_MAX caps a consult). Lower the "
                "answer budget or ask a narrower question.") from None
        except openai_compat.Cancelled:
            raise AdvisorError(
                "The consult was cancelled by the client, and the HTTP stream "
                f"to {spec.name} was closed.") from None
    if dropped:
        _note_usage(dropped_params=dropped)
    _note_usage(**_openai_usage(spec, result.usage))
    _METER.note_headers(spec.name, result.headers)
    # Reasoning models on local servers often send their thinking inline, as
    # a leading <think> block, rather than as reasoning_content.
    answer = _THINK_BLOCK.sub("", result.text, count=1).strip()
    if not answer:
        if result.finish_reason == "length":
            return ("[advisor returned no visible text -- it hit its token "
                    "limit while reasoning. Lower effort or raise max_tokens, "
                    "then retry ONCE.]")
        raise AdvisorError(f"{who} returned an empty answer (finish reason: "
                           f"{result.finish_reason or 'none'}).")
    return answer


def _openai_failure(spec, exc, model) -> AdvisorError:
    who = f"advisor {spec.name!r} ({spec.label})"
    detail = f"HTTP {exc.status}: {exc.message}"
    if exc.status in (401, 403):
        env = f" or set {spec.api_key_env}" if spec.api_key_env else ""
        return AdvisorError(
            f"AUTH FAILURE on {who}: the endpoint rejected the API key "
            f"({detail}). The user must fix the key -- call advisor_connect "
            f"with name={spec.name!r} and the right api_key{env}. Do not retry "
            "until then.")
    text = (exc.message + " " + exc.code).lower()
    if exc.status == 429 and any(s in text for s in (
            "quota", "insufficient", "billing", "credit", "exhausted")):
        return UsageLimitError(
            f"{who}'s account has no quota left ({detail}). This is not a bug "
            "and retrying will not help -- do NOT retry; tell the user, who "
            "can add credit or wait for the quota to reset. Other advisors "
            "may still be available (advisor_status).")
    if exc.status == 429:
        wait = exc.headers.get("retry-after")
        return AdvisorError(
            f"{who} is rate-limited ({detail})."
            + (f" The endpoint asks to wait {wait}s before the next request."
               if wait else "")
            + " Do not retry immediately.")
    if exc.status == 404:
        return AdvisorError(
            f"{who}: {detail}. Either the model {model!r} does not exist on "
            f"this endpoint or the base_url ({spec.base_url}) is wrong. "
            "advisor_connect lists the models an endpoint offers.")
    return AdvisorError(f"{who} failed ({detail}).")


def _openai_footer(spec: advisors.AdvisorSpec, model: str) -> str:
    record = _USAGE.get() or {}
    parts = ["advisor: " + f"{spec.name}/{model}",
             "billed to " + _short_billing(spec)]
    if record.get("sent_effort"):
        parts.append("effort=" + record["sent_effort"])
    if record.get("sent_max_tokens"):
        parts.append(f"max_tokens={record['sent_max_tokens']}")
    if record.get("dropped_params"):
        parts.append("the endpoint refused " + ", ".join(record["dropped_params"])
                     + ", so it was dropped")
    return "\n\n---\n[" + " · ".join(parts) + "]"


# ---------------------------------------------------------------------------
# Account balances, and holding a consult that would go over
# ---------------------------------------------------------------------------

# Rough tokens an effort level spends thinking before it answers.
_THINKING_TOKENS = {"minimal": 200, "low": 1000, "medium": 2000,
                    "high": 4000, "xhigh": 8000, "max": 16000}
_TOKENS_PER_WORD = 1.4
_LOW_SHARE = 0.2  # warn below this share of an allowance


def _estimate_split(spec, system: str, user_content: str, max_tokens: int,
                    effort: Optional[str]) -> tuple:
    """(input, output) tokens a consult will probably use. Deliberately rough:
    4 characters a token in, the answer budget plus thinking out."""
    tokens_in = -(-(len(system) + len(user_content)) // 4)
    words = _effective_answer_budget() or _UNBUDGETED_WORDS
    tokens_out = int(words * _TOKENS_PER_WORD) + _THINKING_TOKENS.get(
        (effort or "").lower(), 0)
    if max_tokens and not LOCKED:
        tokens_out = min(tokens_out, int(max_tokens))
    return tokens_in, tokens_out


def _estimate_tokens(spec, system: str, user_content: str, max_tokens: int,
                     effort: Optional[str]) -> int:
    return sum(_estimate_split(spec, system, user_content, max_tokens, effort))


def _record_tokens(record: dict) -> int:
    return sum(int(record.get(k) or 0) for k in (
        "input_tokens", "cache_read_tokens", "cache_write_tokens",
        "output_tokens"))


def _allowance_used(spec, now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    since = now - spec.allowance_seconds
    return sum(_record_tokens(r) for r in usage.billed(_usage_records(since))
               if (r.get("advisor") or advisors.CLAUDE) == spec.name)


def _clock(ts: float) -> str:
    if ts - time.time() > 20 * 3600:
        return time.strftime("%a %H:%M", time.localtime(ts))
    return time.strftime("%H:%M", time.localtime(ts))


def _fresh_credit(spec) -> dict:
    """The key's credit, refetched when the last reading is stale."""
    credit = _METER.credit(spec.name)
    if credit and time.time() - float(credit.get("seen", 0)) < \
            accounts.CREDIT_MAX_AGE_S:
        return credit
    try:
        data = openai_compat.get_json(spec.base_url + "/key",
                                      _advisor_key(spec), timeout_s=8)
        info = data.get("data") if isinstance(data, dict) else None
        if isinstance(info, dict):
            _METER.note_credit(spec.name, info)
    except Exception as exc:  # a balance check must never cost the consult
        print(f"[wisdomtooth] could not read {spec.name}'s credit: {exc}",
              file=sys.stderr)
    return _METER.credit(spec.name)


def _over_limit(ask: dict) -> list:
    """Why `ask` would cost more than its account has left; [] if it fits."""
    spec = ask["spec"]
    tokens_in, tokens_out = _estimate_split(spec, ask["system"],
                                            ask["user_content"],
                                            ask["max_tokens"], ask["effort"])
    total = tokens_in + tokens_out
    problems = []
    if spec.allowance_tokens and spec.allowance_seconds:
        left = spec.allowance_tokens - _allowance_used(spec)
        if total > left:
            problems.append(
                f"{spec.name}: this request needs ≈{total:,} tokens, but "
                f"≈{max(left, 0):,} of its {spec.allowance_tokens:,}-token "
                f"{spec.allowance_window} allowance are left (the allowance "
                "the user declared, measured by the local ledger)")
    if spec.kind == "claude" and ask["backend"] == "claude-code":
        full = [w for w in _METER.plan_windows(spec.name) if w[1] >= 1.0]
        if full:
            label, util, resets = full[0]
            problems.append(
                f"{spec.name}: the Claude subscription's {label} window is at "
                f"{round(util * 100)}% (resets {_clock(resets)}), so the plan "
                "has nothing left for this request")
        else:
            left_usd = _METER.plan_remaining_usd(spec.name)
            cost = usage.estimate_cost(_tier_or_id(ask["model"]), tokens_in,
                                       tokens_out)
            if left_usd is not None and cost is not None and cost > left_usd:
                problems.append(
                    f"{spec.name}: this request needs ≈{_fmt_usd(cost)} of "
                    f"API-equivalent usage, but the subscription has "
                    f"≈{_fmt_usd(left_usd)} left before its tightest window "
                    "fills (measured from the plan's own meter)")
    if spec.balance == "openrouter":
        remaining = _fresh_credit(spec).get("remaining")
        cost = _spec_cost(spec, tokens_in, tokens_out)
        if remaining is not None and (remaining <= 0 or (
                cost is not None and cost > remaining)):
            problems.append(
                f"{spec.name}: this request costs ≈"
                + (_fmt_usd(cost) if cost is not None else "an unknown amount")
                + f", but the key has {_fmt_usd(remaining)} of credit left")
    return problems


def _hold_if_over(asks: list, confirmed: bool, kind: str) -> None:
    """Raise OverLimitError if any request would go over, unless confirmed.

    Every account is checked before anything is sent, so a multi-advisor call
    is held whole rather than half-spent.
    """
    problems = [p for ask in asks for p in _over_limit(ask)]
    if not problems or confirmed:
        return
    for ask in asks:
        _append_usage(usage.usage_record(kind, ask["backend"], ask["model"],
                                    ask["effort"], "held", ask["user_content"],
                                    advisor=ask["spec"].name))
    raise OverLimitError(
        "HELD -- nothing was sent and nothing was spent: this request would "
        "cost more than the account has left.\n"
        + "\n".join("  - " + p for p in problems)
        + "\nAsk the user whether to go ahead anyway (it may be refused by "
        "the provider, or run into overage or pay-per-token billing). If they "
        "say yes, call the same tool again with the same arguments plus "
        "confirm_over_limit=true. Do NOT set confirm_over_limit without "
        "asking them. Estimates are rough (≈4 characters a token in, the "
        "answer budget out).")


def _plan_text(name: str, with_resets: bool = True) -> str:
    parts = []
    for label, util, resets in _METER.plan_windows(name):
        text = f"{label} {round(util * 100)}% used"
        if with_resets:
            text += f" (resets {_clock(resets)})"
        parts.append(text)
    return ", ".join(parts)


def _balance_footer(spec, record: dict, confirmed: bool) -> str:
    """One line under the answer: what the account has left now."""
    parts = []
    if spec.kind == "claude":
        plan = _plan_text(spec.name, with_resets=False)
        if plan:
            parts.append("plan: " + plan.replace(", ", " · "))
    if spec.allowance_tokens and spec.allowance_seconds:
        # The ledger does not hold this consult yet.
        used = _allowance_used(spec) + _record_tokens(record)
        left = spec.allowance_tokens - used
        if left < 0:
            parts.append(
                f"balance: {spec.name} is over its {spec.allowance_tokens:,}-"
                f"token {spec.allowance_window} allowance by ≈{-left:,}"
                + (" -- sent with the user's confirmation" if confirmed else ""))
        else:
            share = left / spec.allowance_tokens
            parts.append(
                f"balance: ≈{left:,} of {spec.allowance_tokens:,} tokens left "
                f"this {spec.allowance_window} ({int(share * 100)}% left)"
                + (" -- LOW; tell the user" if share < _LOW_SHARE else ""))
    limit = _METER.ratelimit(spec.name)
    if limit.get("limit") and limit.get("remaining") is not None and \
            limit["remaining"] < limit["limit"] * _LOW_SHARE:
        parts.append(f"rate limit: {limit['remaining']:,} of "
                     f"{limit['limit']:,} tokens/min left")
    if spec.balance == "openrouter":
        credit = _METER.credit(spec.name)
        if credit.get("remaining") is not None:
            parts.append(f"credit: {_fmt_usd(credit['remaining'])} left")
    return ("\n[" + " · ".join(parts) + "]") if parts else ""


def _balance_lines() -> list:
    """What every connected account has left, for status and usage."""
    lines = []
    for name, spec in _ADVISORS.items():
        if spec.kind == "claude":
            plan = _plan_text(name)
            if plan:
                left = _METER.plan_remaining_usd(name)
                status = _METER.plan_status(name)
                lines.append(
                    f"{name} subscription: {plan}"
                    + (f"; ≈{_fmt_usd(left)} of API-equivalent usage left"
                       if left is not None else "")
                    + (" -- LIMIT REACHED" if status.get("status") == "rejected"
                       else ""))
        if spec.allowance_tokens and spec.allowance_seconds:
            left = spec.allowance_tokens - _allowance_used(spec)
            lines.append(f"{name}: ≈{max(left, 0):,} of "
                         f"{spec.allowance_tokens:,} tokens left this "
                         f"{spec.allowance_window} (declared allowance)")
        limit = _METER.ratelimit(name)
        if limit.get("limit") and limit.get("remaining") is not None:
            lines.append(f"{name}: rate limit {limit['remaining']:,} of "
                         f"{limit['limit']:,} tokens/min left "
                         f"(as of {_clock(float(limit.get('seen', 0)))})")
        if spec.balance == "openrouter" and _advisor_ready(name)[0]:
            credit = _fresh_credit(spec)
            if credit.get("remaining") is not None:
                lines.append(f"{name}: {_fmt_usd(credit['remaining'])} credit "
                             "left" + (f" of {_fmt_usd(credit['limit'])}"
                                       if credit.get("limit") else ""))
    return lines


# ---------------------------------------------------------------------------
# Connecting another advisor
# ---------------------------------------------------------------------------

def _connect_advisor(name: str, provider: str, api_key: str = "",
                     model: str = "", base_url: str = "",
                     allowance_tokens: int = 0, allowance_window: str = "",
                     notes: str = "", make_default: bool = False) -> str:
    key = str(name or "").strip().lower()
    if key == advisors.CLAUDE:
        raise AdvisorInputError(
            "`claude` is built in. Connect the Claude account with "
            "advisor_login (subscription) or ANTHROPIC_API_KEY (API).")
    if not advisors.NAME.match(key):
        raise AdvisorInputError(
            f"advisor names are 1-32 lowercase letters, digits, - or _; got "
            f"{name!r}. Try e.g. 'chatgpt', 'gemini' or 'local'.")
    provider = str(provider or "").strip().lower()
    if provider not in advisors.PROVIDERS:
        raise AdvisorInputError(
            f"unknown provider {provider!r}; use one of "
            f"{', '.join(advisors.PROVIDERS)}.")
    entry = {"provider": provider}
    for field_name, value in (("api_key", api_key), ("model", model),
                              ("base_url", base_url), ("notes", notes),
                              ("allowance_window", allowance_window)):
        if str(value or "").strip():
            entry[field_name] = str(value).strip()
    if allowance_tokens:
        entry["allowance_tokens"] = int(allowance_tokens)
    problems: list = []
    spec = advisors.build(key, entry, problems.append, "stored")
    if spec is None or problems:
        raise AdvisorInputError("; ".join(problems) or "invalid advisor")

    model_id = advisors.model_for(spec, "")
    token = _advisor_key(spec)
    if spec.needs_key and not token:
        raise AdvisorInputError(
            f"provider {provider!r} needs an API key: pass api_key, or set "
            f"{spec.api_key_env} in the MCP server env first.")
    check = ""
    try:
        listing = openai_compat.get_json(spec.base_url + "/models", token,
                                         timeout_s=15)
        offered = [str(m.get("id")) for m in (listing.get("data") or [])
                   if isinstance(m, dict)]
        if offered and model_id not in offered:
            check = (f"Note: the endpoint does not list the model {model_id!r}"
                     f"; it offers: {', '.join(offered[:20])}"
                     + (" ..." if len(offered) > 20 else "")
                     + ". Pass model= to change it.")
        else:
            check = f"Verified: the endpoint answered and offers {model_id!r}."
    except openai_compat.HTTPFailure as exc:
        if exc.status in (401, 403):
            raise AdvisorError(
                f"{spec.base_url} rejected the API key (HTTP {exc.status}: "
                f"{exc.message}). Nothing was saved. Check the key and call "
                "advisor_connect again.") from None
        check = (f"Could not list the endpoint's models (HTTP {exc.status}); "
                 "saved anyway -- the first consult will show whether it "
                 "works.")
    except (openai_compat.Unreachable, openai_compat.Silent) as exc:
        check = (f"Could not reach {spec.base_url} right now ({exc}); saved "
                 "anyway. Start the server before consulting this advisor.")

    path = _advisors_path()
    store = advisors.read_store(path, _warn)
    store.setdefault("advisors", {})[key] = entry
    if make_default:
        store["default"] = key
        _OVERRIDES.pop("advisor", None)
    advisors.write_store(path, store)
    _load_advisors()
    shadowed = _ADVISORS[key].source != "stored"
    return (
        f"Connected advisor {key!r} ({spec.label}): model {model_id}, endpoint "
        f"{spec.base_url}, key "
        + ("stored in an owner-only file" if api_key else
           f"read from {spec.api_key_env}" if token else "none needed")
        + f". {check}\n"
        + (f"WARNING: an advisor named {key!r} is also defined in the "
           "environment or config file, and that definition wins.\n"
           if shadowed else "")
        + f"Use it now with advisor={key!r} on ask_wisdomtooth, review_code "
        "or compare_approaches, or together with others in multi_advisor. "
        f"Default advisor: {_default_advisor()}.")


def _disconnect_advisor(name: str) -> str:
    key = str(name or "").strip().lower()
    path = _advisors_path()
    store = advisors.read_store(path, _warn)
    stored = store.get("advisors") or {}
    if key not in stored:
        where = ("It is defined in ADVISOR_ADVISORS_JSON or the config file; "
                 "remove it there." if key in _ADVISORS
                 else f"Connected: {_advisor_choices()}.")
        raise AdvisorInputError(f"no stored advisor named {name!r}. {where}")
    del stored[key]
    if store.get("default") == key:
        store.pop("default")
    if _OVERRIDES.get("advisor") == key:
        _OVERRIDES.pop("advisor")
    advisors.write_store(path, store)
    _load_advisors()
    return (f"Disconnected advisor {key!r} and deleted its stored settings and "
            f"key. Default advisor: {_default_advisor()}.")


# ---------------------------------------------------------------------------
# Several advisors at once
# ---------------------------------------------------------------------------

_advisors_mod = advisors  # `advisors` is also multi_advisor's argument name


def _split_target(item) -> tuple:
    """"gpt" -> ("gpt", ""); "gpt:gpt-6-astra" -> ("gpt", "gpt-6-astra")."""
    name, _, model = str(item or "").strip().partition(":")
    return name.strip().lower(), model.strip()


def _multi(question: str = "", context: str = "", attempts_so_far: str = "",
           advisors: Optional[list] = None,
           targeted_questions: Optional[dict] = None, model: str = "",
           effort: str = "", max_tokens: int = 0,
           context_files: Optional[list] = None,
           confirm_over_limit: bool = False,
           saved: Optional[dict] = None) -> str:
    """Consult 2-3 advisors in parallel; return one combined result.

    Everything that can be checked is checked before anything is sent: the
    names, readiness, a question for each, and every account's balance. After
    that one advisor failing costs only its own section.
    """
    limit = _advisors_mod.MAX_PER_CALL
    targeted: dict = {}
    for raw, text in (targeted_questions or {}).items():
        name, choice = _split_target(raw)
        if name in targeted:
            raise AdvisorInputError(f"{name!r} has two targeted questions; "
                                    "give each advisor one.")
        targeted[name] = (choice, str(text or "").strip())
    order: list = []
    chosen: dict = {}
    for item in advisors or []:
        name, choice = _split_target(item)
        if name in chosen:
            raise AdvisorInputError(
                f"{name!r} is listed twice; each advisor is asked once per "
                "call.")
        chosen[name] = choice
        order.append(name)
    for name, (choice, _text) in targeted.items():
        if name not in chosen:
            order.append(name)
            chosen[name] = choice
        elif choice and not chosen[name]:
            chosen[name] = choice

    if not order:
        raise AdvisorInputError(
            "name the advisors to ask: `advisors` (the same question to each) "
            "and/or `targeted_questions` (advisor name -> its own question). "
            f"Connected: {_advisor_choices()}.")
    if len(order) > limit:
        raise AdvisorInputError(
            f"multi_advisor asks at most {limit} advisors per call; got "
            f"{len(order)} ({', '.join(order)}). Pick the {limit} best suited.")
    unknown = [n for n in order if n not in _ADVISORS]
    if unknown:
        raise AdvisorInputError(
            f"not connected: {', '.join(unknown)}. Connected: "
            f"{_advisor_choices()}. The user can add an advisor with "
            "advisor_connect (e.g. provider gemini, openai or lmstudio).")
    if len(order) < 2:
        raise AdvisorInputError(
            f"multi_advisor needs 2 or {limit} advisors. For one, use "
            f"ask_wisdomtooth with advisor={order[0]!r}. Connected: "
            f"{_advisor_choices()}.")
    not_ready = [(n, _advisor_ready(n)[1]) for n in order
                 if not _advisor_ready(n)[0]]
    if not_ready:
        raise AdvisorError(
            "Nothing was sent. Not ready: "
            + "; ".join(f"{n} -- {why}" for n, why in not_ready)
            + ". Ask the ready ones, or have the user fix these first.")
    questions = {n: (targeted[n][1] if n in targeted and targeted[n][1]
                     else str(question or "").strip()) for n in order}
    missing = [n for n in order if not questions[n]]
    if missing:
        raise AdvisorInputError(
            f"no question for {', '.join(missing)}: pass `question` (shared) "
            "or a targeted question for each advisor.")

    parts = [context] if context else []
    if attempts_so_far:
        parts.append(f"<attempts_so_far>\n{attempts_so_far}\n</attempts_so_far>")
    if context_files:
        parts.append(_read_context_files(context_files))  # read once, for all
    shared = "\n\n".join(parts)

    asks = []
    for name in order:
        spec = _ADVISORS[name]
        system, _q, user_content = _prepare(questions[name], shared)
        use_model, use_effort, backend = _route(spec, chosen[name] or model,
                                                effort)
        asks.append(dict(spec=spec, model=use_model, effort=use_effort,
                         backend=backend, max_tokens=max_tokens, system=system,
                         user_content=user_content))
    _hold_if_over(asks, confirm_over_limit, "multi_advisor")

    parent = _CANCELLATION.get()
    jobs = [(name, parent.child(name) if parent is not None else None,
             contextvars.copy_context()) for name in order]

    def run(job):
        name, holder, context_copy = job

        def inner():
            if holder is not None:
                _CANCELLATION.set(holder)
            out: dict = {}
            try:
                answer = _consult(questions[name], context=shared,
                                  model=chosen[name] or model, effort=effort,
                                  max_tokens=max_tokens, kind="multi_advisor",
                                  saved=out, advisor=name,
                                  confirm_over_limit=confirm_over_limit,
                                  held_already=True)
                return name, True, answer, out.get("path")
            except Exception as exc:  # one failure must not cost the others
                return name, False, str(exc) or exc.__class__.__name__, None
        return context_copy.run(inner)

    with ThreadPoolExecutor(max_workers=len(jobs),
                            thread_name_prefix="wisdomtooth-multi") as pool:
        results = list(pool.map(run, jobs))

    if not any(good for _n, good, _t, _p in results):
        raise AdvisorError(
            "Every advisor failed, so there is nothing to compare:\n"
            + "\n".join(f"- {n}: {text}" for n, _g, text, _p in results))
    if saved is not None:
        saved["paths"] = [p for _n, _g, _t, p in results if p]

    all_targeted = all(n in targeted and targeted[n][1] for n in order)
    if all_targeted:
        mode = "a targeted question to each"
    elif targeted:
        mode = ("targeted questions for "
                + ", ".join(n for n in order if n in targeted)
                + ", the shared question for the rest")
    else:
        mode = "the same question to each"
    out = [f"MULTI-ADVISOR: {len(order)} advisors asked in parallel -- {mode}."]
    for i, (name, good, text, _path) in enumerate(results, 1):
        out.append(f"\n\n=== {i}. {name} ({_ADVISORS[name].label})"
                   + ("" if good else " -- FAILED") + " ===")
        if targeted:
            asked = questions[name]
            out.append("Question: " + (asked if len(asked) <= 300
                                       else asked[:300] + " ..."))
        out.append(text)
    failed = sum(1 for _n, good, _t, _p in results if not good)
    if targeted:
        out.append("\n\n---\n[how to use this: each advisor answered its own "
                   "targeted question -- combine the answers, each for its own "
                   "part of the work; they are not votes on one question.]")
    else:
        out.append("\n\n---\n[how to use this: where the advisors agree, "
                   "treat it as strong evidence; where they disagree, weigh "
                   "each one's reasoning against what you know instead of "
                   "taking a majority vote, and tell the user about any "
                   "disagreement that affects the decision. Do not ask the "
                   "same question again.]")
    if failed:
        out.append(f"\n[{failed} of {len(order)} advisors failed; their errors "
                   "are above. Do not retry them in a loop.]")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Runtime configuration and capability discovery
# ---------------------------------------------------------------------------

_CONFIGURABLE = ("advisor", "model", "effort", "max_tokens", "answer_budget")


def _configure(model: str = "", effort: str = "", max_tokens: int = 0,
               answer_budget: int = -1, reset: bool = False,
               advisor: str = "") -> str:
    """Apply runtime overrides and return the resulting effective settings."""
    if LOCKED:
        raise AdvisorError(
            "ADVISOR_LOCK=1 is set, so the advisor's model, effort and token "
            "budget are pinned by the operator and cannot be changed from a "
            "tool call. Ask the user to change the MCP server config (or the "
            "advisor config file) and restart the server entry.")
    if reset:
        _OVERRIDES.clear()
    if advisor:
        name = str(advisor).strip().lower()
        if name not in _ADVISORS:
            raise AdvisorInputError(
                f"no advisor named {advisor!r}. Connected: "
                f"{_advisor_choices()}. Add one with advisor_connect.")
        _OVERRIDES["advisor"] = name
    target = _default_advisor()
    if model and target != advisors.CLAUDE:
        # Another provider's model IDs follow its own naming.
        _OVERRIDES.setdefault("advisor_models", {})[target] = str(model)
    elif model:
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
        f"default advisor: {target}\n"
        f"effective model: {_advisor_model(_ADVISORS[target])}\n"
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
    others = [spec for name, spec in _ADVISORS.items()
              if spec.kind != "claude"]
    if others:
        lines.append("")
        lines.append("OTHER ADVISORS (pass advisor=<name>; the tiers above "
                     "are Claude's, each advisor has its own):")
        for spec in others:
            tiers = ", ".join(f"{t} -> {m}" for t, m in sorted(spec.tiers.items()))
            lines.append(f"  {spec.name} ({spec.label}): default "
                         f"{_advisor_model(spec)}"
                         + (f"; tiers {tiers}" if tiers else "")
                         + "; effort " + ("/".join(spec.efforts)
                                          if spec.efforts else "not sent"))
    if LOCKED:
        lines.append("NOTE: ADVISOR_LOCK=1 -- per-call model/effort/max_tokens "
                     "arguments are ignored.")
    return "\n".join(lines)


async def _consult_with_heartbeat(ctx: Optional[Context], *args, **kwargs):
    """Run `_consult` off the event loop, reporting progress while it works."""
    name = str(kwargs.get("advisor") or "").strip().lower() or _default_advisor()
    who = "Claude" if name == advisors.CLAUDE else name
    return await _with_heartbeat(ctx, _consult, *args,
                                 message=f"{who} is still working", **kwargs)


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
                          follow_up_of: str = "", advisor: str = "",
                          confirm_over_limit: bool = False,
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
        advisor: Which connected advisor to ask -- "claude", or a name
            advisor_status lists (e.g. "gemini", "chatgpt", "local"). Empty
            asks the default advisor (Claude unless the user chose another).
        confirm_over_limit: Leave false. Set true ONLY after the user agreed
            to go ahead with a request the server HELD because it would cost
            more than the account has left.
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
        advisor=advisor,
        confirm_over_limit=confirm_over_limit,
    )
    return _result_blocks(answer, saved)


@tool(title="Review code with an advisor", annotations=CONSULT_ANNOTATIONS)
async def review_code(code: str, concern: str = "general quality",
                      model: str = "", effort: str = "",
                      max_tokens: int = 0,
                      context_files: Optional[list[str]] = None,
                      advisor: str = "", confirm_over_limit: bool = False,
                      ctx: Optional[Context] = None):
    """ESCALATION: Have an advisor (Claude by default) review code you are unsure about.

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
        advisor: Which connected advisor to ask -- "claude", or a name
            advisor_status lists (e.g. "gemini", "chatgpt", "local"). Empty
            asks the default advisor (Claude unless the user chose another).
        confirm_over_limit: Leave false. Set true ONLY after the user agreed
            to go ahead with a request the server HELD because it would cost
            more than the account has left.
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
        advisor=advisor,
        confirm_over_limit=confirm_over_limit,
    )
    return _result_blocks(answer, saved)


@tool(title="Compare approaches with an advisor",
      annotations=CONSULT_ANNOTATIONS)
async def compare_approaches(problem: str, options: str, criteria: str = "",
                             model: str = "", effort: str = "",
                             max_tokens: int = 0, advisor: str = "",
                             confirm_over_limit: bool = False,
                             ctx: Optional[Context] = None):
    """ESCALATION: Have ONE advisor (Claude by default) weigh approaches you can't decide between.

    WHEN TO USE: you have identified 2+ viable approaches to a non-trivial
    problem (architecture, library choice, migration strategy) and the
    tradeoffs are genuinely unclear after your own analysis. Not for
    decisions with an obvious answer. One advisor picks between options; to
    put a question to several advisors, use multi_advisor.

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
        advisor: Which connected advisor to ask -- "claude", or a name
            advisor_status lists (e.g. "gemini", "chatgpt", "local"). Empty
            asks the default advisor (Claude unless the user chose another).
        confirm_over_limit: Leave false. Set true ONLY after the user agreed
            to go ahead with a request the server HELD because it would cost
            more than the account has left.
    """
    question = (
        f"Problem: {problem}\n\nCandidate approaches:\n{options}\n"
        + (f"\nDecision criteria: {criteria}\n" if criteria else "")
        + "\nCompare the tradeoffs briefly, then commit to a single recommendation."
    )
    saved: dict = {}
    answer = await _consult_with_heartbeat(ctx, question, model=model,
                                           effort=effort, max_tokens=max_tokens,
                                           kind="compare_approaches", saved=saved,
                                           advisor=advisor,
                                           confirm_over_limit=confirm_over_limit)
    return _result_blocks(answer, saved)


@tool(title="Ask several advisors at once", annotations=CONSULT_ANNOTATIONS)
async def multi_advisor(question: str = "", context: str = "",
                        attempts_so_far: str = "",
                        advisors: Optional[list[str]] = None,
                        targeted_questions: Optional[dict[str, str]] = None,
                        model: str = "", effort: str = "",
                        context_files: Optional[list[str]] = None,
                        confirm_over_limit: bool = False,
                        ctx: Optional[Context] = None):
    """ESCALATION: Consult 2 or 3 advisors in parallel, in one call (at most 3).

    Only when the user has connected more than one advisor -- advisor_status
    lists them (Claude, plus e.g. Gemini, ChatGPT or a local model). Two uses:

    1. COMPARE: the same `question` to each advisor in `advisors`, for a
       second (and third) opinion on a hard or high-stakes problem.
    2. TARGET: a different question to each, in `targeted_questions`, to play
       to each model's strengths -- e.g. {"claude": "<technical question>",
       "gemini": "<UI/UX question>", "chatgpt": "<review this module>"}.
    They combine: a targeted question overrides the shared one for that
    advisor. Same escalation rules as ask_wisdomtooth; it costs one consult
    per advisor, on each advisor's own account.

    Args:
        question: The shared question, for every advisor without a targeted
            one.
        context: Background every advisor sees -- code, errors, versions.
        attempts_so_far: What you already tried and why it failed.
        advisors: Names to ask the shared question, e.g. ["claude", "gemini"].
            "name:model" picks a model for one advisor, e.g.
            "chatgpt:gpt-6-astra".
        targeted_questions: Advisor name -> its own question.
        model: A tier ("fast", "balanced", "deep") applied to every advisor
            in its own family. Use "name:model" for a specific model ID.
        effort: Reasoning effort for every advisor, clamped to what each
            accepts.
        context_files: Files for the server to read once and attach for all.
        confirm_over_limit: Leave false. Set true ONLY after the user agreed
            to go ahead with a request the server HELD because an account
            would go over what it has left.
    """
    saved: dict = {}
    text = await _with_heartbeat(
        ctx, _multi, question=question, context=context,
        attempts_so_far=attempts_so_far, advisors=advisors,
        targeted_questions=targeted_questions, model=model, effort=effort,
        context_files=context_files, confirm_over_limit=confirm_over_limit,
        saved=saved, message="The advisors are still working")
    blocks = transcripts.result_blocks(text, None)
    for path in saved.get("paths", []):
        blocks += transcripts.result_blocks("", path)[1:]
    return blocks


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


@tool(title="Connect another advisor", annotations=CREDENTIAL_ANNOTATIONS)
async def advisor_connect(name: str, provider: str, api_key: str = "",
                          model: str = "", base_url: str = "",
                          allowance_tokens: int = 0,
                          allowance_window: str = "", notes: str = "",
                          make_default: bool = False) -> str:
    """Connect an advisor besides Claude: ChatGPT, Gemini, OpenRouter or a local model.

    FREE -- makes no model call; it only lists the endpoint's models to check
    the key and the address. Call it when the user asks to add an advisor.
    The advisor is saved (its key in an owner-only file) and usable at once,
    with no config edit or restart. Claude stays the default unless
    make_default is true.

    The key is a credential: do not repeat it back, log it, or put it
    anywhere but this argument.

    Args:
        name: What to call it, e.g. "chatgpt", "gemini", "local".
        provider: "openai" (ChatGPT), "gemini", "openrouter", "lmstudio",
            "ollama", or "openai-compatible" (any other endpoint).
        api_key: The provider's API key. Not needed for local servers, or
            when the key is already in the env (OPENAI_API_KEY,
            GEMINI_API_KEY, OPENROUTER_API_KEY).
        model: A model ID (or tier) to use by default; each hosted provider
            has a sensible default.
        base_url: Only for a non-default endpoint (required for
            "openai-compatible").
        allowance_tokens: Optional token allowance the user wants tracked for
            this account; a request that would exceed it is held for their
            confirmation.
        allowance_window: The allowance's window: "hour", "5h", "day",
            "week" or "month".
        notes: What this advisor is good at, shown in advisor_status.
        make_default: Send plain consults to this advisor instead of Claude.
    """
    return await asyncio.to_thread(
        _connect_advisor, name, provider, api_key=api_key, model=model,
        base_url=base_url, allowance_tokens=allowance_tokens,
        allowance_window=allowance_window, notes=notes,
        make_default=make_default)


@tool(title="Disconnect an advisor", annotations=CREDENTIAL_ANNOTATIONS)
async def advisor_disconnect(name: str) -> str:
    """Forget an advisor added with advisor_connect, and its stored key.

    FREE -- makes no model call. Claude cannot be disconnected here.

    Args:
        name: The advisor's name, as advisor_status lists it.
    """
    return await asyncio.to_thread(_disconnect_advisor, name)


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
                      answer_budget: int = -1, reset: bool = False,
                      advisor: str = "") -> str:
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
        advisor: Make this connected advisor the default for plain consults
            (e.g. "gemini"; "claude" switches back). `model` then applies to
            it.
    """
    return _configure(model=model, effort=effort, max_tokens=max_tokens,
                      answer_budget=answer_budget, reset=reset,
                      advisor=advisor)


@tool(title="Advisor usage and spend", annotations=LOCAL_ANNOTATIONS)
def advisor_usage(days: int = 7) -> str:
    """Report consults, tokens and estimated cost over time, per advisor, and what each account has left.

    FREE — makes no model call; reads the local usage ledger and the last
    balance readings. Use when the user asks how much the advisors have used
    or have left, or before a burst of consults when their limits are tight.
    Cost is an estimate at API rates; the Claude plan's own meter appears
    under BALANCES once a subscription consult has reported it.

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
        lines.append(prompts.NO_CREDENTIALS)
    elif backend == "api":
        lines.append("")
        lines.append("This is billing pay-per-token API credits. If the user has "
                     "a Claude Pro/Max subscription, call `advisor_login` to "
                     "switch to it at no per-token cost.")
    return "\n".join(lines)


@tool(title="Advisor status", annotations=LOCAL_ANNOTATIONS)
def advisor_status() -> str:
    """Report the connected advisors, what each account has left, the billing target and defaults.

    FREE — makes no model call. Use when the user asks which account the
    advisor is using or how much is left, to see which advisors are ready
    (and what each is good for) before using multi_advisor, or to
    sanity-check configuration.
    """
    return _status_report()


def _advisor_status_lines() -> list:
    ready = []
    lines = [f"advisors ({len(_ADVISORS)} connected; multi_advisor asks up to "
             f"{advisors.MAX_PER_CALL} at once):"]
    for name, spec in _ADVISORS.items():
        ok, why = _advisor_ready(name)
        if ok:
            ready.append(name)
        if spec.kind == "claude":
            desc = f"Claude via the {_active_backend()} backend"
        else:
            desc = (f"{spec.label}, model {_advisor_model(spec)}, "
                    f"{spec.base_url}, bills {spec.billing or 'its endpoint'}")
        lines.append(f"  {name}: {desc} -- {'ready' if ok else 'NOT READY: ' + why}"
                     + (f" -- {spec.notes}" if spec.notes else ""))
    if len(ready) > 1:
        lines.append(f"  tip: {len(ready)} advisors are ready -- multi_advisor "
                     "can ask them the same question, or each its own.")
    balances = _balance_lines()
    lines.append("balances:" + ("" if balances else " none reported yet (the "
                                "Claude plan meter appears after a "
                                "subscription consult)"))
    lines += ["  " + line for line in balances]
    return lines


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
        f"default advisor: {_default_advisor()}",
        *_advisor_status_lines(),
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
        "tool surface: " + ("minimal (ask_wisdomtooth, advisor_status)"
                            if MINIMAL_TOOLS
                            else f"full ({len(_TOOL_NAMES)} tools)"),
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
