"""Advisors: the named models a consult can go to.

`claude` is always present and always the default unless the user picks
another. It runs through the existing Claude backends (subscription CLI or
API), so everything about billing Claude stays where it was.

Every other advisor speaks the OpenAI chat-completions protocol, which covers
ChatGPT (the OpenAI API), Gemini (Google's OpenAI-compatible endpoint),
OpenRouter, and local servers such as LM Studio, Ollama, vLLM and llama.cpp.
A provider preset fills in the endpoint, the key variable, tiers and what the
provider accepts, so the user usually writes only a name and a provider:

    {"advisors": {"chatgpt": {"provider": "openai"},
                  "gemini":  {"provider": "gemini"},
                  "local":   {"provider": "lmstudio",
                              "model": "google/gemma-4-12b"}}}

Advisors come from three places, first one wins per name: the
ADVISOR_ADVISORS_JSON environment variable, the config file's `advisors` key,
and the store `advisor_connect` writes. A broken entry is skipped with a
warning; it must never stop the server, or Claude with it.
"""

import json
import os
import re
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping, Optional

from . import cli_advisors, storage

CLAUDE = "claude"
# The most advisors one multi_advisor call may consult.
MAX_PER_CALL = 3

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Allowance windows a user can declare, in seconds. "month" is a rolling 30
# days, inside the ledger's 35-day retention.
WINDOWS = {"hour": 3600, "5h": 5 * 3600, "day": 86400, "week": 7 * 86400,
           "month": 30 * 86400}

_ALL_EFFORT = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Model IDs as the providers documented them in September 2026. They drift, so
# every one can be overridden per advisor with `model` or `tiers`.
PROVIDERS = {
    "openai": dict(
        label="ChatGPT (OpenAI API)",
        base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY",
        tiers={"fast": "gpt-5.6-luna", "balanced": "gpt-5.6-terra",
               "deep": "gpt-6-astra"},
        efforts=_ALL_EFFORT,
        # Reasoning models reject max_tokens; this one covers reasoning too.
        max_tokens_param="max_completion_tokens", send_max_tokens=True,
        needs_key=True, billing="OpenAI API account (pay-per-token)"),
    "gemini": dict(
        label="Gemini (Google AI Studio API)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
        tiers={"fast": "gemini-3.5-flash-lite", "balanced": "gemini-3.8-flash",
               "deep": "gemini-3.1-pro-preview"},
        efforts=("minimal", "low", "medium", "high"),
        send_max_tokens=True, needs_key=True,
        billing="Google Gemini API account"),
    "openrouter": dict(
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        efforts=("low", "medium", "high"), send_max_tokens=True,
        needs_key=True, balance="openrouter",
        billing="OpenRouter credits"),
    "lmstudio": dict(
        label="LM Studio (local)", base_url="http://localhost:1234/v1",
        local=True, billing="local model (no per-token cost)"),
    "ollama": dict(
        label="Ollama (local)", base_url="http://localhost:11434/v1",
        local=True, billing="local model (no per-token cost)"),
    "openai-compatible": dict(
        label="OpenAI-compatible endpoint",
        billing="the endpoint's own account"),
}


# Every provider name an advisor entry may use: HTTP endpoints above, and the
# vendor CLIs in cli_advisors.
ALL_PROVIDERS = tuple(PROVIDERS) + tuple(cli_advisors.PRESETS)


@dataclass(frozen=True)
class AdvisorSpec:
    name: str
    kind: str                      # "claude", "openai" or "cli"
    provider: str = "anthropic"
    label: str = "Claude"
    base_url: str = ""
    api_key: str = ""              # stored or configured; never shown
    api_key_env: str = ""
    model: str = ""                # a tier name or a model ID
    tiers: Mapping = field(default_factory=dict)
    efforts: tuple = ()
    max_tokens_param: str = "max_tokens"
    send_max_tokens: bool = False
    needs_key: bool = False
    local: bool = False
    # USD per million tokens (input, output), for estimates and the ledger.
    prices: Optional[tuple] = None
    balance: Optional[str] = None  # "openrouter": the provider reports credit
    billing: str = ""
    allowance_tokens: int = 0
    allowance_window: str = ""
    notes: str = ""
    source: str = ""               # "env", "config" or "stored"
    command: str = ""              # a CLI advisor's executable
    args: tuple = ()               # extra arguments the operator added

    @property
    def allowance_seconds(self) -> int:
        return WINDOWS.get(self.allowance_window, 0)


def claude_spec(**overrides) -> AdvisorSpec:
    return AdvisorSpec(name=CLAUDE, kind="claude", billing="see the backend",
                       **overrides)


def _as_tuple(value) -> tuple:
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\s]+", value) if v]
    return tuple(str(v).lower() for v in (value or ()))


def _prices(value, warn, where) -> Optional[tuple]:
    if value is None:
        return None
    try:
        rate_in, rate_out = (float(v) for v in value)
        return (rate_in, rate_out)
    except (TypeError, ValueError):
        warn(f"{where}: `prices` must be [input, output] USD per million "
             "tokens; ignoring it")
        return None


def _allowance(raw: Mapping, warn, where) -> dict:
    out = {}
    if raw.get("allowance_tokens") is not None:
        try:
            out["allowance_tokens"] = max(0, int(float(raw["allowance_tokens"])))
        except (TypeError, ValueError):
            warn(f"{where}: allowance_tokens must be a number; ignoring it")
    window = str(raw.get("allowance_window") or "").lower()
    if window:
        if window in WINDOWS:
            out["allowance_window"] = window
        else:
            warn(f"{where}: allowance_window {window!r} is not one of "
                 f"{', '.join(WINDOWS)}; ignoring the allowance")
            out["allowance_tokens"] = 0
    elif out.get("allowance_tokens"):
        out["allowance_window"] = "day"
    if raw.get("notes"):
        out["notes"] = str(raw["notes"])[:300]
    return out


def build(name: str, raw, warn: Callable[[str], None],
          source: str = "") -> Optional[AdvisorSpec]:
    """One advisor from its config entry, or None (with a warning)."""
    where = f"advisor {name!r}"
    if not isinstance(raw, Mapping):
        warn(f"{where}: expected an object; skipping it")
        return None
    key = str(name).strip().lower()
    if not NAME.match(key):
        warn(f"{where}: names are 1-32 lowercase letters, digits, - or _; "
             "skipping it")
        return None
    if key == CLAUDE:
        # Claude's backends are configured by ADVISOR_BACKEND; this entry may
        # only describe the account.
        return claude_spec(source=source, **_allowance(raw, warn, where))

    provider = str(raw.get("provider") or "").lower()
    if provider in cli_advisors.PRESETS:
        return _build_cli(key, provider, raw, warn, where, source)
    if provider not in PROVIDERS:
        warn(f"{where}: unknown provider {provider or '(none)'!r}; use one of "
             f"{', '.join(ALL_PROVIDERS)}. Skipping it")
        return None
    preset = dict(PROVIDERS[provider])
    tiers = dict(preset.pop("tiers", {}))
    if isinstance(raw.get("tiers"), Mapping):
        tiers.update({str(k).lower(): str(v) for k, v in raw["tiers"].items()})
    model = str(raw.get("model") or ("balanced" if "balanced" in tiers else ""))
    base_url = str(raw.get("base_url") or preset.get("base_url") or "")
    base_url = base_url.strip().rstrip("/")
    if not base_url or not model:
        warn(f"{where}: needs a `base_url` and a `model` for provider "
             f"{provider!r}; skipping it")
        return None

    spec = AdvisorSpec(
        name=key, kind="openai", provider=provider,
        label=str(raw.get("label") or preset.get("label") or provider),
        base_url=base_url,
        api_key=str(raw.get("api_key") or ""),
        api_key_env=str(raw.get("api_key_env") or preset.get("api_key_env")
                        or ""),
        model=model, tiers=tiers,
        efforts=_as_tuple(raw["efforts"]) if "efforts" in raw
        else tuple(preset.get("efforts", ())),
        max_tokens_param=str(raw.get("max_tokens_param")
                             or preset.get("max_tokens_param", "max_tokens")),
        send_max_tokens=bool(raw.get("send_max_tokens",
                                     preset.get("send_max_tokens", False))),
        needs_key=bool(raw.get("needs_key", preset.get("needs_key", False))),
        local=bool(preset.get("local", False)),
        prices=_prices(raw.get("prices"), warn, where)
        or ((0.0, 0.0) if preset.get("local") else None),
        balance=raw.get("balance", preset.get("balance")),
        billing=str(raw.get("billing") or preset.get("billing") or ""),
        source=source,
    )
    return replace(spec, **_allowance(raw, warn, where))


def _build_cli(key, provider, raw, warn, where, source) -> Optional[AdvisorSpec]:
    """An advisor that runs a vendor CLI the user installed."""
    preset = cli_advisors.PRESETS[provider]
    command = str(raw.get("command") or preset.binary).strip()
    if not command:
        warn(f"{where}: provider 'cli' needs `command`, the executable to run; "
             "skipping it")
        return None
    args = raw.get("args") or ()
    if isinstance(args, str) or not isinstance(args, (list, tuple)):
        warn(f"{where}: `args` must be a list of strings; ignoring it")
        args = ()
    tiers = dict(preset.tiers)
    if isinstance(raw.get("tiers"), Mapping):
        tiers.update({str(k).lower(): str(v) for k, v in raw["tiers"].items()})
    spec = AdvisorSpec(
        name=key, kind="cli", provider=provider,
        label=str(raw.get("label") or preset.label),
        command=command, args=tuple(str(a) for a in args),
        model=str(raw.get("model") or ""), tiers=tiers,
        efforts=tuple(preset.efforts),
        prices=_prices(raw.get("prices"), warn, where),
        billing=str(raw.get("billing") or preset.billing),
        source=source)
    return replace(spec, **_allowance(raw, warn, where))


def parse_json(text, what: str, warn) -> dict:
    if not text:
        return {}
    if isinstance(text, Mapping):
        return dict(text)
    try:
        data = json.loads(text)
    except ValueError as exc:
        warn(f"ignoring {what}: not valid JSON ({exc})")
        return {}
    if not isinstance(data, dict):
        warn(f"ignoring {what}: expected an object of advisor entries")
        return {}
    return data


def read_store(path: str, warn) -> dict:
    """The advisors `advisor_connect` saved: {"advisors": {...},
    "default": name}."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # a broken store must never stop the server
        warn(f"ignoring {path}: {exc}")
        return {}


def write_store(path: str, data: dict) -> None:
    """Owner-only from the first byte: the store holds API keys."""
    storage.write_atomic(path, json.dumps(data, indent=2), private=True)


def update_store(path: str, change: Callable[[dict], None], warn) -> dict:
    """Read the store, apply `change`, and write it back, under the store's
    lock so a concurrent change in another server process is not lost. An
    exception from `change` leaves the file untouched."""
    with storage.locked(path):
        store = read_store(path, warn)
        change(store)
        write_store(path, store)
    return store


def load(env_json, file_entries, stored_entries, warn) -> dict:
    """name -> AdvisorSpec, claude first. Earlier sources win per name."""
    out = {CLAUDE: claude_spec()}
    seen = set()
    for source, entries in (("env", parse_json(env_json,
                                               "ADVISOR_ADVISORS_JSON", warn)),
                            ("config", parse_json(file_entries,
                                                  "the config file's advisors",
                                                  warn)),
                            ("stored", stored_entries or {})):
        for name, raw in entries.items():
            key = str(name).strip().lower()
            if key in seen:
                continue
            spec = build(name, raw, warn, source)
            if spec is not None:
                seen.add(key)
                out[spec.name] = spec
    return out


TIER_NAMES = ("fast", "balanced", "deep")


def model_for(spec: AdvisorSpec, model: str) -> str:
    """A tier name or model ID for this advisor, resolved to a model ID.

    A tier the advisor does not define -- `fast` on a local server with one
    model loaded -- means its default model, never a model named "fast".
    """
    choice = str(model or spec.model or "")
    if choice.lower() in spec.tiers:
        return str(spec.tiers[choice.lower()])
    if choice.lower() in TIER_NAMES and model:
        return model_for(spec, "")
    return choice


def effort_for(spec: AdvisorSpec, effort: Optional[str]) -> Optional[str]:
    """The effort to send, clamped to what the provider accepts, or None."""
    if not effort or not spec.efforts:
        return None
    effort = effort.lower()
    if effort in spec.efforts:
        return effort
    order = _ALL_EFFORT
    rank = order.index(effort) if effort in order else len(order) - 1
    lower = [e for e in spec.efforts if e in order and order.index(e) <= rank]
    return lower[-1] if lower else spec.efforts[0]


def key_for(spec: AdvisorSpec, environ: Mapping) -> str:
    return spec.api_key or (environ.get(spec.api_key_env, "")
                            if spec.api_key_env else "")
