"""Unit tests for trajectory retention pruning and screenshot modes.

Covers:
- age pruning removes only old run dirs (fake mtimes via os.utime)
- size cap removes oldest-first
- non-run-dir files/dirs are never touched
- _provider_debug age pruning (files; dir removed when empty)
- pruning disabled when max_age_days / max_total_mb are 0
- "last" screenshot mode overwrites one last_step.png (.tmp staged)
- "all" mode keeps legacy per-step files
- config defaults/clamping for the retention keys

Run:  python -m unittest backend.tests.test_retention -v
Temp dirs always via tempfile.TemporaryDirectory() context managers.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path


def _make_run_dir(root: Path, name: str, *, size_bytes: int = 0) -> Path:
    """Create one fake run dir with a task.json and optional filler bytes."""
    run_dir = root / name
    run_dir.mkdir(parents=True)
    (run_dir / "task.json").write_text(
        json.dumps({"task_id": name}), encoding="utf-8"
    )
    if size_bytes > 0:
        (run_dir / "actions.jsonl").write_bytes(b"x" * size_bytes)
    return run_dir


def _set_mtime(path: Path, seconds_ago: float) -> None:
    stamp = time.time() - seconds_ago
    os.utime(path, (stamp, stamp))


class RetentionAgeTests(unittest.TestCase):
    def test_age_prune_removes_only_old_run_dirs(self):
        from backend.trajectory import prune_trajectory_root

        with tempfile.TemporaryDirectory(prefix="pcu-ret-age-") as tmp:
            root = Path(tmp)
            old = _make_run_dir(root, "20200101T000000_oldtask")
            recent = _make_run_dir(root, "20990101T000000_newtask")
            _set_mtime(old, 10 * 86400)  # 10 days old
            result = prune_trajectory_root(root, max_age_days=7, max_total_mb=0)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertEqual(result["removed"], 1)
            self.assertGreaterEqual(result["freed_mb"], 0.0)

    def test_age_prune_disabled_when_zero(self):
        from backend.trajectory import prune_trajectory_root

        with tempfile.TemporaryDirectory(prefix="pcu-ret-age0-") as tmp:
            root = Path(tmp)
            old = _make_run_dir(root, "20200101T000000_oldtask")
            _set_mtime(old, 365 * 86400)
            result = prune_trajectory_root(root, max_age_days=0, max_total_mb=0)
            self.assertTrue(old.exists())
            self.assertEqual(result["removed"], 0)
            self.assertEqual(result["freed_mb"], 0.0)

    def test_age_prune_disabled_when_none(self):
        from backend.trajectory import prune_trajectory_root

        with tempfile.TemporaryDirectory(prefix="pcu-ret-agenone-") as tmp:
            root = Path(tmp)
            old = _make_run_dir(root, "20200101T000000_oldtask")
            _set_mtime(old, 365 * 86400)
            result = prune_trajectory_root(root, max_age_days=None, max_total_mb=0)
            self.assertTrue(old.exists())
            self.assertEqual(result["removed"], 0)


class RetentionSizeTests(unittest.TestCase):
    def test_size_cap_removes_oldest_first(self):
        from backend.trajectory import prune_trajectory_root

        with tempfile.TemporaryDirectory(prefix="pcu-ret-size-") as tmp:
            root = Path(tmp)
            # ~1 MB each; cap at 1.5 MB -> two oldest must go.
            oldest = _make_run_dir(
                root, "20200101T000000_a", size_bytes=1024 * 1024
            )
            middle = _make_run_dir(
                root, "20200102T000000_b", size_bytes=1024 * 1024
            )
            newest = _make_run_dir(
                root, "20200103T000000_c", size_bytes=1024 * 1024
            )
            # Distinct, deterministic mtimes (creation order is not enough).
            _set_mtime(oldest, 3000)
            _set_mtime(middle, 2000)
            _set_mtime(newest, 1000)
            result = prune_trajectory_root(root, max_age_days=0, max_total_mb=1.5)
            self.assertFalse(oldest.exists())
            self.assertFalse(middle.exists())
            self.assertTrue(newest.exists())
            self.assertEqual(result["removed"], 2)
            self.assertGreater(result["freed_mb"], 0.0)

    def test_size_cap_disabled_when_zero(self):
        from backend.trajectory import prune_trajectory_root

        with tempfile.TemporaryDirectory(prefix="pcu-ret-size0-") as tmp:
            root = Path(tmp)
            dirs = [
                _make_run_dir(root, f"2020010{i}T000000_t{i}", size_bytes=1024 * 1024)
                for i in range(1, 4)
            ]
            result = prune_trajectory_root(root, max_age_days=0, max_total_mb=0)
            for d in dirs:
                self.assertTrue(d.exists())
            self.assertEqual(result["removed"], 0)


class RetentionSkipTests(unittest.TestCase):
    def test_non_run_dir_entries_untouched(self):
        from backend.trajectory import prune_trajectory_root

        with tempfile.TemporaryDirectory(prefix="pcu-ret-skip-") as tmp:
            root = Path(tmp)
            loose_file = root / "notes.txt"
            loose_file.write_text("keep me", encoding="utf-8")
            other_dir = root / "not_a_run_dir"
            other_dir.mkdir()
            (other_dir / "data.bin").write_bytes(b"keep")
            weird = root / "99999999T999999_partial"
            weird.mkdir()
            _make_run_dir(root, "20200101T000000_old")
            old_run = root / "20200101T000000_old"
            _set_mtime(old_run, 30 * 86400)
            _set_mtime(other_dir, 30 * 86400)
            prune_trajectory_root(root, max_age_days=7, max_total_mb=0)
            self.assertTrue(loose_file.exists())
            self.assertTrue(other_dir.exists())
            self.assertTrue((other_dir / "data.bin").exists())
            self.assertTrue(weird.exists())
            self.assertFalse(old_run.exists())

    def test_missing_root_is_noop(self):
        from backend.trajectory import prune_trajectory_root

        with tempfile.TemporaryDirectory(prefix="pcu-ret-miss-") as tmp:
            missing = Path(tmp) / "nope"
            result = prune_trajectory_root(missing, max_age_days=7, max_total_mb=200)
            self.assertEqual(result, {"removed": 0, "freed_mb": 0.0})


class ProviderDebugPruneTests(unittest.TestCase):
    def test_provider_debug_age_pruned_and_dir_removed_when_empty(self):
        from backend.trajectory import prune_trajectory_root

        with tempfile.TemporaryDirectory(prefix="pcu-ret-dbg-") as tmp:
            root = Path(tmp)
            debug = root / "_provider_debug"
            debug.mkdir()
            old_file = debug / "raw_replies.jsonl"
            old_file.write_text("{}\n", encoding="utf-8")
            _set_mtime(old_file, 30 * 86400)
            keep = debug / "fresh.jsonl"
            keep.write_text("{}\n", encoding="utf-8")
            result = prune_trajectory_root(root, max_age_days=7, max_total_mb=0)
            self.assertFalse(old_file.exists())
            self.assertTrue(keep.exists())
            self.assertTrue(debug.exists())  # still has one file
            self.assertEqual(result["removed"], 1)

    def test_provider_debug_removed_when_empty_after_prune(self):
        from backend.trajectory import prune_trajectory_root

        with tempfile.TemporaryDirectory(prefix="pcu-ret-dbg2-") as tmp:
            root = Path(tmp)
            debug = root / "_provider_debug"
            debug.mkdir()
            old_file = debug / "raw_replies.jsonl"
            old_file.write_text("{}\n", encoding="utf-8")
            _set_mtime(old_file, 30 * 86400)
            prune_trajectory_root(root, max_age_days=7, max_total_mb=0)
            self.assertFalse(debug.exists())

    def test_provider_debug_untouched_when_age_disabled(self):
        from backend.trajectory import prune_trajectory_root

        with tempfile.TemporaryDirectory(prefix="pcu-ret-dbg3-") as tmp:
            root = Path(tmp)
            debug = root / "_provider_debug"
            debug.mkdir()
            old_file = debug / "raw_replies.jsonl"
            old_file.write_text("{}\n", encoding="utf-8")
            _set_mtime(old_file, 365 * 86400)
            prune_trajectory_root(root, max_age_days=0, max_total_mb=0)
            self.assertTrue(old_file.exists())
            self.assertTrue(debug.exists())


class ScreenshotModeTests(unittest.TestCase):
    def test_last_mode_overwrites_single_file(self):
        from backend.trajectory import TrajectoryRecorder

        with tempfile.TemporaryDirectory(prefix="pcu-shot-last-") as tmp:
            rec = TrajectoryRecorder(
                "t1", "instruction", root=tmp,
                save_screenshots=True, screenshot_mode="last",
            )
            rec.save_screenshot(b"png-one", 1)
            rec.save_screenshot(b"png-two", 2)
            rec.save_screenshot(b"png-three", 3)
            pngs = list(Path(tmp).glob("**/*.png"))
            self.assertEqual(len(pngs), 1)
            self.assertEqual(pngs[0].name, "last_step.png")
            self.assertEqual(pngs[0].read_bytes(), b"png-three")
            leftovers = list(Path(tmp).glob("**/*.tmp"))
            self.assertEqual(leftovers, [])

    def test_all_mode_keeps_per_step_files(self):
        from backend.trajectory import TrajectoryRecorder

        with tempfile.TemporaryDirectory(prefix="pcu-shot-all-") as tmp:
            rec = TrajectoryRecorder(
                "t1", "instruction", root=tmp,
                save_screenshots=True, screenshot_mode="all",
            )
            rec.save_screenshot(b"png-one", 1)
            rec.save_screenshot(b"png-two", 2)
            names = sorted(p.name for p in Path(tmp).glob("**/*.png"))
            self.assertEqual(names, ["step_01.png", "step_02.png"])

    def test_save_screenshots_false_takes_precedence(self):
        from backend.trajectory import TrajectoryRecorder

        with tempfile.TemporaryDirectory(prefix="pcu-shot-off-") as tmp:
            rec = TrajectoryRecorder(
                "t1", "instruction", root=tmp,
                save_screenshots=False, screenshot_mode="last",
            )
            rec.save_screenshot(b"png", 1)
            self.assertEqual(list(Path(tmp).glob("**/*")), [])

    def test_new_recorder_threads_mode(self):
        from backend.trajectory import new_recorder

        with tempfile.TemporaryDirectory(prefix="pcu-shot-new-") as tmp:
            rec = new_recorder(
                "t1", "instruction", root=tmp,
                save_screenshots=True, screenshot_mode="all",
            )
            self.assertEqual(rec.screenshot_mode, "all")
            rec2 = new_recorder(
                "t1", "instruction", root=tmp,
                save_screenshots=True, screenshot_mode="bogus",
            )
            # Unknown mode falls back to the lighter "last".
            self.assertEqual(rec2.screenshot_mode, "last")


class ConfigRetentionTests(unittest.TestCase):
    def test_defaults_present(self):
        from backend import config as config_mod

        self.assertFalse(config_mod.DEFAULTS["saveScreenshots"])
        self.assertEqual(config_mod.DEFAULTS["saveScreenshotsMode"], "last")
        self.assertEqual(config_mod.DEFAULTS["trajectoryRetentionDays"], 7)
        self.assertEqual(config_mod.DEFAULTS["trajectoryMaxTotalMb"], 200)

    def test_agent_loop_cfg_get_fallbacks(self):
        """TaskRunner reads retention + mode keys with safe fallbacks."""
        from backend.agent_loop import TaskRunner

        async def _noop_send(_message: dict) -> None:
            return None

        runner = TaskRunner("t1", "instruction", {"max_steps": 1}, _noop_send)
        self.assertEqual(runner._retention_days, 7.0)
        self.assertEqual(runner._retention_mb, 200.0)

        runner2 = TaskRunner(
            "t2", "instruction",
            {"max_steps": 1, "trajectoryRetentionDays": "junk",
             "trajectoryMaxTotalMb": None, "saveScreenshotsMode": "weird"},
            _noop_send,
        )
        self.assertEqual(runner2._retention_days, 7.0)
        self.assertEqual(runner2._retention_mb, 200.0)
        self.assertEqual(runner2._recorder.screenshot_mode, "last")


if __name__ == "__main__":
    unittest.main()
