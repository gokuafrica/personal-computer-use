"""Safety gate for destructive actions, per ARCHITECTURE.md."""

from __future__ import annotations

import re

MAX_STEPS = 25

DESTRUCTIVE_RE = re.compile(
    r"delete|remove|format|pay|purchase|checkout|send|submit|transfer|password|confirm",
    re.IGNORECASE,
)

DANGEROUS_KEYS = {"alt+f4", "ctrl+alt+delete", "ctrl+alt+del"}
# "win" and win+<key> combos are intentionally allowed: opening the Start
# menu / search / run dialog is reversible navigation, and gating it hangs
# routine tasks behind a confirmation card. Truly destructive combos stay in
# DANGEROUS_KEYS above.


def normalize_key(key: str) -> str:
    """Normalize a combo string: lowercase, '+'-joined parts, no spaces."""
    return "+".join(part.strip().lower() for part in key.split("+") if part.strip())


def check(action_kind: str, detail: str, in_error: bool = False) -> str:
    """Return ``"ok"`` or ``"confirm"`` for a normalized action.

    ``detail`` should carry the human-affecting payload: the text to type or the
    key combo to press. ``in_error`` forces confirmation for any action after an
    error state per the architecture doc.
    """
    if in_error:
        return "confirm"
    kind = action_kind.lower()
    if kind in ("type", "key"):
        if kind == "key":
            normalized = normalize_key(detail)
            if normalized in DANGEROUS_KEYS:
                return "confirm"
        if DESTRUCTIVE_RE.search(detail or ""):
            return "confirm"
    return "ok"
