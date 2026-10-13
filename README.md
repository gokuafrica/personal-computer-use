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
- **Status card**: top-right card shows the current step, last action, and a log
  without taking focus from the app being controlled. Confirmation prompts are
  **off by default**. Enable "Ask before potentially sensitive actions" in
  Settings if desired. Rejecting an action stops the task instead of repeating
  the same request. Tasks default to 40 steps. Moving the mouse into a screen
  corner aborts at the next input check.
- **Stop button** cancels the task. Text entry checks for Stop between characters;
  a pointer move already in progress may finish, but its following click is cancelled.

## Providers

Configured in the settings window (tray icon → Settings), stored in `config.json`:

| provider | needs | default model |
|---|---|---|
| `codex` (default) | Sign in with an eligible ChatGPT subscription; no API key | `gpt-6-luna` |
| `openai` | API key | `computer-use-preview` |
| `anthropic` | API key | `claude-3-7-sonnet-latest` |
| `openai_compat` | base URL + API key + model | any vision model behind an OpenAI-compatible endpoint (OpenCode Go, OpenRouter, local servers) that returns strict JSON per the schema in `backend/providers/openai_compat.py` |

GPT-6 Luna defaults to **High** reasoning effort. Existing saved reasoning settings
are preserved on upgrade and can be changed in Settings.

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
5. First launch automatically opens ChatGPT sign-in if needed. Complete the official login in your browser; Settings updates when connected. Defaults are already saved. Existing Codex ChatGPT sign-in is reused. If sign-in is interrupted, click **Try again** in Settings. Other providers still accept API keys.
6. Press `Ctrl+Alt+K`, type an instruction, press Enter.

## Install & package

Build the distributable NSIS installer from `electron/`:

```
npm run dist
```

Output lands in `electron/dist/`: `Personal Computer Use Setup 1.1.4.exe`
(installer). The installer launches the app after installation. The build bundles a pinned Codex CLI 0.159.2 executable and a
private Python 3.12 runtime (`electron/python-runtime/`) with all backend deps —
the target machine needs no Python installed. When installed, config lives in
`%APPDATA%\personal-computer-use\config.json` (Electron userData) and task
trajectories in `%LOCALAPPDATA%\PCU\trajectories` (local app data, kept out of
OneDrive-synced Documents on purpose). In dev mode,
config stays at the repo root and trajectories in `trajectories/`.

### ChatGPT subscription

The `codex` provider calls the supported `codex exec` interface with image input
and a JSON action schema. PCU continues to execute actions through its existing
optional confirmation gates. Each model call uses an ephemeral session and temporary files that
are removed on completion or cancellation. API-key environment variables are
removed and ChatGPT authentication is required, so this provider cannot silently
fall back to API billing. Codex handles OAuth storage and refresh itself.

Each recipient signs in with their **own** eligible account. The installer has
no OpenCode seed key, ChatGPT password, OAuth token, or shared subscription.
Recipients complete browser sign-in once; the app starts that flow automatically
and reuses the saved session on future launches. Nothing in this build depends on
the developer's PC staying online.
Plus/Pro and eligible organizational accounts support the CLI; Free/Go Luna
access in the desktop app does not establish CLI eligibility. Model availability,
rollout, workspace policy, and subscription limits still apply.

Existing installations retain their selected provider: select **ChatGPT
subscription** in Settings to migrate. Old API credentials remain encrypted for
switching back. A fresh installation defaults to Luna. For development, install
the Electron dependencies and run `npm run prepare-codex` in `electron/`.

References: [OpenAI authentication](https://learn.chatgpt.com/docs/auth),
[models](https://learn.chatgpt.com/docs/models),
[subscription access](https://learn.chatgpt.com/docs/pricing),
[scriptable workflows](https://learn.chatgpt.com/docs/non-interactive-mode).

Validation commands (from the repo root, using a Python with requirements installed):

```
python backend/tests/test_codex_provider.py
python backend/tests/test_codex_live.py
python backend/tests/codex_desktop_e2e.py
python -m unittest backend.tests.test_runtime_audit
node electron/runtime-audit-test.mjs
```

The live tests consume subscription usage. The desktop test requires an unlocked
desktop and a blank foreground Notepad tab; it types only its test sentence,
never approves a safety gate, then checks stop during inference. Independently
verify the resulting text on screen. These smoke tests do not establish general
task reliability. Authentication on a new account still requires that user's
browser interaction.

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
- Optional confirmation prompts use keyword heuristics and are not a guarantee.
  With the default setting, tasks execute without these prompts. Stop, screen-corner
  failsafe, model/runtime restrictions and Windows privilege boundaries still apply.
- Screen content is treated as data; avoid leaving passwords/banking pages open
  during tasks.
