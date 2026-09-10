"""Consult transcripts: one Markdown file per consult, and the tool result
that links to it.

The advisor's answer reaches the user only as a tool result inside another
agent's chat -- somewhere this server does not control and cannot re-open.
So each consult is also written to a Markdown file, and that path travels back
two ways: inside the answer footer, which every client renders because it is
only text, and as a `resource_link` content block for the clients that turn one
into something clickable.
"""

import os
import pathlib
import re
import sys
import time
from typing import Optional

from mcp.types import Annotations, ResourceLink, TextContent

SUFFIX = ".md"
SENT_HEADING = "## Sent to Claude\n\n"
ANSWER_HEADING = "\n## Claude's answer\n\n"
SUPPORT_MARK = "\n---\n\n*Wisdomtooth is free and open source."
NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def slug(text: str, limit: int = 48) -> str:
    """A filename-safe stub of the question, so the directory can be skimmed.

    Whitelist, not blacklist: this text comes from the caller and becomes part
    of a path, so everything outside [a-z0-9-] is dropped rather than escaped.
    Nothing that could act as a separator, a traversal, or a Windows reserved
    character survives, and the timestamp prefix keeps the result from ever
    being a bare device name like `con`.
    """
    stub = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return stub[:limit].strip("-") or "consult"


def prune(directory: str, keep: int) -> None:
    """Keep the newest `keep` transcripts (0 keeps all). Never fatal."""
    if not keep:
        return
    try:
        paths = [os.path.join(directory, name) for name in os.listdir(directory)
                 if name.endswith(SUFFIX)]
        if len(paths) <= keep:
            return
        for stale in sorted(paths, key=os.path.getmtime, reverse=True)[keep:]:
            os.remove(stale)
    except OSError as exc:  # a racing or crowded directory is not an error
        print("[wisdomtooth] could not prune " + directory + ": " + str(exc),
              file=sys.stderr)


def render(kind: str, topic: str, sent: str, answer: str, footer: str,
           version: str, support: str) -> str:
    stripped = topic.strip()
    heading = stripped.splitlines()[0][:120] if stripped else kind
    # The footer arrives as a rule plus the billing and usage lines; each is
    # rendered as its own line of inline code.
    lines = [line.strip() for line in footer.splitlines() if line.strip("- ")]
    return (
        "# " + kind + ": " + heading + "\n\n"
        "*" + time.strftime("%Y-%m-%d %H:%M:%S") + " - wisdomtooth "
        + version + "*\n\n"
        + "".join("`" + line + "`  \n" for line in lines) + "\n"
        + SENT_HEADING + sent + "\n"
        + ANSWER_HEADING + answer + "\n"
        + (SUPPORT_MARK + " If it saved you time, you can support it: "
           + support + "*\n" if support else ""))


def save(directory: str, kind: str, topic: str, body: str) -> Optional[str]:
    """Write one transcript; return its path, or None if nothing was written.

    Never raises. A read-only home or a full disk costs the user a transcript,
    which is a convenience -- it must not cost them the answer they just paid
    for.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        os.makedirs(directory, exist_ok=True)
        # Two consults can land in the same second; "x" mode makes the loser of
        # that race take the next name rather than overwrite the winner.
        for attempt in range(1, 50):
            tail = "" if attempt == 1 else "-" + str(attempt)
            path = os.path.join(directory, stamp + "-" + slug(kind, 24) + "-"
                                + slug(topic) + tail + SUFFIX)
            try:
                with open(path, "x", encoding="utf-8") as fh:
                    fh.write(body)
                return path
            except FileExistsError:
                continue
        return None  # pragma: no cover - 49 collisions inside one second
    except OSError as exc:
        print("[wisdomtooth] could not save the consult transcript to "
              + directory + ": " + str(exc), file=sys.stderr)
        return None


def split(text: str):
    """(what was sent, the answer) from a transcript, or None if it is not
    one."""
    head, found, answer = text.rpartition(ANSWER_HEADING)
    if not found:
        return None
    sent = (head.partition(SENT_HEADING)[2] or head).strip()
    return sent, answer.split(SUPPORT_MARK, 1)[0].strip()


def result_blocks(answer: str, path: Optional[str]) -> list:
    """The tool result: the answer as text, plus a link to its transcript.

    Two blocks rather than one. The text is what the calling model reads and
    what every client renders; the `resource_link` offers the same file to the
    client as something it can put in front of the user directly, marked
    `audience=["user"]` because that is exactly who it is for. A client that
    ignores resource links loses nothing -- the path is in the footer too.
    """
    blocks = [TextContent(type="text", text=answer)]
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
