"""Self-contained run trajectory recorder plus retention pruning.

Designed to be wired into the agent loop LATER (this module must not import
agent_loop). Every method swallows all exceptions and prints once to stdout on
failure so callers can never crash because of recording.

All persisted text (instructions, events, actions, result summaries) is
routed through backend.secrets_filter before it hits disk; screenshots are
only persisted when explicitly enabled (``save_screenshots=True``, config
``saveScreenshots`` defaults to False in the family build). When enabled,
``screenshot_mode`` selects between the lighter rolling ``last_step.png``
(one file per run, mode "last") and the legacy per-step ``step_NN.png``
(mode "all").

``prune_trajectory_root`` provides bounded, best-effort retention over the
trajectory root: run dirs past an age limit and a total-size cap are deleted
(never raising, once-guarded summary), so storage cannot grow unbounded.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend import secrets_filter

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = ROOT / "trajectories"

# Run dirs are exactly "<UTC ts YYYYMMDDT HHMMSS>_<sanitized task id>"; see
# _ensure_dir. Anything else in the root is never touched by pruning.
_RUN_DIR_RE = re.compile(r"^\d{8}T\d{6}_.+$")
PROVIDER_DEBUG_DIR = "_provider_debug"

# Module-level once-guard for the prune summary (openai_compat._DEBUG_REPORTED
# pattern): at most one retention line per process, never an exception.
_PRUNE_REPORTED = {"summary": False}


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
        screenshot_mode: str = "last",
    ) -> None:
        self.task_id = str(task_id)
        self.instruction = str(instruction)
        self.root = Path(root)
        self.save_screenshots = bool(save_screenshots)
        self.screenshot_mode = (
            "all" if str(screenshot_mode or "").strip().lower() == "all" else "last"
        )
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
        # "last" mode rolls into a single last_step.png (write to a .tmp name
        # then replace, mirroring config.py staging) so one task costs at
        # most one screenshot; "all" keeps the legacy per-step files.
        name = (
            "step_{:02d}.png".format(int(step))
            if self.screenshot_mode == "all"
            else "last_step.png"
        )
        try:
            path = self._path(name)
        except Exception as exc:
            self._report("save_screenshot", f"could not prepare screenshot path: {exc}")
            return
        if path is None:
            return
        if self.screenshot_mode == "last":
            tmp = path.with_name(path.name + ".tmp")
            try:
                tmp.write_bytes(png_bytes)
                os.replace(tmp, path)
            except Exception as exc:
                self._report("save_screenshot", f"could not write screenshot {path.name}: {exc}")
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
    screenshot_mode: str = "last",
) -> TrajectoryRecorder:
    """Create a recorder; PCU_TRAJECTORY_DIR env var overrides the default root.

    Screenshots are persisted only when ``save_screenshots`` is True (config
    ``saveScreenshots``; defaults to False in the family build). The mode
    (config ``saveScreenshotsMode``: "last" | "all") picks the rolling
    single-file behavior (default) or the legacy per-step files.
    """
    if root is None:
        root = os.environ.get("PCU_TRAJECTORY_DIR") or DEFAULT_ROOT
    return TrajectoryRecorder(
        task_id, instruction, root, save_screenshots=save_screenshots,
        screenshot_mode=screenshot_mode,
    )


def _dir_size_mb(path: Path) -> float:
    """Recursive size of a directory tree in MB (0 on any error)."""
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0.0
    return total / (1024.0 * 1024.0)


def _rmtree_quiet(path: Path) -> bool:
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        return False


def prune_trajectory_root(
    root: str | Path | None = None,
    max_age_days: float | None = 0,
    max_total_mb: float | None = 0,
) -> dict[str, Any]:
    """Best-effort retention over the trajectory root. Never raises.

    Deletes run dirs (``<YYYYMMDDTHHMMSS>_<id>`` naming, see _ensure_dir)
    whose directory mtime is older than ``max_age_days``, then enforces
    ``max_total_mb`` by deleting oldest-mtime run dirs first until under
    the cap. Also prunes ``<root>/_provider_debug`` by the age rule (files
    only; the dir is removed when left empty). Anything else in the root is
    never touched. ``0``/``None`` disables the respective rule. Returns
    {"removed": N, "freed_mb": X}.
    """
    result = {"removed": 0, "freed_mb": 0.0}
    try:
        root_path = Path(root) if root is not None else (
            Path(os.environ.get("PCU_TRAJECTORY_DIR") or DEFAULT_ROOT)
        )
    except Exception:
        return result
    if not root_path.is_dir():
        return result
    try:
        age_days = float(max_age_days) if max_age_days else 0.0
    except (TypeError, ValueError):
        age_days = 0.0
    try:
        cap_mb = float(max_total_mb) if max_total_mb else 0.0
    except (TypeError, ValueError):
        cap_mb = 0.0
    now = time.time()

    run_dirs: list[tuple[Path, float, float]] = []
    try:
        candidates = [p for p in root_path.iterdir() if p.is_dir()]
    except OSError:
        candidates = []
    for path in candidates:
        try:
            if path.name == PROVIDER_DEBUG_DIR:
                result = {**result, **_prune_provider_debug(path, now, age_days)}
                continue
            if not _RUN_DIR_RE.match(path.name):
                continue
            stat = path.stat()
            age_cutoff = now - age_days * 86400.0
            if age_days > 0 and stat.st_mtime < age_cutoff:
                freed = _dir_size_mb(path)
                if _rmtree_quiet(path):
                    result["removed"] += 1
                    result["freed_mb"] = round(result["freed_mb"] + freed, 2)
                continue
            run_dirs.append((path, stat.st_mtime, _dir_size_mb(path)))
        except OSError:
            continue

    if cap_mb > 0:
        total_mb = sum(size for _, _, size in run_dirs)
        # Oldest mtime first until the surviving total is under the cap.
        for path, _, size in sorted(run_dirs, key=lambda item: item[1]):
            if total_mb <= cap_mb:
                break
            if _rmtree_quiet(path):
                total_mb -= size
                result["removed"] += 1
                result["freed_mb"] = round(result["freed_mb"] + size, 2)

    if not _PRUNE_REPORTED["summary"]:
        _PRUNE_REPORTED["summary"] = True
        print(
            f"[trajectory] retention: removed {result['removed']} run dir(s), "
            f"freed {result['freed_mb']:.2f} MB "
            f"({secrets_filter.filter_text(str(root_path))})",
            file=sys.stdout, flush=True,
        )
    return result


def _prune_provider_debug(
    debug_dir: Path, now: float, age_days: float
) -> dict[str, Any]:
    """Age-prune files inside <root>/_provider_debug; remove the dir if empty."""
    result = {"removed": 0, "freed_mb": 0.0}
    if age_days <= 0:
        return result
    cutoff = now - age_days * 86400.0
    try:
        entries = [p for p in debug_dir.iterdir() if p.is_file()]
    except OSError:
        return result
    for path in entries:
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            size_mb = path.stat().st_size / (1024.0 * 1024.0)
            path.unlink()
            result["removed"] += 1
            result["freed_mb"] = round(result["freed_mb"] + size_mb, 2)
        except OSError:
            continue
    try:
        next(debug_dir.iterdir())
    except StopIteration:
        if not _rmtree_quiet(debug_dir):
            pass
    except OSError:
        pass
    return result
