"""CUA orchestrator: capture -> provider -> safety gate -> execute, in a loop."""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Awaitable, Callable

import pyautogui

from backend import a11y, control, cursor, safety, screen, secrets_filter
from backend.providers import StepResult, get_provider
from backend.trajectory import new_recorder

SendFn = Callable[[dict[str, Any]], Awaitable[None]]

CONFIRM_TIMEOUT_S = 120.0
WAIT_CHUNK_S = 0.2
MAX_WAIT_S = 10.0
PROVIDER_STEP_TIMEOUT_S = 30.0


class _TaskStopped(Exception):
    """Raised when the user requested a stop; handled inside TaskRunner.run."""


def _describe(action: dict[str, Any]) -> str:
    kind = str(action.get("kind", "?"))
    if kind in ("click", "double_click", "right_click", "move"):
        return f"{kind} at ({action.get('x')}, {action.get('y')})"
    if kind == "click_element":
        return f"click_element id={action.get('id')}"
    if kind == "scroll":
        return f"scroll {action.get('direction', 'down')} x{action.get('amount', 3)}"
    if kind == "type":
        # Display/persist/broadcast surface only: NEVER include the typed
        # text here (it is often a password/PIN the secrets filter cannot
        # pattern-match). The provider payload and control.type_text keep
        # the real text so the automation still types it.
        text = str(action.get("text") or "")
        return f"type [typed text withheld: {len(text)} chars]"
    if kind == "key":
        return f"press {action.get('key', '')}"
    if kind == "wait":
        return f"wait {action.get('amount', 1)}s"
    return kind


class TaskRunner:
    """Runs one CUA task; one instance per start_task."""

    def __init__(
        self,
        task_id: str,
        instruction: str,
        cfg: dict[str, Any],
        send: SendFn,
    ) -> None:
        self.task_id = task_id
        self.instruction = instruction
        self.cfg = cfg
        self.send = send
        self.max_steps = int(cfg.get("max_steps") or safety.MAX_STEPS)
        try:
            delay = float(cfg.get("action_delay_s", 0.4))
        except (TypeError, ValueError):
            delay = 0.4
        self.action_delay_s = max(0.0, min(5.0, delay))
        try:
            glide = float(cfg.get("pointer_glide_s", 0.45))
        except (TypeError, ValueError):
            glide = 0.35
        self.pointer_glide_s = max(0.0, min(1.0, glide))
        self.stop_event = asyncio.Event()
        self._confirm_event = asyncio.Event()
        self._confirm_id: str | None = None
        self._confirm_approved: bool | None = None
        self._confirm_seq = 0
        self._finished = False
        self._current_a11y_elements: list[dict[str, Any]] = []
        self._a11y_unavailable_logged = False
        self._recorder = new_recorder(
            task_id, instruction,
            save_screenshots=bool(cfg.get("saveScreenshots", False)),
        )
        self._steps = 0
        self._completion_held = False

    async def _emit(self, message: dict[str, Any]) -> None:
        # Redact at the boundary so trajectory files, WS clients and stdout
        # never see secret material (typed text, provider errors, etc.).
        safe_message = secrets_filter.redact_obj(message)
        self._recorder.save_event(safe_message)
        await self.send(safe_message)

    async def _log(self, line: str) -> None:
        await self._emit({"type": "log", "line": line})

    async def _status(
        self, state: str, message: str, step: int | None = None
    ) -> None:
        body: dict[str, Any] = {"type": "status", "state": state, "message": message}
        if step is not None:
            body["step"] = step
            body["max_steps"] = self.max_steps
        await self._emit(body)

    async def _finish(self, success: bool, summary: str) -> None:
        self._finished = True
        self._recorder.finish(success, summary, self._steps)
        await self._emit({
            "type": "task_done",
            "id": self.task_id,
            "success": success,
            "summary": summary,
        })

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise _TaskStopped()

    def resolve_confirm(self, confirm_id: str, approved: bool) -> bool:
        """Answer a pending safety gate. Returns True if it matched."""
        if self._confirm_id is not None and confirm_id == self._confirm_id:
            self._confirm_approved = approved
            self._confirm_id = None
            self._confirm_event.set()
            return True
        return False

    async def _sleep_checked(self, seconds: float) -> None:
        waited = 0.0
        while waited < seconds:
            self._check_stop()
            chunk = min(WAIT_CHUNK_S, seconds - waited)
            await asyncio.sleep(chunk)
            waited += chunk

    async def _gate(self, detail: str) -> bool:
        """Show need_confirmation and await the answer. Raises on timeout/stop."""
        self._confirm_seq += 1
        confirm_id = f"{self.task_id}#confirm{self._confirm_seq}"
        self._confirm_id = confirm_id
        self._confirm_approved = None
        self._confirm_event.clear()
        await self._emit({
            "type": "need_confirmation",
            "id": confirm_id,
            "reason": "Safety gate matched a potentially destructive action",
            "detail": detail,
        })
        await self._status("awaiting_confirmation", "Waiting for your approval")
        waited = 0.0
        while waited < CONFIRM_TIMEOUT_S:
            self._check_stop()
            if self._confirm_event.is_set():
                break
            await asyncio.sleep(WAIT_CHUNK_S)
            waited += WAIT_CHUNK_S
        if not self._confirm_event.is_set():
            raise RuntimeError("safety confirmation timed out after 120s")
        await self._status("running", "Resuming task")
        approved = self._confirm_approved
        if approved:
            await self._log(f"Approved by user: {detail}")
            return True
        await self._log(f"Refused by user, skipping: {detail}")
        return False

    def _glide_target(self) -> tuple[int, int] | None:
        """Focused control's center for pointer-glide-before-type/key."""
        try:
            center = a11y.get_focused_center()
        except Exception:
            center = None
        if not center or len(center) != 2:
            return None
        return center[0], center[1]

    async def _execute(
        self,
        action: dict[str, Any],
        scale_x: float,
        scale_y: float,
        origin_x: int,
        origin_y: int,
    ) -> None:
        kind = str(action.get("kind", ""))
        if kind in ("click", "double_click", "right_click", "move"):
            x = action.get("x")
            y = action.get("y")
            if x is None or y is None:
                raise ValueError(f"{kind} action missing coordinates")
            px = origin_x + round(float(x) * scale_x)
            py = origin_y + round(float(y) * scale_y)
            await asyncio.to_thread(control.move, px, py, self.pointer_glide_s)
            if kind == "click":
                await asyncio.to_thread(control.left_click)
            elif kind == "double_click":
                await asyncio.to_thread(control.double_click)
            elif kind == "right_click":
                await asyncio.to_thread(control.right_click)
        elif kind == "click_element":
            px = action.get("px")
            py = action.get("py")
            if px is None or py is None:
                raise ValueError("click_element missing resolved coordinates")
            await asyncio.to_thread(control.move, px, py, self.pointer_glide_s)
            await asyncio.to_thread(control.left_click)
        elif kind == "scroll":
            amount = int(action.get("amount") or 1)
            direction = str(action.get("direction", "down"))
            await asyncio.to_thread(control.scroll, amount, direction)
        elif kind == "type":
            # Humanized: glide the pointer to the field that is about to
            # receive the text instead of typing with the cursor parked.
            target = await asyncio.to_thread(self._glide_target)
            if target is not None:
                await asyncio.to_thread(control.move, target[0], target[1], self.pointer_glide_s)
            await asyncio.to_thread(control.type_text, str(action.get("text", "")))
        elif kind == "key":
            target = await asyncio.to_thread(self._glide_target)
            if target is not None:
                await asyncio.to_thread(control.move, target[0], target[1], self.pointer_glide_s)
            await asyncio.to_thread(control.press_key, str(action.get("key", "")))
        elif kind == "wait":
            amount = min(MAX_WAIT_S, max(0.0, float(action.get("amount") or 1)))
            await self._sleep_checked(amount)
        else:
            raise ValueError(f"unknown action kind: {kind!r}")

    def _find_element(self, element_id: Any) -> dict[str, Any] | None:
        """Find an element by id in the CURRENT step's raw (physical) UI tree."""
        if element_id is None:
            return None
        try:
            wanted = int(element_id)
        except (TypeError, ValueError):
            return None
        for element in self._current_a11y_elements:
            if element.get("id") == wanted:
                return element
        return None

    async def _verify_typed(self, text: str) -> str:
        """Tri-state check: "verified" | "absent" | "unknown".

        The focused control (the element that received the typing) is checked
        first. A retry only happens on "absent" — a readable focused element
        that provably lacks the text. Shell/web windows with unreliable UIA
        text yield "unknown", which never triggers a retype (a false "absent"
        doubled the typed string into the Start search box in live testing).
        """
        needle = text.strip().lower()
        if not needle:
            return "unknown"
        focused: str | None = None
        try:
            focused = await asyncio.to_thread(a11y.read_focused_value)
        except Exception:
            focused = None
        if focused and needle in focused.lower():
            return "verified"
        foreground: str | None = None
        try:
            foreground = await asyncio.to_thread(a11y.read_foreground_text)
        except Exception:
            foreground = None
        if foreground and needle in foreground.lower():
            return "verified"
        if focused and focused.strip():
            return "absent"
        return "unknown"

    async def _verify_after_type(
        self,
        action: dict[str, Any],
        scale_x: float,
        scale_y: float,
        origin_x: int,
        origin_y: int,
    ) -> str:
        """Verify-after-action for type: OBSERVE ONLY, never retype.

        A blind retype doubles the text when UIA lags (live-proven: the Start
        search box got 'calculatorcalculator'). The model sees the next
        screenshot anyway and self-corrects — it is the retry mechanism.
        Returns the tri-state result so _run can hold completion when the
        last typed text was not confirmed on screen.
        """
        text = str(action.get("text") or "")
        if not text.strip():
            return "unknown"
        try:
            await asyncio.sleep(0.8)
            state = await self._verify_typed(text)
            if state == "verified":
                await self._log("verified: text landed")
            elif state == "absent":
                await self._log(
                    "warning: typed text not found in window (model will see "
                    "the current state next step)"
                )
            else:
                await self._log(
                    "note: window text unreadable, skipping typed-text verification"
                )
            return state
        except Exception as exc:
            await self._log(f"warning: typed-text verification failed: {exc}")
            return "unknown"

    async def _capture_a11y(
        self, scale_x: float, scale_y: float, origin_x: int, origin_y: int
    ) -> dict[str, Any] | None:
        """Grab the foreground UIA tree and convert centers to model space."""
        try:
            a11y_raw = await asyncio.to_thread(a11y.get_window_elements)
        except Exception:
            a11y_raw = None
        if a11y_raw is None:
            self._current_a11y_elements = []
            if not self._a11y_unavailable_logged:
                self._a11y_unavailable_logged = True
                await self._log("no UI tree available")
            return None
        self._current_a11y_elements = a11y_raw.get("elements") or []
        try:
            return a11y.build_model_context(
                a11y_raw, origin_x, origin_y, scale_x, scale_y
            )
        except Exception:
            return None

    async def run(self) -> None:
        cursor.hide_native_cursor()
        try:
            await self._run()
        except _TaskStopped:
            await self._status("error", "Stopped by user")
            await self._finish(False, "Stopped by user")
        except pyautogui.FailSafeException:
            await self._log(
                "Failsafe triggered (mouse in screen corner) - aborting task."
            )
            await self._status("error", "Failsafe triggered")
            await self._finish(False, "Aborted: mouse moved to a screen corner.")
        except asyncio.CancelledError:
            self._recorder.finish(False, "Stopped by user", self._steps)
            raise
        except Exception as exc:
            await self._status("error", str(exc))
            await self._finish(False, f"Task error: {exc}")
        finally:
            cursor.restore_native_cursor()

    async def _run(self) -> None:
        provider = get_provider(self.cfg)
        history: Any = None
        await self._log(f"Task {self.task_id} started: {self.instruction}")
        for step in range(1, self.max_steps + 1):
            self._check_stop()
            unverified_type = False
            await self._status("running", "Working on it", step)
            img = await asyncio.to_thread(screen.capture)
            model_img, scale_x, scale_y = screen.to_model_image(img)
            screenshot_b64 = screen.to_base64_png(model_img)
            origin_x, origin_y = screen.origin()
            self._steps = step
            self._recorder.save_screenshot(base64.b64decode(screenshot_b64), step)
            a11y_context = await self._capture_a11y(scale_x, scale_y, origin_x, origin_y)
            # One automatic retry when the provider hangs: nothing has been
            # executed yet, so the same screenshot/history payload is safe
            # to resend.
            provider_timeout: asyncio.TimeoutError | None = None
            for attempt in (1, 2):
                try:
                    result = await asyncio.wait_for(
                        provider.run_step(
                            screenshot_b64,
                            model_img.width,
                            model_img.height,
                            self.instruction,
                            history,
                            a11y_context=a11y_context,
                        ),
                        timeout=PROVIDER_STEP_TIMEOUT_S,
                    )
                    provider_timeout = None
                    break
                except asyncio.TimeoutError as exc:
                    provider_timeout = exc
                    if attempt == 1:
                        await self._log(
                            f"provider step timed out after "
                            f"{PROVIDER_STEP_TIMEOUT_S:.0f}s; retrying once"
                        )
                except asyncio.CancelledError:
                    raise
                except _TaskStopped:
                    raise
                except pyautogui.FailSafeException:
                    raise
                except Exception as exc:
                    raise RuntimeError(f"provider step failed: {exc}") from exc
            if provider_timeout is not None:
                raise RuntimeError(
                    f"provider step timed out after "
                    f"{PROVIDER_STEP_TIMEOUT_S:.0f}s (twice, with one retry)"
                )
            history = result.state
            if result.summary:
                await self._log(f"Model: {result.summary}")
            for action in result.actions:
                self._check_stop()
                kind = str(action.get("kind", "")).lower()
                if kind == "done":
                    if unverified_type and not self._completion_held:
                        self._completion_held = True
                        await self._log(
                            "holding completion for one more step: last typed "
                            "text was not verified"
                        )
                        result_done = False
                        break
                    await self._status(
                        "idle", action.get("summary") or result.summary or "Task completed."
                    )
                    await self._finish(
                        True, action.get("summary") or result.summary or "Task completed."
                    )
                    return
                if kind == "fail":
                    summary = action.get("summary") or result.summary or "Task failed."
                    await self._status("error", summary)
                    await self._finish(False, summary)
                    return
                detail = _describe(action)
                if kind == "click_element":
                    element = self._find_element(action.get("id"))
                    if element is None:
                        await self._log(
                            f"element id {action.get('id')} not in current UI tree"
                        )
                        continue
                    name = str(element.get("name") or "")
                    # Clicks are gated by the control's LABEL with the
                    # click-specific vocabulary; the field's VALUE is content
                    # (e.g. text in an edit box) and clicking it is benign.
                    if safety.check_click_element(name) == "confirm":
                        if not await self._gate(detail):
                            continue
                    center = element.get("center")
                    if not center or len(center) != 2:
                        await self._log(
                            f"element id {action.get('id')} has no clickable center"
                        )
                        continue
                    action = dict(action, px=center[0], py=center[1])
                else:
                    gate_detail = str(action.get("text") or action.get("key") or detail)
                    if safety.check(kind, gate_detail) == "confirm":
                        if not await self._gate(detail):
                            continue
                # Structured physical coords let the Electron overlay place
                # click effects at the target before the pointer arrives.
                action_msg: dict[str, Any] = {
                    "type": "action",
                    "kind": kind,
                    "detail": detail,
                }
                if kind in ("click", "double_click", "right_click", "move"):
                    try:
                        action_msg["x"] = origin_x + round(float(action.get("x")) * scale_x)
                        action_msg["y"] = origin_y + round(float(action.get("y")) * scale_y)
                    except (TypeError, ValueError):
                        pass
                elif kind == "click_element":
                    action_msg["x"] = action.get("px")
                    action_msg["y"] = action.get("py")
                await self._emit(action_msg)
                await self._execute(action, scale_x, scale_y, origin_x, origin_y)
                if kind == "type":
                    state = await self._verify_after_type(
                        action, scale_x, scale_y, origin_x, origin_y
                    )
                    if state in ("absent", "unknown"):
                        unverified_type = True
                if self.action_delay_s > 0:
                    await self._sleep_checked(self.action_delay_s)
            result_done = result.done
            if result.done and unverified_type and not self._completion_held:
                self._completion_held = True
                await self._log(
                    "holding completion for one more step: last typed "
                    "text was not verified"
                )
                result_done = False
            if result_done:
                await self._status("idle", result.summary or "Task completed.")
                await self._finish(True, result.summary or "Task completed.")
                return
        await self._status(
            "error", f"Reached max steps ({self.max_steps}) without completion"
        )
        await self._finish(
            False, f"Reached max steps ({self.max_steps}) without completion."
        )
