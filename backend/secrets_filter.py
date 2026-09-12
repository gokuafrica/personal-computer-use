"""Shared secret redaction filter for every persisted line and stdout print.

Redacts registered exact secrets (e.g. the configured API key) plus common
API-token/key patterns. Usable on strings, recursive JSON-safe structures
(dict/list), and whole text blobs. Placeholder: [REDACTED].

Cheap enough to call on every persisted line: exact-secret replacement is a
str.replace loop; patterns are compiled once at import.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

PLACEHOLDER = "[REDACTED]"

_SECRETS: set[str] = set()
# Longest-first so overlapping secrets are fully replaced.
_SORTED_SECRETS: list[str] = []

# Common token shapes: OpenAI/anthropic-style sk- keys, GitHub PATs, AWS
# access keys, Slack tokens, Bearer auth headers.
_PATTERN_RE = re.compile(
    r"(?P<sk>\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b)"
    r"|(?P<ghp>\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b)"
    r"|(?P<akia>\bAKIA[0-9A-Z]{16}\b)"
    r"|(?P<xox>\bxox[baprs]-[A-Za-z0-9-]{10,}\b)"
    r"|(?P<antkey>\bant_api[0-9A-Za-z_-]{20,}\b)"
)

_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=:-]{16,}")

_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.{0,4000}?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

# Generic long hex/base64 token that sits next to a key-like name
# (api_key=..., "token": ..., secret: ..., password=..., Authorization: ...).
_KEYLIKE = r"(?:api[_-]?key|token|secret|password|passwd|authorization)"
_LONG_TOKEN_RE = re.compile(
    r"(" + _KEYLIKE + r"[\"']?[ \t]*[:=,]+[ \t]*(?:\"|')?)"
    r"([A-Za-z0-9_+/=-]{32,})(?:\"|')?",
    re.IGNORECASE,
)


# Derived forms shorter than this are not registered (absurd false positives).
_DERIVED_MIN_LEN = 16


def _derived_forms(secret: str) -> list[str]:
    """Encoded forms of a secret that must also be redacted verbatim.

    Only precomputed exact strings (no pattern matching): base64 of the
    secret, base64 of its UTF-16LE bytes (how Windows/JSON payloads often
    encode text), and the JSON-escape form (json.dumps with ensure_ascii).
    Derived forms shorter than _DERIVED_MIN_LEN are skipped.
    """
    forms: list[str] = []
    encoded_latin = base64.b64encode(secret.encode("utf-8")).decode("ascii")
    encoded_utf16 = base64.b64encode(secret.encode("utf-16-le")).decode("ascii")
    forms.append(encoded_latin)
    if encoded_utf16 != encoded_latin:
        forms.append(encoded_utf16)
    json_escaped = json.dumps(secret, ensure_ascii=True)[1:-1]
    if json_escaped != secret:
        forms.append(json_escaped)
    return [f for f in forms if len(f) >= _DERIVED_MIN_LEN]


def register_secret(value: Any) -> None:
    """Add an exact secret string to redact. Never log or echo it."""
    if not isinstance(value, str):
        return
    value = value.strip()
    if not value or value in _SECRETS:
        return
    _SECRETS.add(value)
    for form in _derived_forms(value):
        _SECRETS.add(form)
    _SORTED_SECRETS[:] = sorted(_SECRETS, key=len, reverse=True)


def clear_secrets() -> None:
    """Forget all registered exact secrets (for tests / re-registration)."""
    _SECRETS.clear()
    _SORTED_SECRETS[:] = []


def _redact_exact(text: str) -> str:
    for secret in _SORTED_SECRETS:
        text = text.replace(secret, PLACEHOLDER)
    return text


def redact_text(text: str) -> str:
    """Redact registered secrets and known token patterns from a string."""
    if not isinstance(text, str):
        return text
    out = _redact_exact(text)
    out = _PATTERN_RE.sub(PLACEHOLDER, out)
    out = _BEARER_RE.sub(r"\1" + PLACEHOLDER, out)
    out = _PEM_RE.sub(PLACEHOLDER, out)
    out = _LONG_TOKEN_RE.sub(r"\1" + PLACEHOLDER, out)
    return out


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside dicts/lists (JSON-safe)."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        redacted = [redact_obj(v) for v in obj]
        return redacted if isinstance(obj, list) else type(obj)(redacted)
    return obj


def filter_text(text: str) -> str:
    """Alias for redact_text, for log/stdout boundary call sites."""
    return redact_text(text)


# Raw provider replies (opt-in debug dump) embed the model's action JSON;
# scrub the "text" payload of type actions so arbitrary typed credentials
# (passwords/PINs the exact-secret filter cannot know) never reach the dump.
_TYPED_TEXT_RE = re.compile(
    r'(?P<lead>"text"\s*:\s*")(?P<val>(?:[^"\\]|\\.)*)"',
    re.IGNORECASE,
)

TYPED_TEXT_PLACEHOLDER = "[TYPED TEXT WITHHELD]"


def scrub_typed_text(obj: Any) -> Any:
    """Replace the string value of ``"text"`` JSON fields with a placeholder.

    JSON-safe recursive (like redact_obj). Applied only to raw provider-reply
    dumps (keep action semantics, drop the secret value). Runs before JSON
    serialization so escaped payload strings are still matched.
    """
    if isinstance(obj, str):
        return _TYPED_TEXT_RE.sub(
            r"\g<lead>" + TYPED_TEXT_PLACEHOLDER + '"', obj)
    if isinstance(obj, dict):
        return {k: scrub_typed_text(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        scrubbed = [scrub_typed_text(v) for v in obj]
        return scrubbed if isinstance(obj, list) else type(obj)(scrubbed)
    return obj
