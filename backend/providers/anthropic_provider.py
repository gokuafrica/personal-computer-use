"""Anthropic Messages API provider (computer use tool).

Uses the single ``computer`` tool shape with the ``computer-use-2025-01-24``
beta header. The tool version, beta string and model are configurable via
``anthropic.tool_version`` / ``anthropic.beta`` / ``anthropic.model`` keys.

display_width_px / display_height_px MUST be the downscaled image dimensions
we send, so returned coordinates are in that (model) space. Message history is
maintained manually: assistant tool_use blocks are followed by a user message
carrying tool_result blocks with the next screenshot.
"""

from __future__ import annotations

from typing import Any

from .. import secrets_filter
from .base import StepResult

DEFAULT_MODEL = "claude-3-7-sonnet-latest"
DEFAULT_TOOL_VERSION = "computer_20250124"
DEFAULT_BETA = "computer-use-2025-01-24"


def _image_block(b64: str) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": b64},
    }


def _map_tool_use(tool_use: Any) -> list[dict[str, Any]]:
    action = getattr(tool_use, "action", "")
    input_data: dict[str, Any] = getattr(tool_use, "input", {}) or {}
    coordinate = input_data.get("coordinate")
    x = int(coordinate[0]) if coordinate and len(coordinate) > 1 else None
    y = int(coordinate[1]) if coordinate and len(coordinate) > 1 else None
    if action == "left_click":
        return [{"kind": "click", "x": x, "y": y}]
    if action == "right_click":
        return [{"kind": "right_click", "x": x, "y": y}]
    if action == "double_click":
        return [{"kind": "double_click", "x": x, "y": y}]
    if action == "triple_click":
        return [{"kind": "click", "x": x, "y": y} for _ in range(3)]
    if action == "left_click_drag":
        # MVP: no drag support, click at the destination instead.
        return [{"kind": "click", "x": x, "y": y}]
    if action == "middle_click":
        return [{"kind": "click", "x": x, "y": y}]
    if action == "scroll":
        amount = int(input_data.get("scroll_amount", 3) or 3)
        return [{"kind": "scroll", "amount": amount,
                 "direction": input_data.get("scroll_direction", "down"), "x": x, "y": y}]
    if action == "type":
        return [{"kind": "type", "text": input_data.get("text", "")}]
    if action == "key":
        return [{"kind": "key", "key": input_data.get("key", "")}]
    if action == "hold_key":
        # MVP: hold not supported; press the key once.
        return [{"kind": "key", "key": input_data.get("key", "")}]
    if action == "mouse_move":
        return [{"kind": "move", "x": x, "y": y}]
    if action in ("screenshot", "cursor_position"):
        return []
    if action == "wait":
        return [{"kind": "wait", "amount": 1}]
    return []


class AnthropicProvider:
    def __init__(self, cfg: dict[str, Any]) -> None:
        section = cfg.get("anthropic", {})
        self.model = section.get("model") or DEFAULT_MODEL
        self.tool_version = section.get("tool_version") or DEFAULT_TOOL_VERSION
        self.beta = section.get("beta") or DEFAULT_BETA
        self._client = None

    def _get_client(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic()
        return self._client

    def _tool(self, width: int, height: int) -> dict[str, Any]:
        return {
            "type": self.tool_version,
            "name": "computer",
            "display_width_px": width,
            "display_height_px": height,
            "display_number": 1,
        }

    async def run_step(
        self,
        screenshot_b64: str,
        width: int,
        height: int,
        instruction: str,
        history: Any,
        a11y_context: dict[str, Any] | None = None,
    ) -> StepResult:
        if a11y_context:
            print(secrets_filter.filter_text(
                "[anthropic] a11y_context present but ignored (pixel-space protocol)"),
                flush=True)
        client = self._get_client()
        messages: list[dict[str, Any]] = [dict(m) for m in history] if history else []
        if not messages:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    _image_block(screenshot_b64),
                ],
            })
        else:
            pending_ids: list[str] = []
            for message in reversed(messages):
                if message.get("role") != "assistant":
                    break
                for block in message.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        pending_ids.append(block["id"])
            pending_ids.reverse()
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": [_image_block(screenshot_b64)],
                    }
                    for tool_use_id in pending_ids
                ],
            })

        response = await client.beta.messages.create(
            model=self.model,
            max_tokens=2048,
            tools=[self._tool(width, height)],
            messages=messages,
            extra_headers={"anthropic-beta": self.beta},
        )

        blocks = list(getattr(response, "content", []) or [])
        new_history = messages + [{"role": "assistant", "content": blocks}]
        actions: list[dict[str, Any]] = []
        summary_parts: list[str] = []
        for block in blocks:
            block_type = getattr(block, "type", block.get("type") if isinstance(block, dict) else None)
            if block_type == "text":
                summary_parts.append(getattr(block, "text", "") or "")
            elif block_type == "tool_use":
                actions.extend(_map_tool_use(block))
        if actions:
            return StepResult(actions=actions, done=False, summary=" ".join(summary_parts),
                              state=new_history)
        return StepResult(actions=[], done=True,
                          summary=" ".join(summary_parts) or "Task completed.", state=new_history)
