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

const ROOT = path.join(__dirname, '..');
const BACKEND_URL = 'ws://127.0.0.1:8765';

const DEFAULT_CONFIG = {
  provider: 'openai',
  openai: { api_key: '', model: 'computer-use-preview' },
  anthropic: { api_key: '', model: 'claude-3-7-sonnet-latest' },
  openai_compat: { base_url: '', api_key: '', model: '' },
  hotkey: 'Control+Alt+K',
  max_steps: 25,
  action_delay_s: 0.4
};

// ---------- config location ----------
// Packaged: %APPDATA%/<productName> (Electron userData). Dev: repo root.
function configDirPath() {
  if (app.isPackaged) return app.getPath('userData');
  return ROOT;
}

function configPath() {
  const dir = configDirPath();
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, 'config.json');
}
// True only when a provider section carries a non-empty api_key.
function configHasApiKey(cfg) {
  if (!cfg || typeof cfg !== 'object') return false;
  for (const section of ['openai', 'anthropic', 'openai_compat']) {
    const sub = cfg[section];
    if (sub && typeof sub === 'object' &&
        typeof sub.api_key === 'string' && sub.api_key.trim() !== '') return true;
  }
  return false;
}

function migrateConfig() {
  const dir = configDirPath();
  const activePath = path.join(dir, 'config.json');
  const legacyPath = path.join(ROOT, 'config.json');
  if (path.relative(activePath, legacyPath) === '') return;
  if (!fs.existsSync(legacyPath)) return;

  const activeExists = fs.existsSync(activePath);
  // An unreadable/corrupt active config is treated as empty so a valid
  // legacy config can still rescue it; keys are never logged.
  let activeIsEmpty = true;
  if (activeExists) {
    try {
      activeIsEmpty = !configHasApiKey(JSON.parse(fs.readFileSync(activePath, 'utf8')));
    } catch {}
  }
  let legacyHas = false;
  try {
    legacyHas = configHasApiKey(JSON.parse(fs.readFileSync(legacyPath, 'utf8')));
  } catch {}

  // Copy when no active config yet; overwrite an existing active config
  // only when it is untouched defaults (all api keys empty) and the legacy
  // one holds a real key — this repairs installs where defaults were
  // written before migration. A legacy config with a non-empty key wins
  // entirely over an empty-key active config; an empty-key legacy never
  // overwrites an active config that has keys.
  const shouldCopy = !activeExists || (activeIsEmpty && legacyHas);
  if (!shouldCopy) return;
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.copyFileSync(legacyPath, activePath);
    console.log('[config] migrated legacy config.json ->', activePath);
  } catch (err) {
    console.log('[config] migration failed:', err.message);
  }
}

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 5000;

let config = { ...DEFAULT_CONFIG };
let tray = null;
let barWin = null;
let statusWin = null;
let settingsWin = null;
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
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify(cfg, null, 2), 'utf8');
}

function showNotification(title, body) {
  const n = new Notification({ title, body, silent: false });
  n.show();
}

// ---------- backend process ----------

function backendEnv() {
  const env = { ...process.env, PCU_CONFIG_DIR: configDirPath() };
  env.PCU_TRAJECTORY_DIR = app.isPackaged
    ? path.join(os.homedir(), 'Documents', 'PCU', 'trajectories')
    : path.join(ROOT, 'trajectories');
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
    if (l) console.log('[backend]', l);
  });
  proc.stdout.on('data', line);
  proc.stderr.on('data', line);
}

function onBackendExit(code) {
  console.log('[backend] exited with code', code);
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

// ---------- websocket client ----------

function connectWs() {
  if (quitting) return;
  clearTimeout(wsRetryTimer);
  try {
    ws = new WebSocket(BACKEND_URL);
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

function broadcastConnection(connected) {
  for (const win of [statusWin, barWin]) {
    if (win && !win.isDestroyed()) {
      win.webContents.send('backend-connection', { connected });
    }
  }
}

function handleBackendMessage(msg) {
  if (!msg || typeof msg.type !== 'string') return;
  if (statusWin && !statusWin.isDestroyed()) {
    statusWin.webContents.send('backend-message', msg);
  }
  switch (msg.type) {
    case 'need_confirmation':
      showStatusCard();
      break;
    case 'status':
      if (msg.state === 'running') {
        showStatusCard();
      }
      break;
    case 'task_done':
      scheduleStatusHide();
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

// ---------- settings ----------

function createSettingsWindow() {
  if (settingsWin && !settingsWin.isDestroyed()) {
    settingsWin.focus();
    return;
  }
  settingsWin = new BrowserWindow({
    width: 520,
    height: 560,
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
  const size = 16;
  const buf = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 4;
      buf[i] = 0x4c; buf[i + 1] = 0x8b; buf[i + 2] = 0xf0; // #4c8bf0
      buf[i + 3] = 0xff;
    }
  }
  return nativeImage.createFromBitmap(buf, { width: size, height: size });
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

ipcMain.handle('get-config', () => config);

ipcMain.handle('save-config', (_e, cfg) => {
  if (!cfg || typeof cfg !== 'object') throw new Error('Invalid config');
  config = deepMerge(structuredClone(DEFAULT_CONFIG), cfg);
  const delay = Number(config.action_delay_s);
  config.action_delay_s = Number.isFinite(delay)
    ? Math.min(5, Math.max(0, delay))
    : DEFAULT_CONFIG.action_delay_s;
  writeConfig(config);
  applyHotkey();
  return config;
});

ipcMain.handle('submit-instruction', (_e, text) => {
  const instruction = String(text || '').trim();
  if (!instruction) return { ok: false };
  const id = crypto.randomUUID();
  const sent = sendWs({ type: 'start_task', id, instruction });
  if (sent) showStatusCard();
  return { ok: sent, id };
});

ipcMain.handle('stop-task', () => sendWs({ type: 'stop_task' }));

ipcMain.handle('confirm', (_e, id, approved) => {
  sendWs({ type: 'confirm', id: String(id || ''), approved: Boolean(approved) });
});

ipcMain.on('hide-bar', () => {
  if (barWin && !barWin.isDestroyed()) barWin.hide();
});

// ---------- lifecycle ----------

app.setAppUserModelId('com.personal-computer-use.app');

app.on('second-instance', showBar);

app.whenReady().then(() => {
  migrateConfig();
  readConfig();
  createStatusWindow();
  createBarWindow();
  createTray();
  applyHotkey();
  spawnBackend();
  connectWs();
});

app.on('window-all-closed', () => {
  // tray app: stay alive
});

app.on('before-quit', () => {
  quitting = true;
  clearTimeout(wsRetryTimer);
  clearTimeout(hideStatusTimer);
  killBackend();
  if (ws) {
    try { ws.close(); } catch {}
  }
});

app.on('quit', () => {
  if (registeredHotkey) globalShortcut.unregister(registeredHotkey);
});
