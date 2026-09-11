"""OpenAI Responses API provider (computer-use-preview).

Uses the ``computer`` tool with previous_response_id chaining. Coordinates in
computer_call actions are already in the sent-image space we declared via
display_width/display_height, so they map 1:1 onto the normalized schema.
"""

from __future__ import annotations

from typing import Any

from .base import StepResult

DEFAULT_MODEL = "computer-use-preview"


def _data_uri(b64: str) -> str:
    return f"data:image/png;base64,{b64}"


def _map_action(action: Any) -> list[dict[str, Any]]:
    kind = getattr(action, "type", "")
    if kind == "click":
        button = getattr(action, "button", "left")
        if button == "double":
            return [{"kind": "double_click", "x": action.x, "y": action.y}]
        if button == "right":
            return [{"kind": "right_click", "x": action.x, "y": action.y}]
        return [{"kind": "click", "x": action.x, "y": action.y}]
    if kind == "double_click":
        return [{"kind": "double_click", "x": action.x, "y": action.y}]
    if kind == "scroll":
        amount = getattr(action, "scroll_y", 0) or 0
        if amount == 0:
            amount = getattr(action, "scroll_x", 0) or 0
        direction = "up" if amount > 0 else "down"
        return [{"kind": "scroll", "amount": max(1, abs(amount)), "direction": direction,
                 "x": getattr(action, "x", None), "y": getattr(action, "y", None)}]
    if kind == "keypress":
        keys = list(getattr(action, "keys", []) or [])
        return [{"kind": "key", "key": "+".join(keys)}]
    if kind == "type":
        return [{"kind": "type", "text": getattr(action, "text", "")}]
    if kind == "wait":
        return [{"kind": "wait", "amount": 1}]
    if kind == "move":
        return [{"kind": "move", "x": action.x, "y": action.y}]
    if kind == "drag":
        path = list(getattr(action, "path", []) or [])
        return [{"kind": "move", "x": point.x, "y": point.y} for point in path[1:]]
    if kind == "screenshot":
        return []
    return []


class OpenAIProvider:
    def __init__(self, cfg: dict[str, Any]) -> None:
        section = cfg.get("openai", {})
        self.model = section.get("model") or DEFAULT_MODEL
        self._client = None

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI()
        return self._client

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
            print("[openai] a11y_context present but ignored (pixel-space protocol)",
                  flush=True)
        client = self._get_client()
        state: dict[str, Any] = dict(history) if history else {}
        tools: list[dict[str, Any]] = [
            {"type": "computer", "display_width": width, "display_height": height,
             "environment": "windows"}
        ]
        if not state.get("previous_response_id"):
            response = await client.responses.create(
                model=self.model,
                input=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": instruction},
                        {"type": "input_image", "image_url": _data_uri(screenshot_b64)},
                    ],
                }],
                tools=tools,
                truncation="auto",
            )
        else:
            acknowledged = [
                {"id": c.get("id"), "code": c.get("code"), "message": c.get("message")}
                for c in state.get("pending_safety_checks", [])
            ]
            response = await client.responses.create(
                model=self.model,
                previous_response_id=state["previous_response_id"],
                input=[{
                    "type": "computer_call_output",
                    "call_id": state["call_id"],
                    "output": {
                        "type": "computer_screenshot",
                        "image_url": _data_uri(screenshot_b64),
                    },
                    "acknowledged_safety_checks": acknowledged,
                }],
                tools=tools,
                truncation="auto",
            )

        actions: list[dict[str, Any]] = []
        done = False
        summary = ""
        pending_checks: list[dict[str, Any]] = []
        for item in response.output:
            item_type = getattr(item, "type", "")
            if item_type == "computer_call":
                state["call_id"] = item.call_id
                checks = getattr(item, "pending_safety_checks", []) or []
                if checks:
                    print(
                        f"[openai] acknowledging {len(checks)} pending safety check(s); "
                        "continuing",
                        flush=True,
                    )
                    pending_checks = [
                        {"id": c.id, "code": c.code, "message": c.message} for c in checks
                    ]
                actions.extend(_map_action(item.action))
            elif item_type == "message":
                done = True
                parts = []
                for block in getattr(item, "content", []) or []:
                    text = getattr(block, "text", None)
                    if text:
                        parts.append(text)
                summary = "".join(parts)

        state["previous_response_id"] = response.id
        state["pending_safety_checks"] = pending_checks
        return StepResult(actions=actions, done=done, summary=summary, state=state)
