"""Provider adapters for the CUA loop.

Every adapter exposes the same async interface:

    async def run_step(screenshot_b64: str, width: int, height: int,
                       instruction: str, history,
                       a11y_context: dict | None = None) -> StepResult

``history`` is the ``state`` value returned by the previous step (or None on
the first step); adapters return their updated internal state back out.
``a11y_context`` is an optional UIA element list (see backend/a11y.py) with
centers already converted to model-image space; the openai_compat provider
uses it for ``click_element`` targeting, the strict pixel-space protocols
(openai, anthropic) accept and ignore it.
Actions use the NORMALIZED schema shared by all providers:

    kind in {click, double_click, right_click, click_element, scroll, type,
             key, move, wait, done, fail}
    click/double_click/right_click/move: x, y (model-image space)
    click_element: id (UIA element id from a11y_context)
    scroll: amount, direction (up/down/left/right)
    type: text | key: key | wait: amount (seconds)
    done/fail: optional summary
"""

from __future__ import annotations

from typing import Any

from .base import StepResult


def get_provider(cfg: dict[str, Any]) -> Any:
    """Build a provider adapter from the root config dict."""
    name = str(cfg.get("provider", "openai")).lower()
    if name == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(cfg)
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(cfg)
    if name == "openai_compat":
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(cfg)
    raise ValueError(f"unknown provider: {name!r}")
