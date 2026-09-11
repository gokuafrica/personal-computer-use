# Personal Computer Use

A computer-use agent (CUA) for Windows. Press a global hotkey, type what you want
done, and an LLM looks at the screen and moves the mouse and keyboard to get it done —
like the Codex computer-use skill, but standalone and provider-agnostic.

Built for people (e.g., parents) who find day-to-day computer operation fiddly.

## How it works

```
Global hotkey → instruction bar → "open Chrome and go to youtube"
                                    ↓
   Electron shell  ⇄  Python backend (loop): screenshot → model → act → screenshot…
                                    ↓
                            status card (live progress, Stop, safety approvals)
```

- **Hotkey**: default `Ctrl+Alt+K` (configurable). Note: `Win+K` is reserved by
  Windows for Cast and will not register.
- **Status card**: bottom-right card shows the current step, last action, and a log.
  Safe actions run automatically; potentially destructive ones (typing/pressing
  anything matching delete/pay/send/etc., `Alt+F4`, `Ctrl+Alt+Del`, `Win`) pop an
  Approve/Reject prompt. Tasks cap at 40 steps. Mouse into a screen corner aborts.
- **Stop button** kills the task instantly.

## Providers

Configured in the settings window (tray icon → Settings), stored in `config.json`:

| provider | needs | default model |
|---|---|---|
| `openai` | API key | `computer-use-preview` |
| `anthropic` | API key | `claude-3-7-sonnet-latest` |
| `openai_compat` | base URL + API key + model | any vision model behind an OpenAI-compatible endpoint (OpenCode Go, OpenRouter, local servers) that returns strict JSON per the schema in `backend/providers/openai_compat.py` |

## Setup

1. Install Python 3.11+ and Node.js 18+.
2. Backend deps:
   ```
   pip install -r requirements.txt
   ```
3. Shell deps:
   ```
   cd electron && npm install
   ```
4. Run:
   ```
   npm start          # from repo root
   ```
5. First run: tray icon → Settings → pick provider, paste API key → Save.
6. Press `Ctrl+Alt+K`, type an instruction, press Enter.

## Install & package

Build the distributable (NSIS installer + portable exe) from `electron/`:

```
npm run dist
```

Output lands in `electron/dist/`: `Personal Computer Use Setup 1.0.0.exe`
(installer) and `Personal Computer Use 1.0.0.exe` (portable). The build bundles a
private Python 3.12 runtime (`electron/python-runtime/`) with all backend deps —
the target machine needs no Python installed. When installed, config lives in
`%APPDATA%\personal-computer-use\config.json` (Electron userData) and task
trajectories in `%USERPROFILE%\Documents\PCU\trajectories`. In dev mode,
config stays at the repo root and trajectories in `trajectories/`.

## Diagnostics & debugging

- `python backend/doctor.py` — full health checklist (deps, DPI, screen capture,
  port, config, provider connectivity). Run this first when something misbehaves.
- Every task writes a trajectory folder to `trajectories/` (per-step screenshots,
  actions, events, result) for post-mortems. Delete old folders freely.

## Project layout

See [ARCHITECTURE.md](ARCHITECTURE.md) for the component diagram, WebSocket
protocol, safety gate rules, and coordinate-scaling contract.

```
backend/       Python: CUA loop, provider adapters, capture, input, safety
electron/      Electron shell: hotkey, tray, instruction bar, status card, settings
config.json    created at runtime (provider, API keys, model, hotkey) — gitignore it
```

## Safety notes

- Only grant API keys you trust to an app that can see your screen and click around.
- The safety gate is heuristic (keyword-based), not a guarantee. Review actions on
  the status card before approving.
- Screen content is treated as data; avoid leaving passwords/banking pages open
  during tasks.
