// End-to-end auth verification (no GUI, no real %APPDATA%):
// 1. Provisions a temp config dir with a FAKE key DPAPI-encrypted through
//    backend/secret_store.py.
// 2. Starts the backend on an ephemeral port (PCU_WS_PORT) with PCU_CONFIG_DIR
//    pointed at the temp dir.
// 3. Connects from Node with X-PCU-Token read from the backend's runtime_token
//    and asserts the handshake succeeds.
// 4. Asserts no-token, wrong-token and foreign-origin connections are rejected
//    with close code 1008.
// Fake key material only; temp dirs are removed on exit.
import { spawn, spawnSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import WebSocket from 'ws';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const FAKE_KEY = 'sk-test-fake-000000000000-e2e';
const PORT = 20000 + Math.floor(Math.random() * 20000);
const URL = `ws://127.0.0.1:${PORT}`;
let failures = 0;

function check(name, ok, detail) {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ` :: ${detail}` : ''}`);
  if (!ok) failures += 1;
}

function encryptFakeKey(configDir) {
  const script = [
    'import sys, json',
    `sys.path.insert(0, ${JSON.stringify(ROOT)})`,
    'from backend import secret_store',
    `print(json.dumps(secret_store.protect(${JSON.stringify(FAKE_KEY)})))`
  ].join('\n');
  const res = spawnSync('python', ['-c', script], { encoding: 'utf8' });
  if (res.status !== 0) {
    throw new Error(`provisioning failed: ${(res.stderr || res.stdout).trim()}`);
  }
  const config = {
    provider: 'openai',
    openai: { model: 'computer-use-preview' },
    apiKeyEncrypted: JSON.parse(res.stdout.trim()),
    keySource: 'user',
    keyVersion: 0
  };
  fs.writeFileSync(path.join(configDir, 'config.json'), JSON.stringify(config, null, 2));
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForToken(tokenPath, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const token = fs.readFileSync(tokenPath, 'utf8').trim();
      if (token) return token;
    } catch {}
    await wait(100);
  }
  throw new Error(`runtime_token never appeared at ${tokenPath}`);
}

// Auth now runs in process_request BEFORE the HTTP 101 handshake completes
// (missing/wrong token -> HTTP 401, foreign origin -> HTTP 403), so never
// resolve on 'open': accept = first {"type":"status"} message, reject =
// unexpected HTTP response. A timeout guards both.
function connect(headers, timeoutMs = 5000) {
  return new Promise((resolve) => {
    let settled = false;
    let sawOpen = false;
    const ws = new WebSocket(URL, { headers });
    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { ws.terminate(); } catch {}
      resolve(result);
    };
    const timer = setTimeout(
      () => finish({ opened: sawOpen, closeCode: ws._closeCode, timedOut: true }),
      timeoutMs
    );
    ws.on('open', () => { sawOpen = true; });
    ws.on('message', (data) => {
      try {
        const msg = JSON.parse(data.toString());
        if (msg.type === 'status') finish({ opened: true, status: true });
      } catch {}
    });
    ws.on('unexpected-response', (_req, res) =>
      finish({ opened: false, httpStatus: res.statusCode }));
    ws.on('close', (code) => finish({ opened: false, closeCode: code }));
    ws.on('error', (err) =>
      finish({ opened: false, closeCode: ws._closeCode, httpStatus: err.statusCode,
        err: String(err && err.message).slice(0, 120) }));
  });
}

async function main() {
  const configDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pcu-e2e-auth-'));
  const trajDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pcu-e2e-traj-'));
  const tokenPath = path.join(configDir, 'runtime_token');
  let backend = null;
  try {
    encryptFakeKey(configDir);
    backend = spawn('python', [path.join(ROOT, 'backend', 'main.py')], {
      cwd: ROOT,
      env: {
        ...process.env,
        PCU_CONFIG_DIR: configDir,
        PCU_TRAJECTORY_DIR: trajDir,
        PCU_WS_PORT: String(PORT)
      },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true
    });
    let backendLog = '';
    backend.stdout.on('data', (d) => { backendLog += d.toString(); });
    backend.stderr.on('data', (d) => { backendLog += d.toString(); });
    backend.on('exit', (code) => {
      if (code !== 0 && code !== null && !failures) {
        console.error('backend exited early:', backendLog.slice(-500));
      }
    });

    const token = await waitForToken(tokenPath, 15000);
    check('backend issued runtime_token', Boolean(token));
    await wait(300);

    const good = await connect({ 'X-PCU-Token': token });
    check('valid token accepted', good.opened && Boolean(good.status),
      good.opened ? 'received status broadcast' : JSON.stringify(good));

    const rejected = (r) => !r.opened && (r.closeCode === 1008 || (r.httpStatus ?? 0) >= 401);
    const detail = (r) =>
      `closeCode=${r.closeCode} httpStatus=${r.httpStatus ?? '-'}${r.timedOut ? ' TIMEDOUT' : ''}${r.err ? ` err=${r.err}` : ''}`;

    const none = await connect({});
    check('no token rejected', rejected(none), detail(none));

    const wrong = await connect({ 'X-PCU-Token': 'wrong-token-value' });
    check('wrong token rejected', rejected(wrong), detail(wrong));

    const foreign = await connect({ 'X-PCU-Token': token, Origin: 'http://evil.example' });
    check('foreign browser origin rejected', rejected(foreign), detail(foreign));
  } finally {
    if (backend && backend.exitCode === null) {
      try { backend.kill(); } catch {}
    }
    await wait(200);
    try { fs.rmSync(configDir, { recursive: true, force: true }); } catch {}
    try { fs.rmSync(trajDir, { recursive: true, force: true }); } catch {}
  }
  if (failures > 0) process.exit(1);
  console.log('E2E-AUTH PASS');
}

main();
