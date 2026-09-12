"""Backend entry point.

Sets per-monitor-v2 DPI awareness BEFORE importing anything that touches the
screen, then serves the WebSocket protocol from ARCHITECTURE.md on
ws://127.0.0.1:8765. Broadcasts every event to ALL connected clients (a
reconnecting Electron can never miss a running task's events); keeps serving
on disconnect and waits for reconnects.

Access control (per launch):
- A random 32-byte token is generated at startup and written to
  <config dir>/runtime_token with a user-only ACL; it is deleted on shutdown.
  Clients must present it in the X-PCU-Token header on every connection.
- http/https browser origins other than the app's own origin are rejected.
- Failed handshakes log (redacted) and close after a small delay to slow
  brute force. The API key is never used as the token and never appears in
  any URL.
"""

from __future__ import annotations

import asyncio
import ctypes
import hmac
import json
import os
import secrets as _secrets
import subprocess
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
from backend import secrets_filter
from backend.agent_loop import TaskRunner

HOST = "127.0.0.1"
PORT = int(os.environ.get("PCU_WS_PORT") or 8765)

TOKEN_FILE_NAME = "runtime_token"
TOKEN_HEADER = "X-PCU-Token"
REJECT_DELAY_S = 0.5
WS_CLOSE_UNAUTHORIZED = 1008
# Origins the app itself connects from; anything else http/https is a
# foreign browser origin and is rejected even with a valid token.
OWN_BROWSER_ORIGINS = frozenset({
    f"http://{HOST}:{PORT}", f"https://{HOST}:{PORT}",
    f"http://localhost:{PORT}", f"https://localhost:{PORT}",
})


def _request_headers(websocket: Any) -> Any:
    """Headers object across websockets versions (request.headers or direct)."""
    request = getattr(websocket, "request", None)
    if request is not None:
        headers = getattr(request, "headers", None)
        if headers is not None:
            return headers
    return getattr(websocket, "request_headers", None)


def _safe_print(line: str, *, stderr: bool = False) -> None:
    print(secrets_filter.filter_text(line), file=sys.stderr if stderr else sys.stdout,
          flush=True)


def is_browser_origin(origin: str | None) -> bool:
    o = (origin or "").strip().lower()
    return o.startswith(("http://", "https://"))


def check_ws_auth(origin: str | None, token: str | None,
                  expected_token: str) -> tuple[bool, str]:
    """Pure validation for one WS handshake. Returns (allowed, reason).

    Every connection needs the per-launch token. http/https origins that are
    not the app's own origin are additionally rejected as foreign browser
    origins, even when they carry a valid token.
    """
    if not expected_token:
        return False, "backend has no auth token"
    if not token:
        return False, "missing auth token"
    if not hmac.compare_digest(str(token), expected_token):
        return False, "invalid auth token"
    if is_browser_origin(origin):
        o = (origin or "").strip().lower()
        if o not in OWN_BROWSER_ORIGINS:
            return False, "foreign browser origin rejected"
    return True, "ok"


def _restrict_dacl_to_owner(path: Path) -> bool:
    """Reduce the file's DACL to its owner only (user-only access).

    Uses icacls: strip inherited ACEs, then grant full control to the
    OWNER RIGHTS well-known SID (S-1-3-4), which resolves to the file's
    current owner regardless of account type (local or Microsoft account).
    """
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", "*S-1-3-4:F"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class Backend:
    def __init__(self) -> None:
        self.clients: set[Any] = set()
        self.runner: TaskRunner | None = None
        self.runner_task: asyncio.Task[None] | None = None
        self.auth_token: str = ""
        self.last_status: dict[str, Any] = {
            "type": "status",
            "state": "idle",
            "message": "Backend started",
        }

    def issue_runtime_token(self) -> str:
        """Generate this launch's token; persist user-only, register secret."""
        self.auth_token = _secrets.token_urlsafe(32)
        secrets_filter.register_secret(self.auth_token)
        token_path = config_mod.CONFIG_PATH.parent / TOKEN_FILE_NAME
        try:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                token_path.unlink()
            except OSError:
                pass
            token_path.write_text(self.auth_token, encoding="utf-8")
            if not _restrict_dacl_to_owner(token_path):
                _safe_print(
                    "[CUA] warning: runtime_token ACL hardening unavailable; "
                    "token still lives in the user profile directory"
                )
        except OSError as exc:
            _safe_print(f"[CUA] runtime_token write failed: {exc}")
        return self.auth_token

    def clear_runtime_token(self) -> None:
        self.auth_token = ""
        try:
            (config_mod.CONFIG_PATH.parent / TOKEN_FILE_NAME).unlink()
        except OSError:
            pass

    async def send(self, message: dict[str, Any]) -> None:
        kind = message.get("type", "?")
        _safe_print(
            f"[CUA] -> {kind}: {json.dumps(secrets_filter.redact_obj(message), default=str)[:200]}"
        )
        if message.get("type") in ("status", "task_done"):
            self.last_status = dict(message)
        payload = json.dumps(secrets_filter.redact_obj(message))
        for conn in list(self.clients):
            try:
                await conn.send(payload)
            except Exception:
                self.clients.discard(conn)

    async def process_request(self, connection: Any, request: Any) -> Any:
        """Pre-handshake rejection (websockets >= 13 hook).

        Returning an HTTP response here refuses the connection BEFORE the
        HTTP 101 handshake completes. The in-handler check below stays as
        defense-in-depth and should never trigger for the normal flow.
        """
        headers = getattr(request, "headers", None)
        ok, reason = check_ws_auth(
            headers.get("Origin") if headers else None,
            headers.get(TOKEN_HEADER) if headers else None,
            self.auth_token,
        )
        if ok:
            return None
        _safe_print(f"[CUA] connection rejected ({reason}): pre-handshake")
        await asyncio.sleep(REJECT_DELAY_S)
        status = 403 if "origin" in reason else 401
        return connection.respond(status, "unauthorized")

    async def handler(self, websocket: Any) -> None:
        headers = _request_headers(websocket)
        peer = getattr(websocket, "remote_address", None)
        ok, reason = check_ws_auth(
            headers.get("Origin") if headers else None,
            headers.get(TOKEN_HEADER) if headers else None,
            self.auth_token,
        )
        if not ok:
            _safe_print(f"[CUA] connection rejected ({reason}): peer={peer}")
            await asyncio.sleep(REJECT_DELAY_S)
            try:
                await websocket.close(
                    code=WS_CLOSE_UNAUTHORIZED, reason="unauthorized"
                )
            except Exception:
                pass
            return
        self.clients.add(websocket)
        _safe_print(f"[CUA] client connected: {peer}")
        try:
            await self.send(self.last_status)
            async for raw in websocket:
                _safe_print(f"[CUA] <- {self._incoming_summary(raw)}")
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
            self.clients.discard(websocket)
            _safe_print("[CUA] client disconnected")

    def _incoming_summary(self, raw: str) -> str:
        """Log line for one incoming message; never echoes key material.

        rotate_key payloads are never printed at all. The incoming key is
        registered with the secrets filter BEFORE this summary is printed,
        so even a short non-pattern key cannot appear unredacted in stdout.
        """
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return raw[:300]
        if isinstance(message, dict) and message.get("type") == "rotate_key":
            self._register_incoming_key(message)
            source = str(message.get("keySource", "") or "bundled")
            return f"rotate_key received (source={source}, version={message.get('keyVersion')!r})"
        return raw[:300]

    def _register_incoming_key(self, message: dict[str, Any]) -> None:
        """Register a rotate_key's key with the filter before any logging."""
        key = str(message.get("apiKey", "") or "")
        if not key:
            blob = str(message.get("apiKeyEncrypted", "") or "")
            if blob:
                decrypted = config_mod.secret_store.unprotect(blob)
                if decrypted is not None:
                    key = decrypted
        if key:
            secrets_filter.register_secret(key)

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
        elif msg_type == "rotate_key":
            await self.handle_rotate_key(message)
        elif msg_type == "restore_bundled":
            await self.handle_restore_bundled()
        else:
            await self.send({"type": "log", "line": f"unknown message type: {msg_type!r}"})

    async def _key_op_result(self, op: str, ok: bool, error: str = "",
                             outcome: str = "") -> None:
        """Structured result for a key operation (rotate_key/restore_bundled).

        Never contains key material; the error text is user-facing and
        redacted by send() like every other broadcast.
        """
        reply: dict[str, Any] = {"type": "key_op_result", "op": op, "ok": ok}
        if outcome:
            reply["outcome"] = outcome
        if error:
            reply["error"] = error
        await self.send(reply)

    async def handle_rotate_key(self, message: dict[str, Any]) -> None:
        """Rotation trigger for Electron/doctor (see backend.config.rotate_key).

        Fail-closed: on any failure the previously stored credential is left
        untouched and a key_op_result reports it. The incoming key is
        registered with the secrets filter before any logging (see
        _incoming_summary) and is never echoed back.
        """
        new_key = str(message.get("apiKey", "") or "")
        blob = str(message.get("apiKeyEncrypted", "") or "")
        if blob and not new_key:
            decrypted = config_mod.secret_store.unprotect(blob)
            if decrypted is None:
                await self._key_op_result(
                    "rotate_key", False,
                    "provided key blob could not be decrypted; existing key kept")
                return
            new_key = decrypted
        if not new_key.strip():
            await self._key_op_result(
                "rotate_key", False, "empty key ignored; existing key kept")
            return
        version: Any = message.get("keyVersion")
        try:
            outcome = config_mod.rotate_key(
                new_key,
                version,
                key_source=str(message.get("keySource", "") or "bundled"),
            )
        except (OSError, config_mod.ConfigWriteError):
            # Fail-closed: protection failed, previous encrypted key intact.
            await self._key_op_result(
                "rotate_key", False,
                "secure storage unavailable; existing key kept")
            await self.send({"type": "log",
                             "line": "key rotation failed: secure storage "
                                     "unavailable; existing key kept"})
            return
        ok = outcome in ("replaced", "same")
        error = "" if ok else \
            "key not changed: an existing user key is never replaced by a bundled rotation"
        await self._key_op_result("rotate_key", ok, error, outcome=outcome)
        await self.send({"type": "log", "line": f"key rotation outcome: {outcome}"})

    async def handle_restore_bundled(self) -> None:
        """Restore the last-known bundled key ("use built-in key").

        Explicit user action, so replacing a stored user key is allowed.

        Fail-closed: when no bundled backup exists, it is unreadable, or
        persistence fails, the current credential is left untouched and the
        failure is reported. Key material is never logged; the restored key
        is registered with the secrets filter before any output.
        """
        result = config_mod.restore_bundled()
        if result.get("ok"):
            key = config_mod.decrypt_key(config_mod.load())
            if key:
                secrets_filter.register_secret(key)
            _safe_print("[CUA] built-in key restored as the active credential")
        else:
            _safe_print(f"[CUA] built-in key restore failed: {result.get('error')}")
        await self._key_op_result(
            "restore_bundled", bool(result.get("ok")),
            str(result.get("error") or ""))

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
        _safe_print("[CUA] stop requested")

    def handle_confirm(self, message: dict[str, Any]) -> None:
        confirm_id = str(message.get("id", ""))
        approved = bool(message.get("approved", False))
        if self.runner is None or not self.runner.resolve_confirm(confirm_id, approved):
            _safe_print(
                f"[CUA] confirm ignored (no matching pending gate): {confirm_id}"
            )


def _register_config_secrets() -> None:
    """Teach the filter the configured key(s) so exact matches get redacted."""
    cfg = config_mod.load()
    for section in config_mod.PROVIDER_SECTIONS:
        sub = cfg.get(section)
        if isinstance(sub, dict):
            key = sub.get("api_key")
            if isinstance(key, str) and key.strip():
                secrets_filter.register_secret(key)
    top = cfg.get("apiKey")
    if isinstance(top, str) and top.strip():
        secrets_filter.register_secret(top)


async def amain() -> None:
    _register_config_secrets()
    backend = Backend()
    backend.issue_runtime_token()
    try:
        async with websockets.serve(
            backend.handler, HOST, PORT, max_size=2**22,
            process_request=backend.process_request,
        ):
            _safe_print(f"[CUA] backend listening on ws://{HOST}:{PORT}")
            await asyncio.Future()
    finally:
        backend.clear_runtime_token()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
