"""Safety gate for destructive actions, per ARCHITECTURE.md."""

from __future__ import annotations

import re

MAX_STEPS = 25

# Full vocabulary for typed text: matches words typed INTO something (prompt
# boxes, forms, documents) where the words themselves could trigger a
# destructive confirmation.
DESTRUCTIVE_RE = re.compile(
    r"delete|remove|format|pay|purchase|checkout|send|submit|transfer|password|confirm",
    re.IGNORECASE,
)

# Narrower vocabulary for CLICKS on UI elements. Clicking a control named
# "Format" in Word's dialogs is routine formatting, so words that are common
# in benign UI (format/confirm/password) are excluded here; clicking buttons
# that plausibly destroy things stays gated.
DESTRUCTIVE_CLICK_RE = re.compile(
    r"\b(delete|remove|empty|pay|purchase|checkout|buy|transfer|send|submit|"
    r"uninstall|shut ?down|restart|sign ?out|log ?out)\b",
    re.IGNORECASE,
)

DANGEROUS_KEYS = {"alt+f4", "ctrl+alt+delete", "ctrl+alt+del"}
# "win" and win+<key> combos are intentionally allowed: opening the Start
# menu / search / run dialog is reversible navigation, and gating it hangs
# routine tasks behind a confirmation card. Truly destructive combos stay in
# DANGEROUS_KEYS above. Plain keys (including the delete key in a text
# editor) are reversible via undo and are NOT gated.


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
            return "ok"
        if DESTRUCTIVE_RE.search(detail or ""):
            return "confirm"
    return "ok"


def check_click_element(name: str) -> str:
    """Gate for clicking an accessibility element by its NAME (not value).

    Clicking is judged by the control's label with a vocabulary tuned to
    irreversible button actions, so routine editing UI (e.g. Word's "Format"
    button in Find & Replace) does not spam the approval card, while
    "Delete file"-style buttons still gate.
    """
    if DESTRUCTIVE_CLICK_RE.search(name or ""):
        return "confirm"
    return "ok"
