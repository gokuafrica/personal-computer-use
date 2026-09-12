"""Provider for generic OpenAI-compatible chat-completions VLM endpoints.

Config keys: ``openai_compat.base_url``, ``openai_compat.api_key``,
``openai_compat.model``. The model receives the screenshot plus the task
instruction and MUST reply with STRICT JSON (no prose, no code fences):

    {
      "actions": [
        {"kind": "click", "x": 640, "y": 400},
        {"kind": "double_click", "x": 640, "y": 400},
        {"kind": "right_click", "x": 640, "y": 400},
        {"kind": "move", "x": 640, "y": 400},
        {"kind": "click_element", "id": 12},
        {"kind": "scroll", "amount": 3, "direction": "down"},
        {"kind": "type", "text": "hello"},
        {"kind": "key", "key": "ctrl+s"},
        {"kind": "wait", "amount": 2},
        {"kind": "done", "summary": "..."},
        {"kind": "fail", "summary": "..."}
      ],
      "done": false,
      "summary": "one-line progress note"
    }

Coordinates are in the space of the image we sent (top-left origin), matching
the normalized schema the agent loop executes after scaling to physical
pixels. Parsing is defensive: code fences are stripped and the first/last
braces are used as a fallback.

Element grounding: when the agent loop supplies ``a11y_context``, a compact
list of UIA elements (with centers already converted to model-image space) is
appended to the user message and the model should prefer
``{"kind": "click_element", "id": <int>}`` over pixel coordinates whenever a
suitable element is listed.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import StepResult
from .. import secrets_filter

MAX_TOKENS = 4096

_DEBUG_REPORTED = {"dir": False}


def _raw_dump_enabled() -> bool:
    """Raw provider-reply dumps are OFF by default (family build).

    Enabled only by an explicit opt-in: PCU_RAW_PROVIDER_DUMP=1.
    """
    return os.environ.get("PCU_RAW_PROVIDER_DUMP", "").strip() == "1"


def _debug_dir() -> Path:
    """Directory for raw-reply dumps: <trajectory root>/_provider_debug.

    Uses PCU_TRAJECTORY_DIR (same override the trajectory recorder honors) so
    dumps land next to the run trajectories in both dev and packaged apps.
    """
    root = os.environ.get("PCU_TRAJECTORY_DIR") or (
        Path(__file__).resolve().parents[2] / "trajectories"
    )
    return Path(root) / "_provider_debug"


def _dump_raw(tag: str, payload: dict[str, Any]) -> None:
    """Append one redacted raw-reply record as JSONL; never raises.

    No-op unless PCU_RAW_PROVIDER_DUMP=1. Even when enabled, secret material
    is redacted before the record is written.
    """
    if not _raw_dump_enabled():
        return
    try:
        d = _debug_dir()
        d.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tag": tag,
            # Typed-action text (arbitrary passwords/PINs) is scrubbed from
            # the raw model reply even in this opt-in debug dump.
            **secrets_filter.scrub_typed_text(secrets_filter.redact_obj(payload)),
        }
        with open(d / "raw_replies.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if not _DEBUG_REPORTED.get("dir"):
            _DEBUG_REPORTED["dir"] = True
            print(f"[openai_compat] raw replies dumped to {d / 'raw_replies.jsonl'}",
                  flush=True)
    except Exception as exc:
        if not _DEBUG_REPORTED.get("dump_err"):
            _DEBUG_REPORTED["dump_err"] = True
            print(f"[openai_compat] could not dump raw reply: "
                  f"{secrets_filter.filter_text(str(exc))}", flush=True)

SYSTEM_PROMPT = (
    "You are a Windows computer-use agent driving a real desktop. You receive a "
    "screenshot and a task instruction. Decide the next actions and reply with "
    "STRICT JSON only, no markdown fences, matching exactly:\n"
    '{"actions": [{"kind": "...", ...}], "done": false, "summary": "string"}\n'
    "Action kinds and fields:\n"
    '{"kind": "click", "x": <int>, "y": <int>} | {"kind": "double_click", "x", "y"} | '
    '{"kind": "right_click", "x", "y"} | {"kind": "move", "x", "y"} | '
    '{"kind": "scroll", "amount": <int>, "direction": "up"|"down"|"left"|"right"} | '
    '{"kind": "click_element", "id": <int>} | '
    '{"kind": "type", "text": <string>} | {"kind": "key", "key": <string like "ctrl+s">} | '
    '{"kind": "wait", "amount": <seconds>} | {"kind": "done", "summary": <string>} | '
    '{"kind": "fail", "summary": <string>}\n'
    "Coordinates are pixel positions in the screenshot you were given, with (0,0) "
    "at the top-left. When the user message includes a UIA element list, its "
    "centers are already in the same screenshot space and you should prefer "
    '{"kind": "click_element", "id": N} over pixel coordinates when a suitable '
    "element is in the list; fall back to pixel coordinates otherwise. Keep "
    "actions minimal; set done=true and give a summary when the task is "
    "finished. Never invent action kinds outside the list above."
)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json(content: str) -> dict[str, Any] | None:
    candidate = _strip_fences(content)
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _format_a11y_context(context: dict[str, Any]) -> str:
    """Render the a11y element list as compact one-per-line text for the model.

    Element centers arrive already converted to model-image space by the agent
    loop; the prompt text says so explicitly.
    """
    lines = [
        "UIA element list for the foreground window"
        f" (window_title=\"{context.get('window_title', '')}\")."
        " The center coordinates below are ALREADY in the model-image space of"
        " the screenshot in this message:"
    ]
    for element in context.get("elements", []):
        center = element.get("center") or []
        if len(center) != 2:
            continue
        value = element.get("value")
        lines.append(
            f'id={element.get("id")} role={element.get("role")} '
            f'name="{element.get("name")}" '
            f'value="{value if value is not None else ""}" '
            f'center=({center[0]},{center[1]})'
        )
    if len(lines) == 1:
        lines.append("(no actionable elements found)")
    return "\n".join(lines)


def _sanitize_actions(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    actions: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).lower()
        if kind not in ("click", "double_click", "right_click", "click_element",
                        "scroll", "type", "key", "move", "wait", "done", "fail"):
            continue
        action = {"kind": kind}
        if kind == "click_element":
            try:
                action["id"] = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            actions.append(action)
            continue
        for field_name in ("x", "y", "amount", "direction", "text", "key", "summary"):
            if field_name in item:
                action[field_name] = item[field_name]
        actions.append(action)
    return actions


class OpenAICompatProvider:
    def __init__(self, cfg: dict[str, Any]) -> None:
        section = cfg.get("openai_compat", {})
        self.base_url = section.get("base_url") or None
        self.api_key = section.get("api_key") or None
        self.model = section.get("model") or ""
        self._session_id = str(uuid.uuid4())
        self._client = None

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self.api_key or "-",
                default_headers={
                    # OpenCode Go's edge rejects default SDK signatures (CF 1010)
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "personal-computer-use/0.1",
                    "x-opencode-session": self._session_id,
                },
            )
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
        client = self._get_client()
        if not self.model:
            raise ValueError("openai_compat.model is not configured")
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            notes = "; ".join(
                str(step.get("summary", "")) for step in history[-5:]
                if isinstance(step, dict)
            )
            if notes:
                messages.append({
                    "role": "user",
                    "content": f"Progress so far: {notes}",
                })
                messages.append({"role": "assistant", "content": "Understood."})
        user_text = f"Task: {instruction}"
        if a11y_context:
            user_text += "\n\n" + _format_a11y_context(a11y_context)
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{screenshot_b64}"
                }},
                {"type": "text", "text":
                 "Reply with the STRICT JSON object described in the system prompt."},
            ],
        })

        response = None
        try:
            response = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=MAX_TOKENS,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 - some endpoints reject response_format
            message = str(exc)
            if "response_format" not in message and "json" not in message.lower():
                raise
            print("[openai_compat] response_format unsupported; retrying without it",
                  flush=True)
            response = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=MAX_TOKENS,
            )

        def _reply_meta(resp: Any) -> dict[str, Any]:
            try:
                choice = resp.choices[0]
                return {
                    "finish_reason": choice.finish_reason,
                    "usage": (resp.usage.model_dump() if hasattr(resp.usage, "model_dump")
                              else None),
                }
            except Exception:
                return {}

        content = response.choices[0].message.content or ""
        parsed = _parse_json(content)
        if parsed is None:
            # Capture everything about the failed reply before deciding what to
            # do next; the dump is the only place the raw reply survives.
            _dump_raw("parse_fail", {
                "model": self.model,
                "attempt": 1,
                "content": content,
                "finish_reason": _reply_meta(response).get("finish_reason"),
                "usage": _reply_meta(response).get("usage"),
            })
            # One retry without response_format in case the JSON mode output
            # was malformed; keep messages identical otherwise.
            try:
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=MAX_TOKENS,
                )
                content = response.choices[0].message.content or ""
                meta = _reply_meta(response)
                parsed = _parse_json(content)
                if parsed is None:
                    _dump_raw("parse_fail", {
                        "model": self.model,
                        "attempt": 2,
                        "content": content,
                        "finish_reason": meta.get("finish_reason"),
                        "usage": meta.get("usage"),
                    })
                else:
                    _dump_raw("parse_recovered", {
                        "model": self.model,
                        "attempt": 2,
                        "content": content,
                        "finish_reason": meta.get("finish_reason"),
                        "usage": meta.get("usage"),
                    })
            except Exception as exc:  # noqa: BLE001 - retry is best-effort
                _dump_raw("retry_error", {
                    "model": self.model,
                    "error": f"{type(exc).__name__}: {exc}",
                })
        if parsed is None:
            print("[openai_compat] could not parse model reply as JSON", flush=True)
            return StepResult(actions=[{"kind": "wait", "amount": 1}],
                              done=False, summary="unparseable model response", state=None)
        actions = _sanitize_actions(parsed.get("actions"))
        done = bool(parsed.get("done"))
        summary = str(parsed.get("summary", ""))
        state = list(history) if history else []
        state.append({"summary": summary, "actions": actions})
        return StepResult(actions=actions, done=done, summary=summary, state=state)
