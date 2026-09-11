# Personal Computer Use — Architecture

An LLM-driven computer-use agent (CUA) for Windows: a global hotkey summons a small
instruction bar, the user types what they want, and an agent loop (screenshot → model
returns actions → execute → screenshot again) gets it done on screen.

## Components

```
┌────────────────────────────┐         ws://127.0.0.1:8765        ┌──────────────────────────┐
│  Electron shell (Node)     │◄──────────────────────────────────►│  Python agent backend    │
│  - global hotkey           │        JSON messages (below)       │  - CUA loop              │
│  - tray icon               │                                    │  - provider adapters     │
│  - instruction bar window  │                                    │    (OpenAI / Anthropic / │
│  - status card window      │                                    │     any OpenAI-compat)   │
│  - settings window         │                                    │  - screen capture (mss)  │
│  - spawns/kills backend    │                                    │  - input control         │
└────────────────────────────┘                                    │    (SendInput/pyautogui) │
                                                                  │  - safety gate           │
                                                                  └──────────────────────────┘
```

## Layout

```
backend/
  main.py            # entry: starts WS server on 127.0.0.1:8765
  agent_loop.py      # CUA loop
  providers/         # openai.py, anthropic.py, openai_compat.py
  a11y.py            # UIA element grounding + typed-text verification
  screen.py          # capture, DPI, scaling
  control.py         # mouse/keyboard execution
  safety.py          # destructive-action gate
   config.py          # settings load/save (PCU_CONFIG_DIR/config.json; repo root in dev)
  doctor.py          # diagnostics checklist: python backend/doctor.py
  trajectory.py      # per-task recorder; writes trajectories/<ts>_<id>/ (screenshots,
                     #   actions.jsonl, events.jsonl, result.json) — gitignored
requirements.txt

electron/
  package.json
  main.js            # hotkey, tray, window mgmt, backend spawn, WS client
  preload.js
  renderer/          # instruction-bar, status-card, settings (plain HTML/JS)
  python-runtime/    # bundled Python 3.12 + backend deps (packaging only, gitignored)
  dist/              # electron-builder output (gitignored)

config.json          # created at runtime; stores api keys, model, hotkey
                     # dev: repo root; packaged: Electron userData dir
```

## Config & trajectory locations (env contract)

Electron passes these env vars to the spawned backend (dev and packaged):

- `PCU_CONFIG_DIR` — directory holding `config.json`. Always set: repo root in
  dev, Electron `%APPDATA%/<productName>` (userData) when packaged. Both
  electron/main.js and backend/config.py resolve config from it; when packaged
  and no config exists at the new location, both sides copy a legacy repo-root
  `config.json` over so saved API keys survive. Backend `save()` creates the
  directory if missing.
- `PCU_TRAJECTORY_DIR` — trajectory root for `trajectory.new_recorder`. Dev:
  `repo-root/trajectories`; packaged: `%USERPROFILE%\Documents\PCU\trajectories`.

Pacing: config key `action_delay_s` (seconds between executed actions, default
0.4, clamped 0–5) is honored by the agent loop after each action.


## WebSocket protocol (JSON, one object per message)

### Electron → Python
| type | fields | meaning |
|---|---|---|
| `start_task` | `id: str`, `instruction: str` | begin a CUA run |
| `stop_task` | | cancel current run |
| `confirm` | `id: str`, `approved: bool` | answer a pending safety gate |
| `get_status` | | request current status |

### Python → Electron
| type | fields | meaning |
|---|---|---|
| `status` | `state: "idle"\|"running"\|"awaiting_confirmation"\|"error"`, `message: str`, `step?: int`, `max_steps?: int` | general state updates |
| `log` | `line: str` | human-readable progress line for the status card |
| `action` | `kind: str`, `detail: str` | about to execute an action (click, type, …) |
| `need_confirmation` | `id: str`, `reason: str`, `detail: str` | safety gate; blocks until `confirm` |
| `task_done` | `id: str`, `success: bool`, `summary: str` | final result |

## Safety gate (MVP rules)

Auto-approved: left/double/right click, scroll, move, wait, screenshot, key
presses that are not on the blocklist.
Blocked → require confirmation: typing or pressing that matches
`delete|remove|format|pay|purchase|checkout|send|submit|transfer|password|confirm`,
and keys `alt+f4`, `ctrl+alt+delete`. The `win` key and `win+<key>` combos are
allowed (reversible Start-menu/search/run navigation; gating them hung routine
tasks). Hard cap: 25 steps per task.

## Coordinate contract

- Capture full virtual screen at physical pixels (process must set
  per-monitor-v2 DPI awareness **before** importing pyautogui).
- Screenshots are downscaled (max width 1280) before sending to the model.
- Models return coordinates in sent-image space; backend multiplies by
  `(physical_width / sent_width)` and `(physical_height / sent_height)`
  before moving the mouse.
- Virtual desktop coords handle multi-monitor (monitors left of primary
  have negative X).

### Element grounding (UIA)

Per step, in addition to the screenshot, the backend walks the foreground
window's UI Automation tree (`a11y.get_window_elements`, best-effort; None on
failure). Each element is `{id, role, name, value, enabled, center}` where
`center` is the physical-pixel midpoint of `BoundingRectangle` (None if the
rect is empty). The provider adapters receive the element list as
`a11y_context` with centers already converted to model-image space:
`mx = (cx - origin_x) / scale_x`, `my = (cy - origin_y) / scale_y`; elements
without a center or outside the model image are dropped.

- Action vocabulary addition: `{"kind": "click_element", "id": <int>}` —
  resolves the element by id in the current step's UI tree, moves the mouse
  to its stored physical center and left-clicks. The element's name+value
  text is passed through the safety gate, so clicking a control named e.g.
  "Delete File" is gated like typing "Delete". Unknown ids log
  "element id N not in current UI tree" and skip; they do not fail the task.
- Only `openai_compat` consumes `a11y_context` (elements are listed in the
  user message and the model is told to prefer `click_element` when a
  suitable element is present); openai/anthropic are strict pixel-space
  protocols and ignore it.

### Verify-after-action (typed text MVP)

After every `type` action, the backend reads the focused control
(`a11y.read_focused_value`) and then the foreground window's visible text
(`a11y.read_foreground_text`) and checks whether the typed text
(case-insensitive, trimmed) appears: logs "verified: text landed", otherwise
"warning: typed text not found in window" (readable focused control without
the text) or "note: window text unreadable, skipping typed-text verification"
(UIA text unavailable). Verification is observe-only and never retypes — a
blind retype doubled the text when UIA lagged (the Start search box got
'calculatorcalculator'); the model sees the next screenshot and self-corrects.
If a step's batch contains `done` but its `type` actions were not verified
("absent"/"unknown"), completion is held for one more step so the model sees
the post-typing screenshot ("holding completion for one more step: last typed
text was not verified"); this hold fires at most once per task, and a later
`done` finishes normally. All verification is wrapped in try/except and can
never break the loop.

## Test suite

`backend/tests/` holds 17 cases in three categories. **A planning-only** (no execution): imports screen/a11y/providers directly (DPI-before-import like main.py), captures one screenshot + a11y tree, calls `provider.run_step`, and only schema-validates the returned actions (kinds allowed, coordinate bounds, `click_element` ids present in the tree). **B gated-live**: real disk/window effects; the harness answers `need_confirmation` itself (auto-approve or auto-reject per case; reject cases must SKIP, e.g. recycle-bin emptying must leave the bin intact). **C autonomous**: safe/reversible, no gate expected (unexpected gates are auto-rejected and reported). Run with `python backend/tests/run_suite.py --list`, `--validate`, or `--category A|B|C [--only id1,id2] [--no-prompt] [--report path.json]`; B/C connect to an already-running backend on `ws://127.0.0.1:8765` and print a precondition prompt before cases that need manual setup. Results go to `backend/tests/report.json`.
