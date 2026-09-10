"""The provider seam.

A backend is a name the user can put in ADVISOR_BACKEND, the account it bills,
a readiness check, one `consult` function, and a few declarations about what
it honours. The consult path, the answer footer, `advisor_status` and
`advisor_models` are all built from those declarations, so a new provider
(another CLI on the user's own plan, say) registers once and gets the same
treatment -- the ledger, caps and repeat guard included, as long as it reports
its tokens through the server's `_note_usage`.
"""

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Backend:
    name: str
    provider: str
    # The account it bills, in full, for advisor_status.
    billing: str
    # (system, user_content, model, effort, max_tokens) -> the answer text.
    consult: Callable[[str, str, str, Optional[str], int], str]
    available: Callable[[], bool]
    # The account in the footer's "billed to ..." -- short, because it is
    # repeated under every answer. Defaults to `billing`.
    billed_to: str = ""
    # Whether a per-call max_tokens reaches the model.
    honours_max_tokens: bool = False
    # How the footer names the model, e.g. "claude-code/sonnet".
    label: Optional[Callable[[str], str]] = None
    # The backend an "auto" setup may move to when this one's quota is
    # exhausted, if the operator allows it.
    fallback: Optional[str] = None
    # What advisor_status says when `available()` is false.
    unavailable: str = "not ready"

    def label_for(self, model: str) -> str:
        return self.label(model) if self.label else f"{self.name}/{model}"

    def footer(self, model: str, effort: Optional[str], requested_tokens: int,
               effective_tokens: int, locked: bool) -> str:
        parts = ["advisor: " + self.label_for(model),
                 "billed to " + (self.billed_to or self.billing)]
        if effort:
            parts.append("effort=" + effort)
        if self.honours_max_tokens:
            parts.append(f"max_tokens={effective_tokens}")
        elif requested_tokens and not locked:
            # Say so, rather than let the caller believe a cap was applied.
            parts.append("max_tokens ignored (not settable on this backend)")
        return "\n\n---\n[" + " · ".join(parts) + "]"
