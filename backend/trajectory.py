"""Self-contained run trajectory recorder.

Designed to be wired into the agent loop LATER (this module must not import
agent_loop). Every method swallows all exceptions and prints once to stdout on
failure so callers can never crash because of recording.

All persisted text (instructions, events, actions, result summaries) is
routed through backend.secrets_filter before it hits disk; screenshots are
only persisted when explicitly enabled (``save_screenshots=True``, config
``saveScreenshots`` defaults to False in the family build).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend import secrets_filter

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = ROOT / "trajectories"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip())
    cleaned = cleaned.strip("_")
    return cleaned[:60] or "task"


class TrajectoryRecorder:
    """Records screenshots, actions, events, and a final result for one task.

    The run directory ``<root>/<UTC timestamp>_<sanitized task_id>`` is created
    lazily on the first write, together with ``task.json``.
    """

    def __init__(
        self,
        task_id: str,
        instruction: str,
        root: str | Path = "trajectories",
        save_screenshots: bool = False,
    ) -> None:
        self.task_id = str(task_id)
        self.instruction = str(instruction)
        self.root = Path(root)
        self.save_screenshots = bool(save_screenshots)
        self.started_at = _now_iso()
        self._mono_start = time.monotonic()
        self._dir: Path | None = None
        self._reported: set[str] = set()

    def _report(self, key: str, message: str) -> None:
        if key not in self._reported:
            self._reported.add(key)
            print(f"[trajectory] {secrets_filter.filter_text(message)}",
                  file=sys.stdout, flush=True)

    def _ensure_dir(self) -> Path | None:
        if self._dir is not None:
            return self._dir
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            run_dir = self.root / f"{ts}_{_sanitize(self.task_id)}"
            run_dir.mkdir(parents=True, exist_ok=False)
            task_json = {
                "task_id": self.task_id,
                "instruction": secrets_filter.redact_text(self.instruction),
                "started_at": self.started_at,
            }
            (run_dir / "task.json").write_text(
                json.dumps(task_json, indent=2), encoding="utf-8"
            )
            self._dir = run_dir
            return run_dir
        except Exception as exc:
            self._report("ensure_dir", f"could not create run directory: {exc}")
            return None

    def _path(self, name: str) -> Path | None:
        run_dir = self._ensure_dir()
        if run_dir is None:
            return None
        return run_dir / name

    def _append_jsonl(self, filename: str, record: dict[str, Any], key: str) -> None:
        path = self._path(filename)
        if path is None:
            return
        try:
            record = secrets_filter.redact_obj(record)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            self._report(key, f"could not write {filename}: {exc}")

    def save_screenshot(self, png_bytes: bytes, step: int) -> None:
        if not self.save_screenshots:
            return
        try:
            path = self._path(f"step_{int(step):02d}.png")
        except Exception as exc:
            self._report("save_screenshot", f"could not prepare screenshot path: {exc}")
            return
        if path is None:
            return
        try:
            path.write_bytes(png_bytes)
        except Exception as exc:
            self._report("save_screenshot", f"could not write screenshot {path.name}: {exc}")

    def save_action(self, record: dict) -> None:
        self._append_jsonl("actions.jsonl", {**record, "ts": _now_iso()}, "save_action")

    def save_event(self, record: dict) -> None:
        self._append_jsonl("events.jsonl", {**record, "ts": _now_iso()}, "save_event")

    def finish(self, success: bool, summary: str, steps: int) -> None:
        try:
            path = self._path("result.json")
        except Exception as exc:
            self._report("finish", f"could not prepare result path: {exc}")
            return
        if path is None:
            return
        try:
            finished_at = _now_iso()
            duration_s = time.monotonic() - self._mono_start
            result = {
                "task_id": self.task_id,
                "success": bool(success),
                "summary": secrets_filter.redact_text(str(summary)),
                "steps": int(steps),
                "started_at": self.started_at,
                "finished_at": finished_at,
                "duration_s": round(duration_s, 3),
            }
            path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        except Exception as exc:
            self._report("finish", f"could not write result.json: {exc}")


def new_recorder(
    task_id: str,
    instruction: str,
    root: str | Path | None = None,
    save_screenshots: bool = False,
) -> TrajectoryRecorder:
    """Create a recorder; PCU_TRAJECTORY_DIR env var overrides the default root.

    Screenshots are persisted only when ``save_screenshots`` is True (config
    ``saveScreenshots``; defaults to False in the family build).
    """
    if root is None:
        root = os.environ.get("PCU_TRAJECTORY_DIR") or DEFAULT_ROOT
    return TrajectoryRecorder(
        task_id, instruction, root, save_screenshots=save_screenshots
    )
