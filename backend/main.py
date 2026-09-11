"""Backend entry point.

Sets per-monitor-v2 DPI awareness BEFORE importing anything that touches the
screen, then serves the WebSocket protocol from ARCHITECTURE.md on
ws://127.0.0.1:8765. Handles one Electron client at a time; keeps serving on
disconnect and waits for a reconnect.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except Exception:
        pass

import websockets

from backend import config as config_mod
from backend.agent_loop import TaskRunner

HOST = "127.0.0.1"
PORT = 8765


class Backend:
    def __init__(self) -> None:
        self.conn: Any = None
        self.runner: TaskRunner | None = None
        self.runner_task: asyncio.Task[None] | None = None
        self.last_status: dict[str, Any] = {
            "type": "status",
            "state": "idle",
            "message": "Backend started",
        }

    async def send(self, message: dict[str, Any]) -> None:
        kind = message.get("type", "?")
        print(f"[CUA] -> {kind}: {json.dumps(message, default=str)[:200]}", flush=True)
        if message.get("type") in ("status", "task_done"):
            self.last_status = dict(message)
        conn = self.conn
        if conn is None:
            return
        try:
            await conn.send(json.dumps(message))
        except Exception:
            pass

    async def handler(self, websocket: Any) -> None:
        if self.conn is not None:
            try:
                await self.conn.close(code=4000, reason="new client connected")
            except Exception:
                pass
        self.conn = websocket
        peer = getattr(websocket, "remote_address", None)
        print(f"[CUA] client connected: {peer}", flush=True)
        try:
            async for raw in websocket:
                print(f"[CUA] <- {raw[:300]}", flush=True)
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    await self.send({"type": "log", "line": "malformed JSON message"})
                    continue
                if not isinstance(message, dict):
                    await self.send({"type": "log", "line": "message must be a JSON object"})
                    continue
                await self.handle(message)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self.conn is websocket:
                self.conn = None
            print("[CUA] client disconnected; waiting for reconnect", flush=True)

    async def handle(self, message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        if msg_type == "start_task":
            await self.handle_start(message)
        elif msg_type == "stop_task":
            await self.handle_stop()
        elif msg_type == "confirm":
            self.handle_confirm(message)
        elif msg_type == "get_status":
            await self.send(self.last_status)
        else:
            await self.send({"type": "log", "line": f"unknown message type: {msg_type!r}"})

    async def handle_start(self, message: dict[str, Any]) -> None:
        if self.runner_task is not None and not self.runner_task.done():
            await self.send({"type": "log", "line": "a task is already running"})
            await self.send(self.last_status)
            return
        task_id = str(message.get("id", ""))
        instruction = str(message.get("instruction", ""))
        if not task_id or not instruction.strip():
            await self.send({"type": "log", "line": "start_task needs id and instruction"})
            return
        cfg = config_mod.load()
        runner = TaskRunner(task_id, instruction, cfg, self.send)
        self.runner = runner
        self.runner_task = asyncio.create_task(self._guarded(runner))

    async def _guarded(self, runner: TaskRunner) -> None:
        try:
            await runner.run()
        except asyncio.CancelledError:
            if runner._finished:
                return
            await self.send({
                "type": "task_done",
                "id": runner.task_id,
                "success": False,
                "summary": "Stopped by user",
            })
            await self.send({"type": "status", "state": "idle", "message": "Stopped by user"})

    async def handle_stop(self) -> None:
        if self.runner_task is None or self.runner_task.done():
            await self.send({"type": "log", "line": "no task running"})
            return
        if self.runner is not None:
            self.runner.stop_event.set()
        self.runner_task.cancel()
        print("[CUA] stop requested", flush=True)

    def handle_confirm(self, message: dict[str, Any]) -> None:
        confirm_id = str(message.get("id", ""))
        approved = bool(message.get("approved", False))
        if self.runner is None or not self.runner.resolve_confirm(confirm_id, approved):
            print(f"[CUA] confirm ignored (no matching pending gate): {confirm_id}",
                  flush=True)


async def amain() -> None:
    backend = Backend()
    async with websockets.serve(backend.handler, HOST, PORT, max_size=2**22):
        print(f"[CUA] backend listening on ws://{HOST}:{PORT}", flush=True)
        await asyncio.Future()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
