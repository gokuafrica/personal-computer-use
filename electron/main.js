'use strict';

const {
  app,
  BrowserWindow,
  Tray,
  Menu,
  Notification,
  globalShortcut,
  ipcMain,
  nativeImage,
  screen
} = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const WebSocket = require('ws');
const { createSecretsFilter } = require('./secrets-filter');
const { trustedRendererPath } = require('./ipc-trust');

const ROOT = path.join(__dirname, '..');
const BACKEND_URL = 'ws://127.0.0.1:8765';

// Testability hook: PCU_CONFIG_DIR redirects the packaged app's config dir
// (and Electron userData) away from %APPDATA%\<productName>. Used only by
// isolated build validation; unset in normal use, so behavior is unchanged.
const PCU_CONFIG_DIR_OVERRIDE = (process.env.PCU_CONFIG_DIR || '').trim();
if (PCU_CONFIG_DIR_OVERRIDE) {
  app.setPath('userData', PCU_CONFIG_DIR_OVERRIDE);
}
// Testability hook: PCU_AUTO_QUIT_MS makes the packaged app call app.quit()
// after N ms so isolated build validation exercises the REAL quit path
// (before-quit: killBackend + deleteRuntimeToken) without UI interaction.
// Unset in normal use; no behavior change in production.
const PCU_AUTO_QUIT_MS = parseInt(process.env.PCU_AUTO_QUIT_MS || '', 10) || 0;

const DEFAULT_CONFIG = {
  provider: 'openai',
  openai: { model: 'computer-use-preview' },
  anthropic: { model: 'claude-3-7-sonnet-latest' },
  openai_compat: { base_url: '', model: '' },
  apiKeyEncrypted: null,
  keySource: 'none',
  keyVersion: 0,
  hotkey: 'Control+Alt+K',
  max_steps: 40,
  action_delay_s: 0.4,
  pointer_glide_s: 0.45,
  cursor_overlay: true
};

// ---------- config location ----------
// Packaged: %APPDATA%/<productName> (Electron userData). Dev: repo root.
// PCU_CONFIG_DIR wins over both (isolated build-validation hook).
function configDirPath() {
  if (PCU_CONFIG_DIR_OVERRIDE) return PCU_CONFIG_DIR_OVERRIDE;
  if (app.isPackaged) return app.getPath('userData');
  return ROOT;
}

function configPath() {
  const dir = configDirPath();
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, 'config.json');
}

// ---------- secret provisioning ----------
// Packaged apps ship NO seed: the NSIS installer provisions the bundled key
// into the config dir as the DPAPI provision.json envelope (consumed by the
// backend on first config load, then deleted). Dev (non-packaged) mode has a
// repo seed at electron/default-config.json, which never enters app.asar.
//
// Electron itself NEVER persists key material anymore: safeStorage in this
// Electron version emits Chromium OSCrypt v10/AES-GCM blobs (key in Local
// State) that the backend's raw-DPAPI secret_store cannot decrypt. Every
// credential write goes through the backend over the token-authenticated
// WebSocket (rotate_key / restore_bundled); Electron only ever reads the
// sanitized config for the settings summary and never writes key fields.
const SECTIONS = ['openai', 'anthropic', 'openai_compat'];

function extractLegacyKey(cfg) {
  if (!cfg || typeof cfg !== 'object') return null;
  if (typeof cfg.apiKey === 'string' && cfg.apiKey.trim() !== '') return cfg.apiKey.trim();
  for (const section of SECTIONS) {
    const sub = cfg[section];
    if (sub && typeof sub === 'object' &&
        typeof sub.api_key === 'string' && sub.api_key.trim() !== '') return sub.api_key.trim();
  }
  return null;
}

function stripPlaintextKeys(cfg) {
  if (!cfg || typeof cfg !== 'object') return;
  delete cfg.apiKey;
  for (const section of SECTIONS) {
    const sub = cfg[section];
    if (sub && typeof sub === 'object') delete sub.api_key;
  }
}

// Secrets that must never survive into forwarded logs; rebuilt whenever a new
// value becomes known to the main process only.
const knownSecrets = new Set();
let redact = createSecretsFilter([]);

function addKnownSecret(value) {
  if (typeof value !== 'string' || value.length < 8) return;
  if (knownSecrets.has(value)) return;
  knownSecrets.add(value);
  redact = createSecretsFilter([...knownSecrets]);
}

// Per-launch WebSocket token: the backend writes runtime_token (user-only ACL)
// into the config dir at startup and deletes it at shutdown; the app deletes a
// stale copy on quit as a safety net.
function runtimeTokenPath() {
  return path.join(configDirPath(), 'runtime_token');
}

function readRuntimeToken() {
  try {
    const token = fs.readFileSync(runtimeTokenPath(), 'utf8').trim();
    if (token) addKnownSecret(token);
    return token || null;
  } catch {
    return null;
  }
}

function deleteRuntimeToken() {
  try {
    fs.rmSync(runtimeTokenPath(), { force: true });
  } catch {}
}

// Electron never persists credentials: the backend is the single owner of
// the DPAPI-protected store (config.json apiKeyEncrypted / bundledKeyEncrypted).
// Key writes happen exclusively via the WS key ops (rotate_key / restore_bundled)
// handled below; there is no local write fallback in any mode (fail closed).
function provisionConfig() {
  const cfgPath = configPath();
  let raw = null;
  try {
    raw = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
  } catch {
    raw = null;
  }
  if (!raw || typeof raw !== 'object') {
    // Fresh config: defaults only, no key fields. Legacy plaintext keys that
    // may exist in a pre-existing config are left for the backend to migrate
    // with its interoperable raw-DPAPI form on its first config load.
    writeConfig(structuredClone(DEFAULT_CONFIG));
    console.log('[config] provisioned user config at', cfgPath);
  }
}

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 5000;

let config = { ...DEFAULT_CONFIG };
let tray = null;
let barWin = null;
let statusWin = null;
let settingsWin = null;
let overlayWin = null;
let overlayHideTimer = null;
let overlayPollTimer = null;
let overlayBoundsCache = null;
let backendProc = null;
let backendRestarted = false;
let backendExitNotified = false;
let registeredHotkey = null;
let ws = null;
let wsUp = false;
let wsRetryTimer = null;
let wsAttempt = 0;
let hideStatusTimer = null;
let quitting = false;

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
}

function readConfig() {
  const cfgPath = configPath();
  try {
    const raw = fs.readFileSync(cfgPath, 'utf8');
    const parsed = JSON.parse(raw);
    config = deepMerge(structuredClone(DEFAULT_CONFIG), parsed);
  } catch {
    config = structuredClone(DEFAULT_CONFIG);
    writeConfig(config);
  }
}

function deepMerge(base, extra) {
  for (const key of Object.keys(extra)) {
    const val = extra[key];
    if (val && typeof val === 'object' && !Array.isArray(val) &&
        base[key] && typeof base[key] === 'object' && !Array.isArray(base[key])) {
      deepMerge(base[key], val);
    } else {
      base[key] = val;
    }
  }
  return base;
}

function writeConfig(cfg) {
  const dir = configDirPath();
  const out = structuredClone(cfg);
  // Fail closed: plaintext key fields are NEVER persisted, whatever the state
  // of the backend. Credential blobs (apiKeyEncrypted / bundledKeyEncrypted)
  // are preserved byte-for-byte: only the backend produces them.
  stripPlaintextKeys(out);
  delete out.apiKey;
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify(out, null, 2), 'utf8');
}

function showNotification(title, body) {
  const n = new Notification({ title, body, silent: false });
  n.show();
}

// ---------- backend process ----------

function backendEnv() {
  const env = { ...process.env, PCU_CONFIG_DIR: configDirPath() };
  // An explicitly provided PCU_TRAJECTORY_DIR wins (isolated validation hook).
  // Packaged default is local app data (not Documents, which may be OneDrive-synced
  // and would upload screenshots to cloud): %LOCALAPPDATA%\PCU\trajectories.
  env.PCU_TRAJECTORY_DIR = process.env.PCU_TRAJECTORY_DIR || (app.isPackaged
    ? path.join(app.getPath('home'), 'AppData', 'Local', 'PCU', 'trajectories')
    : path.join(ROOT, 'trajectories'));
  return env;
}

function spawnBackend() {
  if (quitting) return;
  backendExitNotified = false;
  const env = backendEnv();
  if (app.isPackaged) {
    const resources = process.resourcesPath;
    const py = path.join(resources, 'python-runtime', 'python.exe');
    const script = path.join(resources, 'backend', 'main.py');
    backendProc = spawn(py, [script], {
      cwd: resources,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true
    });
    if (!backendProc || backendProc.pid === undefined) {
      showNotification('CUA backend', 'Could not start bundled Python backend.');
      return;
    }
    backendProc.on('error', () => {
      showNotification('CUA backend', 'Could not start bundled Python backend.');
    });
    pipeBackend(backendProc);
    backendProc.on('exit', onBackendExit);
    return;
  }
  const run = (cmd) => {
    const proc = spawn(cmd, ['backend/main.py'], {
      cwd: ROOT,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true
    });
    return proc;
  };
  try {
    backendProc = run('python');
  } catch {
    try {
      backendProc = run('py');
    } catch (err) {
      showNotification('CUA backend', 'Could not start Python backend: ' + err.message);
      return;
    }
  }
  // 'python' may resolve as the Store alias which exits immediately with
  // code 9009/1 without throwing; fall back to 'py' in that case.
  backendProc.on('error', () => {
    if (backendProc && backendProc.spawnargs[0] === 'python') {
      try {
        backendProc = run('py');
        pipeBackend(backendProc);
      } catch (err) {
        showNotification('CUA backend', 'Could not start Python backend: ' + err.message);
      }
      return;
    }
    showNotification('CUA backend', 'Could not start Python backend.');
  });
  pipeBackend(backendProc);
  backendProc.on('exit', onBackendExit);
}

function pipeBackend(proc) {
  const line = (buf) => buf.toString().trimEnd().split(/\r?\n/).forEach((l) => {
    if (l) console.log('[backend]', redact(l));
  });
  proc.stdout.on('data', line);
  proc.stderr.on('data', line);
}

function onBackendExit(code) {
  console.log('[backend] exited with code', code);
  hideOverlay();
  forceRestoreCursor();
  if (quitting) return;
  if (!backendRestarted) {
    backendRestarted = true;
    setTimeout(() => {
      console.log('[backend] restarting once');
      spawnBackend();
    }, 3000);
    return;
  }
  if (!backendExitNotified) {
    backendExitNotified = true;
    showNotification('CUA backend stopped', 'The Python backend exited and will not be restarted.');
    if (statusWin && !statusWin.isDestroyed()) {
      statusWin.webContents.send('backend-connection', { connected: false, reason: 'backend-exited' });
    }
  }
}

function killBackend() {
  if (backendProc && backendProc.exitCode === null) {
    try {
      if (process.platform === 'win32') {
        spawn('taskkill', ['/pid', String(backendProc.pid), '/T', '/F'], { windowsHide: true });
      } else {
        backendProc.kill();
      }
    } catch {}
  }
  backendProc = null;
}

// Safety net: if the backend died without restoring the native cursors it hid
// for the task, force-restore them via SPI_SETCURSORS. Cheap and idempotent.
function forceRestoreCursor() {
  if (process.platform !== 'win32') return;
  const py = app.isPackaged
    ? path.join(process.resourcesPath, 'python-runtime', 'python.exe')
    : 'python';
  const script = app.isPackaged
    ? path.join(process.resourcesPath, 'backend', 'cursor_restore.py')
    : path.join(ROOT, 'backend', 'cursor_restore.py');
  try {
    const proc = spawn(py, [script], { windowsHide: true, stdio: 'ignore' });
    if (!app.isPackaged) {
      proc.on('error', () => {
        try {
          spawn('py', [script], { windowsHide: true, stdio: 'ignore' });
        } catch {}
      });
    }
  } catch {}
}

// ---------- websocket client ----------

function connectWs() {
  if (quitting) return;
  clearTimeout(wsRetryTimer);
  // Per-launch token is sent as an upgrade header, never in the URL and never
  // the API key itself. Header name must match TOKEN_HEADER in backend/main.py.
  const headers = {};
  const token = readRuntimeToken();
  if (token) headers['X-PCU-Token'] = token;
  try {
    ws = new WebSocket(BACKEND_URL, { headers });
  } catch {
    scheduleReconnect();
    return;
  }
  ws.on('open', () => {
    wsUp = true;
    wsAttempt = 0;
    console.log('[ws] connected');
    sendWs({ type: 'get_status' });
    broadcastConnection(true);
  });
  ws.on('message', (data) => {
    let msg;
    try {
      msg = JSON.parse(data.toString());
    } catch {
      return;
    }
    handleBackendMessage(msg);
  });
  ws.on('close', () => {
    wsUp = false;
    // A pending key op can no longer be answered; fail it closed so the
    // renderer is not left waiting until the timeout.
    resolveKeyOp({
      ok: false,
      error: 'The key store connection was lost; the key was NOT changed. ' +
        'Your existing key (if any) still works.'
    });
    broadcastConnection(false);
    scheduleReconnect();
  });
  ws.on('error', () => {
    wsUp = false;
    // close handler drives reconnection
  });
}

function scheduleReconnect() {
  clearTimeout(wsRetryTimer);
  const delay = Math.min(RECONNECT_BASE_MS * Math.pow(2, wsAttempt), RECONNECT_MAX_MS);
  wsAttempt += 1;
  wsRetryTimer = setTimeout(connectWs, delay);
}

function sendWs(obj) {
  if (wsUp && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
    return true;
  }
  return false;
}

// ---------- WS key operations (rotate_key / restore_bundled) ----------
// All persisted credentials live in ONE DPAPI-protected store owned by the
// backend (config.json apiKeyEncrypted + bundledKeyEncrypted backup of the
// last-known bundled key). Electron never writes key material itself: these
// ops are sent over the existing token-authenticated WebSocket and the
// backend replies with a structured {"type":"key_op_result","op":...}
// message. One outstanding op at a time (UI-driven, serialized).
let keyOpWaiter = null;

function resolveKeyOp(result) {
  if (!keyOpWaiter) return;
  clearTimeout(keyOpWaiter.timer);
  const waiter = keyOpWaiter;
  keyOpWaiter = null;
  waiter.resolve(result);
}

function sendKeyOpAwait(payload, timeoutMs = 8000) {
  return new Promise((resolve) => {
    if (!wsUp || !ws || ws.readyState !== WebSocket.OPEN) {
      resolve({
        ok: false,
        error: 'The key store is not connected right now, so the key was NOT changed. ' +
          'Your existing key (if any) still works. Try again in a moment.'
      });
      return;
    }
    keyOpWaiter = { op: payload.type, resolve, timer: null };
    keyOpWaiter.timer = setTimeout(() => {
      resolveKeyOp({
        ok: false,
        error: 'The key store did not respond in time; the key was NOT changed. ' +
          'Your existing key (if any) still works.'
      });
    }, timeoutMs);
    if (!sendWs(payload)) {
      resolveKeyOp({
        ok: false,
        error: 'The key store is not connected right now, so the key was NOT changed. ' +
          'Your existing key (if any) still works.'
      });
    }
  });
}

function broadcastConnection(connected) {
  for (const win of [statusWin, barWin]) {
    if (win && !win.isDestroyed()) {
      win.webContents.send('backend-connection', { connected });
    }
  }
  if (!connected) hideOverlay();
}

// Backend action coords are physical screen pixels; the overlay works in DIP.
function convertActionPoint(msg) {
  if (!Number.isFinite(msg.x) || !Number.isFinite(msg.y)) return;
  let dip = { x: msg.x, y: msg.y };
  try {
    if (typeof screen.screenToDIPPoint === 'function') {
      dip = screen.screenToDIPPoint({ x: msg.x, y: msg.y });
    } else {
      const scale = screen.getPrimaryDisplay().scaleFactor || 1;
      dip = { x: msg.x / scale, y: msg.y / scale };
    }
  } catch {}
  const bounds = overlayBoundsCache || overlayBounds();
  msg.x = dip.x - bounds.x;
  msg.y = dip.y - bounds.y;
}

function handleBackendMessage(msg) {
  if (!msg || typeof msg.type !== 'string') return;
  if (msg.type === 'action') convertActionPoint(msg);
  if (msg.type === 'key_op_result') resolveKeyOp(msg);
  if (statusWin && !statusWin.isDestroyed()) {
    statusWin.webContents.send('backend-message', msg);
  }
  if (overlayWin && !overlayWin.isDestroyed()) {
    overlayWin.webContents.send('backend-message', msg);
  }
  switch (msg.type) {
    case 'need_confirmation':
      showStatusCard();
      showOverlay();
      break;
    case 'status':
      if (msg.state === 'running' || msg.state === 'awaiting_confirmation') {
        showStatusCard();
        showOverlay();
      } else if (msg.state === 'idle' || msg.state === 'error') {
        hideOverlay();
      }
      break;
    case 'task_done':
      scheduleStatusHide();
      hideOverlay();
      break;
  }
}

// ---------- status card visibility ----------

function scheduleStatusHide() {
  clearTimeout(hideStatusTimer);
  hideStatusTimer = setTimeout(() => {
    if (statusWin && !statusWin.isDestroyed() && !statusWin.webContents.isLoading()) {
      statusWin.hide();
    }
  }, 8000);
}

function createStatusWindow() {
  statusWin = new BrowserWindow({
    width: 420,
    height: 340,
    show: false,
    frame: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  statusWin.setAlwaysOnTop(true, 'screen-saver');
  positionStatusCard();
  statusWin.loadFile(path.join(__dirname, 'renderer', 'status-card.html'));
  statusWin.on('close', (e) => {
    if (!quitting) e.preventDefault();
  });
}

function positionStatusCard() {
  if (!statusWin) return;
  const { workArea } = screen.getPrimaryDisplay();
  statusWin.setPosition(
    workArea.x + workArea.width - 420 - 16,
    workArea.y + workArea.height - 340 - 16
  );
}

function showStatusCard() {
  clearTimeout(hideStatusTimer);
  if (!statusWin || statusWin.isDestroyed()) createStatusWindow();
  positionStatusCard();
  statusWin.show();
}

// ---------- instruction bar ----------

function createBarWindow() {
  barWin = new BrowserWindow({
    width: 720,
    height: 110,
    show: false,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  barWin.setAlwaysOnTop(true, 'screen-saver');
  barWin.setVisibleOnAllWorkspaces(true);
  barWin.loadFile(path.join(__dirname, 'renderer', 'instruction-bar.html'));
  barWin.on('blur', () => {
    if (barWin.isVisible()) barWin.hide();
  });
}

function showBar() {
  if (!barWin || barWin.isDestroyed()) createBarWindow();
  const cursor = screen.getCursorScreenPoint();
  const display = screen.getDisplayNearestPoint(cursor);
  const wa = display.workArea;
  const [w] = barWin.getSize();
  barWin.setPosition(Math.round(wa.x + (wa.width - w) / 2), wa.y + 80);
  barWin.show();
  barWin.moveTop();
  app.focus({ steal: true });
  barWin.focus();
  barWin.webContents.send('bar-shown');
}

function toggleBar() {
  if (barWin && !barWin.isDestroyed() && barWin.isVisible()) {
    barWin.hide();
  } else {
    showBar();
  }
}

// ---------- agent cursor overlay ----------

// Fullscreen transparent click-through overlay: fancy agent cursor, edge glow,
// per-action effects. Visible only while a task runs; never takes input.
function overlayBounds() {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const display of screen.getAllDisplays()) {
    minX = Math.min(minX, display.bounds.x);
    minY = Math.min(minY, display.bounds.y);
    maxX = Math.max(maxX, display.bounds.x + display.bounds.width);
    maxY = Math.max(maxY, display.bounds.y + display.bounds.height);
  }
  return { x: minX, y: minY, width: maxX - minX, height: maxY - minY };
}

function createOverlayWindow() {
  if (overlayWin && !overlayWin.isDestroyed()) return;
  const bounds = overlayBounds();
  overlayWin = new BrowserWindow({
    x: bounds.x,
    y: bounds.y,
    width: bounds.width,
    height: bounds.height,
    show: false,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    movable: false,
    focusable: false,
    hasShadow: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  overlayWin.setAlwaysOnTop(true, 'screen-saver');
  overlayWin.setIgnoreMouseEvents(true);
  overlayWin.loadFile(path.join(__dirname, 'renderer', 'agent-overlay.html'));
  overlayWin.on('close', (e) => {
    if (!quitting) e.preventDefault();
  });
}

function startOverlayPoll() {
  if (overlayPollTimer) return;
  overlayPollTimer = setInterval(() => {
    if (!overlayWin || overlayWin.isDestroyed() || !overlayWin.isVisible()) return;
    const pt = screen.getCursorScreenPoint();
    const bounds = overlayBoundsCache || overlayBounds();
    overlayWin.webContents.send('overlay-cursor', { x: pt.x - bounds.x, y: pt.y - bounds.y });
  }, 16);
}

function stopOverlayPoll() {
  clearInterval(overlayPollTimer);
  overlayPollTimer = null;
}

function showOverlay() {
  if (config.cursor_overlay === false) return;
  createOverlayWindow();
  const bounds = overlayBounds();
  const cached = overlayBoundsCache;
  if (!cached || cached.x !== bounds.x || cached.y !== bounds.y ||
      cached.width !== bounds.width || cached.height !== bounds.height) {
    overlayBoundsCache = bounds;
    overlayWin.setBounds(bounds);
  }
  if (!overlayWin.isVisible()) overlayWin.showInactive();
  startOverlayPoll();
}

function hideOverlay() {
  clearTimeout(overlayHideTimer);
  stopOverlayPoll();
  if (!overlayWin || overlayWin.isDestroyed() || !overlayWin.isVisible()) return;
  // Give the renderer a moment to fade its content out first.
  overlayHideTimer = setTimeout(() => {
    if (overlayWin && !overlayWin.isDestroyed()) overlayWin.hide();
  }, 450);
}

// ---------- settings ----------

function createSettingsWindow() {
  if (settingsWin && !settingsWin.isDestroyed()) {
    settingsWin.focus();
    return;
  }
  settingsWin = new BrowserWindow({
    width: 520,
    height: 650,
    show: false,
    resizable: false,
    title: 'PCU Settings',
    backgroundColor: '#1b1d22',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  settingsWin.setMenuBarVisibility(false);
  settingsWin.loadFile(path.join(__dirname, 'renderer', 'settings.html'));
  settingsWin.once('ready-to-show', () => settingsWin.show());
}

// ---------- hotkey ----------

function applyHotkey() {
  const wanted = config.hotkey || DEFAULT_CONFIG.hotkey;
  if (registeredHotkey) {
    globalShortcut.unregister(registeredHotkey);
    registeredHotkey = null;
  }
  if (globalShortcut.isRegistered(wanted)) globalShortcut.unregister(wanted);
  const ok = globalShortcut.register(wanted, toggleBar);
  if (ok) {
    registeredHotkey = wanted;
  } else if (wanted !== DEFAULT_CONFIG.hotkey) {
    const fallback = DEFAULT_CONFIG.hotkey;
    if (globalShortcut.register(fallback, toggleBar)) {
      registeredHotkey = fallback;
    }
    showNotification('Hotkey not registered', `"${wanted}" is unavailable. Falling back to ${fallback}.`);
  } else {
    showNotification('Hotkey not registered', `"${wanted}" is unavailable. Try another combination in Settings.`);
  }
  console.log('[hotkey] registered:', registeredHotkey || 'none');
}

// ---------- tray ----------

function makeTrayIcon() {
  const scale = screen.getPrimaryDisplay()?.scaleFactor || 1;
  const size = scale >= 2 ? 32 : scale >= 1.5 ? 24 : scale >= 1.25 ? 20 : 16;
  const img = nativeImage.createFromPath(path.join(__dirname, 'assets', `icon-${size}.png`));
  return img;
}

function createTray() {
  tray = new Tray(makeTrayIcon());
  tray.setToolTip('Personal Computer Use');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Show instruction bar', click: showBar },
    { label: 'Settings', click: createSettingsWindow },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() }
  ]));
  tray.on('double-click', showBar);
}

// ---------- IPC ----------

// Renderers may only be pages bundled in this app (file: URLs under renderer/).
// Trust decision lives in ipc-trust.js (unit-tested with plain Node); packaged
// asar layouts work because __dirname sits inside app.asar and asar paths are
// plain strings.
function rendererDir() {
  return path.join(__dirname, 'renderer');
}

function isTrustedSender(event) {
  try {
    const frame = event.senderFrame;
    if (!frame || typeof frame.url !== 'string') return false;
    return trustedRendererPath(frame.url, rendererDir());
  } catch {
    return false;
  }
}

function assertTrustedSender(event) {
  if (!isTrustedSender(event)) throw new Error('Untrusted IPC sender');
}

function configSummary() {
  return {
    provider: config.provider,
    openai: { model: config.openai.model },
    anthropic: { model: config.anthropic.model },
    openai_compat: {
      base_url: config.openai_compat.base_url,
      model: config.openai_compat.model
    },
    hotkey: config.hotkey,
    action_delay_s: config.action_delay_s,
    pointer_glide_s: config.pointer_glide_s,
    cursor_overlay: config.cursor_overlay,
    keyConfigured: Boolean(config.apiKeyEncrypted || extractLegacyKey(config)),
    keySource: config.keySource || 'none',
    keyVersion: typeof config.keyVersion === 'number' ? config.keyVersion : 0,
    // Kept for UI compatibility. safeStorage is no longer used to persist
    // credentials (the backend owns the DPAPI store), so this now reflects
    // the thing that actually gates key writes: whether the backend key
    // store is reachable over the authenticated WebSocket.
    keyEncryptionAvailable: wsUp
  };
}

// Whitelist: renderers can never write key-bearing or unknown fields.
function pickEditableConfig(cfg) {
  const out = {};
  if (!cfg || typeof cfg !== 'object') return out;
  if (typeof cfg.provider === 'string') out.provider = cfg.provider;
  if (cfg.openai && typeof cfg.openai === 'object' &&
      typeof cfg.openai.model === 'string') out.openai = { model: cfg.openai.model };
  if (cfg.anthropic && typeof cfg.anthropic === 'object' &&
      typeof cfg.anthropic.model === 'string') out.anthropic = { model: cfg.anthropic.model };
  if (cfg.openai_compat && typeof cfg.openai_compat === 'object') {
    out.openai_compat = {};
    if (typeof cfg.openai_compat.base_url === 'string') out.openai_compat.base_url = cfg.openai_compat.base_url;
    if (typeof cfg.openai_compat.model === 'string') out.openai_compat.model = cfg.openai_compat.model;
  }
  if (typeof cfg.hotkey === 'string') out.hotkey = cfg.hotkey;
  if (typeof cfg.action_delay_s === 'number' && Number.isFinite(cfg.action_delay_s)) {
    out.action_delay_s = cfg.action_delay_s;
  }
  if (typeof cfg.pointer_glide_s === 'number' && Number.isFinite(cfg.pointer_glide_s)) {
    out.pointer_glide_s = cfg.pointer_glide_s;
  }
  if (typeof cfg.cursor_overlay === 'boolean') out.cursor_overlay = cfg.cursor_overlay;
  return out;
}

ipcMain.handle('get-config-summary', (e) => {
  assertTrustedSender(e);
  return configSummary();
});

ipcMain.handle('save-config', (e, cfg) => {
  assertTrustedSender(e);
  config = deepMerge(structuredClone(config), pickEditableConfig(cfg));
  const delay = Number(config.action_delay_s);
  config.action_delay_s = Number.isFinite(delay)
    ? Math.min(5, Math.max(0, delay))
    : DEFAULT_CONFIG.action_delay_s;
  writeConfig(config);
  applyHotkey();
  return configSummary();
});

// One-way key set: the renderer sends a new key and never receives the stored
// one back. Only summary data is returned. Fail closed: the key is stored by
// the BACKEND (raw-DPAPI apiKeyEncrypted) over the token-authenticated WS;
// when the backend is unreachable or reports a failure, nothing is written
// locally, the existing stored key is preserved untouched, and a clear,
// nontechnical error is surfaced. The key is registered with the secrets
// filter before any logging (the WS payload itself is never logged).
ipcMain.handle('set-api-key', async (e, key) => {
  assertTrustedSender(e);
  const plain = typeof key === 'string' ? key.trim() : '';
  if (!plain) throw new Error('Empty API key');
  addKnownSecret(plain);
  const result = await sendKeyOpAwait({
    type: 'rotate_key',
    apiKey: plain,
    keySource: 'user'
  });
  if (!result.ok) {
    const message = result.error ||
      'The key was NOT saved. Your existing key (if any) still works.';
    console.warn('[config] set-api-key rejected:', redact(message), '(fail closed)');
    showNotification('API key not saved', message);
    throw new Error(message);
  }
  // The backend rewrote config.json; refresh the in-memory summary.
  readConfig();
  return configSummary();
});

// "Use built-in key": the backend restores its last-known bundled key from
// bundledKeyEncrypted (maintained whenever a bundled key is adopted). Works
// in packaged AND dev mode via the same WS verb. There is deliberately NO
// local/seed fallback: Electron cannot persist credentials in a form the
// backend can read (safeStorage = OSCrypt), so a local write would recreate
// the very bug this removes. Fail closed: when no bundled backup exists (or
// the restore fails) the current key is preserved and the reason is surfaced.
ipcMain.handle('clear-api-key', async (e) => {
  assertTrustedSender(e);
  const result = await sendKeyOpAwait({ type: 'restore_bundled' });
  if (!result.ok) {
    const message = /no built-in key stored/i.test(String(result.error || ''))
      ? 'No built-in key stored on this installation.'
      : (result.error ||
        'Could not restore the built-in key. Your current key still works.');
    console.warn('[config] clear-api-key failed:', redact(message), '(fail closed)');
    throw new Error(message);
  }
  // The backend rewrote config.json; refresh the in-memory summary.
  readConfig();
  return configSummary();
});

ipcMain.handle('submit-instruction', (e, text) => {
  assertTrustedSender(e);
  const instruction = String(text || '').trim();
  if (!instruction) return { ok: false };
  const id = crypto.randomUUID();
  const sent = sendWs({ type: 'start_task', id, instruction });
  if (sent) showStatusCard();
  return { ok: sent, id };
});

ipcMain.handle('stop-task', (e) => {
  assertTrustedSender(e);
  return sendWs({ type: 'stop_task' });
});

ipcMain.handle('confirm', (e, id, approved) => {
  assertTrustedSender(e);
  sendWs({ type: 'confirm', id: String(id || ''), approved: Boolean(approved) });
});

ipcMain.on('hide-bar', (e) => {
  if (!isTrustedSender(e)) return;
  if (barWin && !barWin.isDestroyed()) barWin.hide();
});

// ---------- lifecycle ----------

app.setAppUserModelId('com.personal-computer-use.app');

app.on('second-instance', showBar);

app.whenReady().then(() => {
  provisionConfig();
  readConfig();
  // The stored credential blob is produced/owned by the backend (raw DPAPI)
  // and is intentionally NOT decrypted in Electron: key-material redaction
  // for forwarded backend output is done backend-side (secrets_filter), while
  // the Electron filter covers the runtime token and user-entered keys.
  createStatusWindow();
  createBarWindow();
  createOverlayWindow();
  screen.on('display-metrics-changed', () => {
    overlayBoundsCache = null;
    if (overlayWin && !overlayWin.isDestroyed() && overlayWin.isVisible()) {
      showOverlay();
    }
  });
  createTray();
  applyHotkey();
  spawnBackend();
  connectWs();
  if (PCU_AUTO_QUIT_MS > 0) {
    setTimeout(() => app.quit(), PCU_AUTO_QUIT_MS);
  }
});

app.on('window-all-closed', () => {
  // tray app: stay alive
});

app.on('before-quit', () => {
  quitting = true;
  clearTimeout(wsRetryTimer);
  clearTimeout(hideStatusTimer);
  clearTimeout(overlayHideTimer);
  stopOverlayPoll();
  if (overlayWin && !overlayWin.isDestroyed()) overlayWin.destroy();
  killBackend();
  forceRestoreCursor();
  deleteRuntimeToken();
  if (ws) {
    try { ws.close(); } catch {}
  }
});

app.on('quit', () => {
  if (registeredHotkey) globalShortcut.unregister(registeredHotkey);
});
