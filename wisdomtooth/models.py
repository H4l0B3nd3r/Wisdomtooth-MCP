"""Model tiers, per-family request capabilities, and the API request body."""

from typing import Optional

# Tier aliases let the calling agent pick by intent instead of model strings.
# Remap or extend them with ADVISOR_TIERS_JSON / the config file's "tiers" key,
# e.g. {"deep": "claude-fable-5-1", "cheapest": "claude-haiku-4-5"}.
DEFAULT_TIERS = {
    "fast": "claude-haiku-4-5",     # cheap sanity checks
    "balanced": "claude-sonnet-5",  # everyday escalations
    "deep": "claude-opus-5",        # architecture and stubborn bugs
}

# Claude Code CLI model aliases for the claude-code backend.
CLAUDE_CODE_ALIASES = {"fast": "haiku", "balanced": "sonnet", "deep": "opus"}

VALID_EFFORT = ("low", "medium", "high", "xhigh", "max")

# 128k is the current output ceiling on the Opus/Sonnet/Fable families; asking
# for more is a 400, not a longer answer.
MAX_TOKENS_CEILING = 128000

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


def caps(model: str):
    for prefix in _CAP_PREFIXES:
        if model.startswith(prefix):
            return MODEL_CAPS[prefix]
    # A model released after this table was written: send nothing optional, so
    # an unknown ID degrades to a plain request instead of a 400.
    return (None, ())


def supports_fallbacks(model: str) -> bool:
    return model.startswith(FALLBACK_CAPABLE_PREFIXES)


def build_kwargs(model: str, effort: Optional[str], max_tokens: int) -> dict:
    """The Messages API request body for one consult, `max_tokens` resolved."""
    thinking, allowed_effort = caps(model)
    kwargs: dict = {"model": model, "max_tokens": max_tokens}

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
