"""Outbound-content safety: redact secrets, cap size, optionally scrub words,
and decide which local files the server may read for a caller."""

import json
import os
import re
import sys
from typing import Optional

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


def redact(text: str) -> str:
    for pattern, repl in SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25):]
    dropped = len(text) - len(head) - len(tail)
    return (head + f"\n\n[...advisor-server truncated {dropped} chars; pass "
            "focused excerpts instead of whole files...]\n\n" + tail)


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


def load_nsfw_map(extra_path: Optional[str] = None) -> dict:
    mapping = dict(NSFW_REPLACEMENTS)
    if extra_path and os.path.isfile(extra_path):
        try:
            with open(extra_path, encoding="utf-8") as fh:
                mapping.update({str(k).lower(): str(v)
                                for k, v in json.load(fh).items()})
        except Exception as exc:  # a bad user file must not kill the server
            print(f"[wisdomtooth] ignoring ADVISOR_NSFW_EXTRA_JSON: {exc}",
                  file=sys.stderr)
    return mapping


def compile_words(mapping: dict):
    if not mapping:
        return None
    return re.compile(r"\b(" + "|".join(sorted(map(re.escape, mapping), key=len,
                                               reverse=True)) + r")\b",
                      re.IGNORECASE)


def match_case(replacement: str, original: str) -> str:
    if original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def scrub_words(text: str, regex, mapping: dict) -> str:
    return regex.sub(lambda m: match_case(mapping[m.group(0).lower()],
                                          m.group(0)), text)


# ---------------------------------------------------------------------------
# Files the server reads itself (`context_files`)
# ---------------------------------------------------------------------------
# Reading stays inside allowed folders, and credential-shaped files are refused
# before they are opened. The server's own state directory (stored token,
# transcripts) is refused by location in the server, not by name here.

MAX_CONTEXT_FILES = 20
DENY_DIRS = {".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker", ".git"}
DENY_FILE = re.compile(
    r"^(\.env(\..+)?|\.netrc|\.npmrc|\.pypirc|\.git-credentials"
    r"|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|credentials(\.json)?"
    r"|.+\.(pem|key|p12|pfx|kdbx|jks|keystore))$", re.IGNORECASE)
SAFE_SUFFIXES = (".example", ".sample", ".template")


def too_broad(path: str) -> bool:
    """The home folder or a drive root: too much to expose as a file root."""
    real = os.path.normcase(os.path.realpath(path))
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    return real == home or os.path.dirname(real) == real


def inside(path: str, root: str) -> bool:
    try:
        return (os.path.commonpath([os.path.normcase(path),
                                    os.path.normcase(root)])
                == os.path.normcase(root))
    except ValueError:  # different drives
        return False


def refused(parts: list) -> bool:
    """Whether a path, split into its parts, looks like a credential."""
    if any(p.lower() in DENY_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    return (not name.lower().endswith(SAFE_SUFFIXES)
            and DENY_FILE.match(name) is not None)
