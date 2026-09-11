"""Runnable harness for the CUA test suite.

Usage:
  python backend/tests/run_suite.py --list
  python backend/tests/run_suite.py --validate
  python backend/tests/run_suite.py --category A
  python backend/tests/run_suite.py --category B --only B_desktop_file
  python backend/tests/run_suite.py --category C --no-prompt --report my_report.json

Category A never touches the desktop input pipeline: it imports screen/a11y/
providers directly (with the same DPI-before-import ordering as backend/main.py),
captures one real screenshot + a11y tree, calls provider.run_step, and checks
the returned actions for schema validity only (kinds allowed, coordinates in
bounds, click_element ids present in the tree). Actions are NEVER executed.

Categories B/C connect over WebSocket to an ALREADY-RUNNING backend
(ws://127.0.0.1:8765 per ARCHITECTURE.md), send start_task, answer
need_confirmation messages themselves according to the case's gate policy
(auto-approve or auto-reject), wait for task_done within the case timeout
(sending stop_task on timeout), then run the case's verification function.
Before cases with setup preconditions the harness prints the setup steps and
waits for Enter (human-in-the-loop), unless --no-prompt is given.

Results go to a JSON report (default backend/tests/report.json) plus a stdout
table. Exit code 0 = all pass, 1 = failures, 2 = bad usage/validation.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import re
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except Exception:
        pass

from backend.tests.test_cases import (  # noqa: E402
    CASES,
    VERIFICATIONS,
    VOCAB,
    TestCase,
    enum_window_titles,
    foreground_title,
    recycle_bin_count,
)

WS_URL = "ws://127.0.0.1:8765"
REPORT_PATH = _ROOT / "backend" / "tests" / "report.json"
COORD_KINDS = {"click", "double_click", "right_click", "move"}
DEFAULT_TIMEOUT_S = 300


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CUA test suite harness")
    parser.add_argument("--list", action="store_true", help="list all cases and exit")
    parser.add_argument("--validate", action="store_true",
                        help="schema-check the case file and exit")
    parser.add_argument("--category", choices=["A", "B", "C"], help="run one category")
    parser.add_argument("--only", help="comma-separated case ids to run")
    parser.add_argument("--report", default=str(REPORT_PATH), help="report JSON path")
    parser.add_argument("--ws", default=WS_URL, help="backend WebSocket URL for B/C")
    parser.add_argument("--no-prompt", action="store_true",
                        help="skip precondition Enter prompts (B/C)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                        help="default per-case timeout override (s)")
    return parser.parse_args(argv)


def _filter_cases(args: argparse.Namespace) -> list[TestCase]:
    cases = CASES
    if args.category:
        cases = [c for c in cases if c.category == args.category]
    if args.only:
        wanted = {part.strip() for part in args.only.split(",") if part.strip()}
        cases = [c for c in cases if c.id in wanted]
        missing = wanted - {c.id for c in cases}
        if missing:
            print(f"error: unknown case id(s): {sorted(missing)}", file=sys.stderr)
            raise SystemExit(2)
    return cases


def cmd_list(cases: list[TestCase]) -> int:
    print(f"{'id':<30} {'cat':<4} {'apps':<22} {'steps':>5} {'t/o':>5}  title")
    print("-" * 110)
    for case in cases:
        apps = ",".join(case.apps_needed) or "-"
        gate = case.gates.get("expect", "?")
        print(f"{case.id:<30} {case.category:<4} {apps:<22} {case.max_steps:>5} "
              f"{case.timeout_s:>5}  {case.title} [gates:{gate}]")
    print("-" * 110)
    counts = {cat: sum(1 for c in cases if c.category == cat) for cat in ("A", "B", "C")}
    print(f"total: {len(cases)} cases (A={counts['A']} planning, "
          f"B={counts['B']} gated-live, C={counts['C']} autonomous)")
    return 0


def cmd_validate(cases: list[TestCase]) -> int:
    errors: list[str] = []
    required = {"id", "title", "instruction", "category", "apps_needed",
                "preconditions", "verification", "gates", "max_steps", "notes"}
    seen_ids: set[str] = set()
    for case in cases:
        prefix = case.id
        data = {
            "id": case.id, "title": case.title, "instruction": case.instruction,
            "category": case.category, "apps_needed": case.apps_needed,
            "preconditions": case.preconditions, "verification": case.verification,
            "gates": case.gates, "max_steps": case.max_steps, "notes": case.notes,
        }
        missing = required - set(data)
        if missing:
            errors.append(f"{prefix}: missing fields {sorted(missing)}")
        if case.id in seen_ids:
            errors.append(f"{prefix}: duplicate id")
        seen_ids.add(case.id)
        if case.category not in ("A", "B", "C"):
            errors.append(f"{prefix}: bad category {case.category!r}")
        if not case.instruction.strip():
            errors.append(f"{prefix}: empty instruction")
        if case.max_steps <= 0 or case.max_steps > 40:
            errors.append(f"{prefix}: max_steps {case.max_steps} outside 1..40")
        if case.timeout_s <= 0:
            errors.append(f"{prefix}: timeout_s must be positive")
        if case.gates.get("policy") not in ("approve", "reject"):
            errors.append(f"{prefix}: gates.policy must be approve|reject")
        if case.gates.get("expect") not in ("required", "preferred", "none"):
            errors.append(f"{prefix}: gates.expect must be required|preferred|none")
        machine = case.verification.get("machine", "")
        if case.category in ("B", "C") and machine not in VERIFICATIONS:
            errors.append(f"{prefix}: unknown verification function {machine!r}")
        if case.category == "A":
            allowed = case.schema.get("allowed_kinds", [])
            bad = [k for k in allowed if k not in VOCAB]
            if bad:
                errors.append(f"{prefix}: allowed_kinds contains {bad}")
            if not allowed:
                errors.append(f"{prefix}: allowed_kinds empty")
    if errors:
        for error in errors:
            print(f"INVALID {error}")
        print(f"--validate: {len(errors)} problem(s)")
        return 1
    print(f"--validate: OK ({len(cases)} cases, all required fields, "
          f"verifiers and gate policies resolvable)")
    return 0


def validate_planning_step(
    actions: list[dict[str, Any]],
    case: TestCase,
    tree: dict[str, Any] | None,
    width: int,
    height: int,
    summary: str,
) -> list[str]:
    schema = case.schema
    allowed = set(schema.get("allowed_kinds", []))
    tree_ids = {element.get("id") for element in (tree or {}).get("elements", [])}
    errors: list[str] = []
    if schema.get("require_actions") and not actions:
        errors.append("model returned no actions")
    if schema.get("expect_summary") and not summary.strip():
        errors.append("model summary is empty")
    for action in actions:
        kind = str(action.get("kind", "")).lower()
        if kind not in VOCAB:
            errors.append(f"unknown action kind {kind!r}")
            continue
        if kind not in allowed:
            errors.append(f"kind {kind!r} outside case allowed_kinds {sorted(allowed)}")
        if kind in COORD_KINDS:
            x, y = action.get("x"), action.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                errors.append(f"{kind} has non-numeric coordinates: {x!r},{y!r}")
            elif not (0 <= x < width and 0 <= y < height):
                errors.append(f"{kind} coordinates ({x},{y}) outside image {width}x{height}")
        if kind == "click_element":
            element_id = action.get("id")
            if tree is not None and element_id not in tree_ids:
                errors.append(f"click_element id {element_id!r} not in provided a11y tree")
        if kind == "scroll":
            if str(action.get("direction", "down")) not in ("up", "down", "left", "right"):
                errors.append("scroll direction invalid")
            if int(action.get("amount") or 0) <= 0:
                errors.append("scroll amount must be > 0")
        if kind in ("type", "key") and not str(action.get("text") or action.get("key") or "").strip():
            errors.append(f"{kind} action has empty payload")
        if kind in ("done", "fail") and not str(action.get("summary") or "").strip():
            errors.append(f"{kind} action lacks a summary")
    return errors


def run_planning_case(case: TestCase) -> dict[str, Any]:
    """Category A: capture + provider step in-process; actions only validated."""
    import backend.a11y as a11y
    import backend.screen as screen
    from backend import config as config_mod
    from backend.providers.openai_compat import OpenAICompatProvider

    cfg = dict(config_mod.load())
    cfg["provider"] = "openai_compat"
    provider = OpenAICompatProvider(cfg)
    img = screen.capture()
    model_img, scale_x, scale_y = screen.to_model_image(img)
    screenshot_b64 = screen.to_base64_png(model_img)
    origin_x, origin_y = screen.origin()
    try:
        a11y_raw = a11y.get_window_elements()
    except Exception:
        a11y_raw = None
    tree = a11y.build_model_context(a11y_raw, origin_x, origin_y, scale_x, scale_y)
    started = time.monotonic()
    try:
        result = asyncio.run(provider.run_step(
            screenshot_b64, model_img.width, model_img.height,
            case.instruction, None, a11y_context=tree,
        ))
    except Exception as exc:
        return {"id": case.id, "category": case.category, "ok": False,
                "outcome": "FAIL", "detail": f"provider.run_step raised: {exc}"}
    errors = validate_planning_step(
        result.actions, case, tree, model_img.width, model_img.height, result.summary
    )
    detail = (f"actions={[a.get('kind') for a in result.actions]} "
              f"done={result.done} errors={errors or 'none'}")
    return {"id": case.id, "category": case.category, "ok": not errors,
            "outcome": "PASS" if not errors else "FAIL", "detail": detail,
            "duration_s": round(time.monotonic() - started, 1)}


def _snapshot() -> dict[str, Any]:
    return {
        "foreground_title": foreground_title(),
        "window_titles": enum_window_titles(),
        "recycle_bin_count": recycle_bin_count(),
        "taken_at": datetime.now(timezone.utc).isoformat(),
    }


async def _live_session(case: TestCase, ws_url: str) -> dict[str, Any]:
    import websockets

    logs: list[str] = []
    actions: list[dict[str, Any]] = []
    gates: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    task_done: dict[str, Any] | None = None
    approve = case.gates.get("policy", "reject") == "approve"
    deadline = time.monotonic() + case.timeout_s
    async with websockets.connect(ws_url, max_size=2 ** 22) as ws:
        await ws.send(json.dumps({
            "type": "start_task", "id": case.id, "instruction": case.instruction,
        }))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            try:
                message = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            mtype = message.get("type")
            if mtype == "log":
                logs.append(str(message.get("line", "")))
            elif mtype == "action":
                actions.append({"kind": message.get("kind"), "detail": message.get("detail")})
            elif mtype == "status":
                statuses.append(message)
            elif mtype == "need_confirmation":
                gates.append(message)
                print(f"    [gate] {message.get('detail', '')!r} -> "
                      f"{'APPROVE' if approve else 'REJECT'}")
                await ws.send(json.dumps({
                    "type": "confirm", "id": message.get("id", ""), "approved": approve,
                }))
            elif mtype == "task_done":
                if str(message.get("id", "")) == case.id:
                    task_done = message
                    break
        if task_done is None:
            print("    timeout reached; sending stop_task")
            try:
                await ws.send(json.dumps({"type": "stop_task"}))
            except Exception:
                pass
            grace_deadline = time.monotonic() + 10.0
            while time.monotonic() < grace_deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    break
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if message.get("type") == "task_done" and \
                        str(message.get("id", "")) == case.id:
                    task_done = message
                    break
                if message.get("type") == "log":
                    logs.append(str(message.get("line", "")))
    return {"logs": logs, "actions": actions, "gates": gates,
            "statuses": statuses, "task_done": task_done}


def _gate_check(case: TestCase, gates: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    expect = case.gates.get("expect", "none")
    occurred = bool(gates)
    if expect == "required" and not occurred:
        errors.append("expected safety gate never triggered")
    if expect == "none" and occurred:
        errors.append("unexpected safety gate triggered")
    pattern = case.gates.get("detail_regex", "")
    if occurred and pattern:
        compiled = re.compile(pattern, re.IGNORECASE)
        if not any(compiled.search(str(g.get("detail", ""))) for g in gates):
            errors.append(f"gate detail did not match expected pattern {pattern!r}")
    return errors


def run_live_case(case: TestCase, args: argparse.Namespace) -> dict[str, Any]:
    """Categories B/C: drive a running backend over WebSocket, then verify."""
    print(f"  preconditions: {case.preconditions}")
    if case.setup_prompt and not args.no_prompt:
        input("  Set up the precondition above, then press Enter to start... ")
    else:
        print("  (continuing without pause)")
    before = _snapshot()
    started = time.monotonic()
    try:
        session = asyncio.run(_live_session(case, args.ws))
    except Exception as exc:
        return {"id": case.id, "category": case.category, "ok": False,
                "outcome": "FAIL",
                "detail": f"could not connect/run against {args.ws}: {exc}"}
    duration = round(time.monotonic() - started, 1)
    errors = _gate_check(case, session["gates"])
    task_done = session["task_done"]
    if task_done is None:
        errors.append(f"no task_done within {case.timeout_s}s (backend hung?)")
    elif case.expect_task_success is not None and \
            bool(task_done.get("success")) != case.expect_task_success:
        errors.append(
            f"task_done success={task_done.get('success')}, expected "
            f"{case.expect_task_success}")
    ctx: dict[str, Any] = {
        "case": case, "task_done": task_done, "logs": session["logs"],
        "actions": session["actions"], "gates": session["gates"],
        "before": before, "statuses": session["statuses"],
    }
    verify = VERIFICATIONS[case.verification["machine"]]
    try:
        verify_ok, verify_detail = verify(ctx)
    except Exception as exc:
        verify_ok, verify_detail = False, f"verification function raised: {exc}"
    if not verify_ok:
        errors.append(verify_detail)
    outcome = "PASS" if not errors else "FAIL"
    return {
        "id": case.id, "category": case.category, "ok": not errors,
        "outcome": outcome,
        "detail": errors or [verify_detail],
        "verify_detail": verify_detail,
        "gates": [str(g.get("detail", "")) for g in session["gates"]],
        "task_done": task_done,
        "num_actions": len(session["actions"]),
        "duration_s": duration,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    cases = _filter_cases(args)
    if args.timeout != DEFAULT_TIMEOUT_S:
        cases = [replace(case, timeout_s=args.timeout) for case in cases]
    if args.list:
        return cmd_list(cases if cases else CASES)
    if args.validate:
        return cmd_validate(cases if cases else CASES)
    if not cases:
        print("nothing to run: pass --category and/or --only", file=sys.stderr)
        return 2
    if args.category is None and any(c.category != "A" for c in cases):
        print("error: live categories B/C need --category to be explicit", file=sys.stderr)
        return 2
    results: list[dict[str, Any]] = []
    for case in cases:
        print(f"[{case.id}] running ({case.category}, max_steps={case.max_steps})")
        if case.category == "A":
            results.append(run_planning_case(case))
        else:
            results.append(run_live_case(case, args))
        print(f"[{case.id}] {results[-1]['outcome']}")
    report = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "passed": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
    }
    report_path = Path(args.report)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print()
    print(f"{'id':<30} {'cat':<4} {'result':<7} note")
    print("-" * 110)
    for result in results:
        note = "; ".join(result["detail"]) if isinstance(result["detail"], list) \
            else str(result["detail"])
        note = note if len(note) <= 74 else note[:71] + "..."
        print(f"{result['id']:<30} {result['category']:<4} {result['outcome']:<7} {note}")
    print("-" * 110)
    print(f"report: {report_path}  |  pass {report['passed']} / fail {report['failed']}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
