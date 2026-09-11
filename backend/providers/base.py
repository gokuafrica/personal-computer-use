"""Shared StepResult type for provider adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepResult:
    """One model turn: normalized actions plus provider-internal state.

    Actions use the normalized schema shared by all providers (see the package
    docstring); ``state`` is opaque data the loop feeds back as ``history``.
    """

    actions: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    summary: str = ""
    state: Any = None
