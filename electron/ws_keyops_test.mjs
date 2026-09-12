// WS key-op verification against a REAL running backend (no GUI, no real
// %APPDATA%): 1. Seeds a temp config dir with a FAKE bundled key
// DPAPI-encrypted through backend/secret_store.py (simulating the
// post-provision state, including the bundledKeyEncrypted backup).
// 2. Starts the backend on an ephemeral port (PCU_WS_PORT) with
// PCU_CONFIG_DIR pointed at the temp dir.
// 3. Connects from Node with X-PCU-Token and exercises:
//    rotate_key (keySource user) -> persists backend-encrypted user key
//    restore_bundled             -> restores the bundled key + version
//    rotate_key (bundled)        -> replaces + refreshes the backup
//    restore_bundled w/o backup  -> fail closed, current key kept
// 4. Asserts no plaintext key material in config.json, no fake key in the
//    backend stdout, and that every blob decrypts (via secret_store) to the
//    expected synthetic key.
// Fake key material only; temp dirs are removed on exit.
import { spawn, spawnSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import WebSocket from 'ws';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const FAKE_BUNDLED_V5 = 'sk-test-fake-bundled-000000000001-ws';
const FAKE_USER = 'sk-test-fake-user-000000000002-ws';
const FAKE_BUNDLED_V6 = 'sk-test-fake-bundled-000000000003-ws';
let failures = 0;

function check(name, ok, detail) {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ` :: ${detail}` : ''}`);
  if (!ok) failures += 1;
}

function pythonSecret(mode, value) {
  // protect/unprotect a synthetic key via the backend's secret_store.
  // Values travel on stdin, never on the command line.
  const script = [
    'import sys, json',
    `sys.path.insert(0, ${JSON.stringify(ROOT)})`,
    'from backend import secret_store',
    'data = sys.stdin.read()',
    `print(json.dumps(secret_store.${mode}(data)))`
  ].join('\n');
  const res = spawnSync('python', ['-c', script], {
    encoding: 'utf8',
    input: value
  });
  if (res.status !== 0) {
    throw new Error(`secret_store.${mode} failed: ${(res.stderr || res.stdout).trim()}`);
  }
  return JSON.parse(res.stdout.trim());
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

// Shared LIVE output capture. `output` is a mutable object whose `text`
// property is appended to by the stdout/stderr 'data' callbacks, so every
// reader (assertion code) sees the accumulated output at read time — unlike
// assigning a string once (`proc.log = log`), which freezes an empty copy.
// `drained` resolves only after BOTH streams emit 'close' (i.e. all buffered
// output has been delivered), and `exited` resolves on process exit.
function attachCapture(proc) {
  const output = { text: '' };
  const exited = new Promise((resolve) => {
    if (proc.exitCode !== null) resolve();
    else proc.on('exit', resolve);
  });
  const drained = new Promise((resolve) => {
    const done = { out: false, err: false };
    const mark = (stream) => {
      done[stream] = true;
      if (done.out && done.err) resolve();
    };
    proc.stdout.on('end', () => mark('out'));
    proc.stdout.on('close', () => mark('out'));
    proc.stderr.on('end', () => mark('err'));
    proc.stderr.on('close', () => mark('err'));
  });
  proc.stdout.on('data', (d) => { output.text += d.toString(); });
  proc.stderr.on('data', (d) => { output.text += d.toString(); });
  return { output, exited, drained };
}

// Reusable redaction assertion: throws (with a useful message) when the
// synthetic key is found in the captured buffer.
function assertNoSecret(buffer, secret, label) {
  if (typeof buffer === 'string' && buffer.includes(secret)) {
    throw new Error(
      `LEAK [${label}]: synthetic key ${secret.slice(0, 24)}... was found in captured output`
    );
  }
}

function startBackend(configDir, trajDir, port) {
  const proc = spawn('python', [path.join(ROOT, 'backend', 'main.py')], {
    cwd: ROOT,
    env: {
      ...process.env,
      PCU_CONFIG_DIR: configDir,
      PCU_TRAJECTORY_DIR: trajDir,
      PCU_WS_PORT: String(port)
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true
  });
  const capture = attachCapture(proc);
  proc.output = capture.output;
  proc.exited = capture.exited;
  proc.drained = capture.drained;
  return proc;
}

function connect(url, token) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url, { headers: { 'X-PCU-Token': token } });
    ws.on('error', (err) => reject(err));
    ws.on('open', () => resolve(ws));
  });
}

function awaitMsg(ws, pred, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      ws.off('message', onMessage);
      reject(new Error('timed out waiting for backend reply'));
    }, timeoutMs);
    const onMessage = (data) => {
      let msg;
      try { msg = JSON.parse(data.toString()); } catch { return; }
      if (pred(msg)) {
        clearTimeout(timer);
        ws.off('message', onMessage);
        resolve(msg);
      }
    };
    ws.on('message', onMessage);
  });
}

function sendKeyOp(ws, payload) {
  const op = payload.type;
  const reply = awaitMsg(ws, (m) => m.type === 'key_op_result' && m.op === op);
  ws.send(JSON.stringify(payload));
  return reply;
}

function readDisk(configDir) {
  return JSON.parse(fs.readFileSync(path.join(configDir, 'config.json'), 'utf8'));
}

async function stopBackend(proc, configDir, trajDir, label, allLogs) {
  if (proc) {
    if (proc.exitCode === null) {
      try { proc.kill(); } catch {}
    }
    // Drain before returning: wait for process exit AND stdout/stderr
    // 'end'/'close', bounded by a settle delay so late output is included
    // but teardown cannot hang forever.
    await Promise.race([Promise.all([proc.exited, proc.drained]), wait(2000)]);
    if (allLogs) allLogs.push(proc.output.text);
  }
  await wait(300);
  if (configDir) {
    try { fs.rmSync(configDir, { recursive: true, force: true }); } catch {}
  }
  if (trajDir) {
    try { fs.rmSync(trajDir, { recursive: true, force: true }); } catch {}
  }
}

async function main() {
  const allLogs = [];

  // ---- Session 1: bundled seeded -> user rotate -> restore -> bundled rotate
  const configDir1 = fs.mkdtempSync(path.join(os.tmpdir(), 'pcu-wskey1-'));
  const trajDir1 = fs.mkdtempSync(path.join(os.tmpdir(), 'pcu-wskey1-traj-'));
  const port1 = 20000 + Math.floor(Math.random() * 20000);
  let backend = null;
  try {
    fs.writeFileSync(path.join(configDir1, 'config.json'), JSON.stringify({
      provider: 'openai',
      openai: { model: 'computer-use-preview' },
      apiKeyEncrypted: pythonSecret('protect', FAKE_BUNDLED_V5),
      keySource: 'bundled',
      keyVersion: 5,
      bundledKeyEncrypted: pythonSecret('protect', FAKE_BUNDLED_V5),
      bundledKeyVersion: 5
    }, null, 2));

    backend = startBackend(configDir1, trajDir1, port1);
    const token = await waitForToken(path.join(configDir1, 'runtime_token'), 15000);
    await wait(300);
    const ws = await connect(`ws://127.0.0.1:${port1}`, token);

    // 1) user rotation via WS: backend encrypts + persists, user key wins.
    let reply = await sendKeyOp(ws, { type: 'rotate_key', apiKey: FAKE_USER, keySource: 'user' });
    check('rotate_key user: ok reply', reply.ok === true && reply.outcome === 'replaced', JSON.stringify(reply));
    let disk = readDisk(configDir1);
    check('rotate_key user: keySource user', disk.keySource === 'user');
    check('rotate_key user: no plaintext on disk',
      !JSON.stringify(disk).includes(FAKE_USER));
    check('rotate_key user: apiKeyEncrypted decrypts to user key',
      pythonSecret('unprotect', disk.apiKeyEncrypted) === FAKE_USER);
    check('rotate_key user: bundled backup kept',
      pythonSecret('unprotect', disk.bundledKeyEncrypted) === FAKE_BUNDLED_V5
      && disk.bundledKeyVersion === 5);

    // 2) restore_bundled: user key replaced by the backed-up bundled key.
    reply = await sendKeyOp(ws, { type: 'restore_bundled' });
    check('restore_bundled: ok reply', reply.ok === true, JSON.stringify(reply));
    disk = readDisk(configDir1);
    check('restore_bundled: keySource bundled + version restored',
      disk.keySource === 'bundled' && disk.keyVersion === 5);
    check('restore_bundled: active key decrypts to bundled key',
      pythonSecret('unprotect', disk.apiKeyEncrypted) === FAKE_BUNDLED_V5);
    check('restore_bundled: no plaintext on disk',
      !JSON.stringify(disk).includes(FAKE_BUNDLED_V5));

    // 3) bundled rotation refreshes the backup.
    reply = await sendKeyOp(ws, {
      type: 'rotate_key', apiKey: FAKE_BUNDLED_V6, keySource: 'bundled', keyVersion: 6
    });
    check('rotate_key bundled: ok reply', reply.ok === true && reply.outcome === 'replaced', JSON.stringify(reply));
    disk = readDisk(configDir1);
    check('rotate_key bundled: backup refreshed',
      pythonSecret('unprotect', disk.bundledKeyEncrypted) === FAKE_BUNDLED_V6
      && disk.bundledKeyVersion === 6
      && pythonSecret('unprotect', disk.apiKeyEncrypted) === FAKE_BUNDLED_V6);

    try { ws.close(); } catch {}
  } finally {
    await stopBackend(backend, configDir1, trajDir1, 'session1', allLogs);
  }

  // ---- Session 2: no bundled backup -> restore fails closed, key kept
  const configDir2 = fs.mkdtempSync(path.join(os.tmpdir(), 'pcu-wskey2-'));
  const trajDir2 = fs.mkdtempSync(path.join(os.tmpdir(), 'pcu-wskey2-traj-'));
  const port2 = 20000 + Math.floor(Math.random() * 20000);
  backend = null;
  try {
    fs.writeFileSync(path.join(configDir2, 'config.json'), JSON.stringify({
      provider: 'openai',
      openai: { model: 'computer-use-preview' },
      apiKeyEncrypted: pythonSecret('protect', FAKE_USER),
      keySource: 'user',
      keyVersion: 0
    }, null, 2));
    const diskBefore = fs.readFileSync(path.join(configDir2, 'config.json'), 'utf8');

    backend = startBackend(configDir2, trajDir2, port2);
    const token = await waitForToken(path.join(configDir2, 'runtime_token'), 15000);
    await wait(300);
    const ws = await connect(`ws://127.0.0.1:${port2}`, token);

    const reply = await sendKeyOp(ws, { type: 'restore_bundled' });
    check('restore_bundled without backup: fails closed',
      reply.ok === false && /no built-in key stored/i.test(reply.error || ''),
      JSON.stringify(reply));
    check('restore_bundled without backup: no key material in error',
      !(reply.error || '').includes(FAKE_USER));
    const diskAfter = fs.readFileSync(path.join(configDir2, 'config.json'), 'utf8');
    check('restore_bundled without backup: config untouched', diskAfter === diskBefore);
    check('restore_bundled without backup: user key still active',
      pythonSecret('unprotect', readDisk(configDir2).apiKeyEncrypted) === FAKE_USER);

    try { ws.close(); } catch {}
  } finally {
    await stopBackend(backend, configDir2, trajDir2, 'session2', allLogs);
  }

  // ---- Privacy: fake keys never appear in backend output.
  // These read the LIVE captured stdout+stderr (drained on stop), so a leak
  // would now actually be seen here.
  const combined = allLogs.join('\n');
  for (const key of [FAKE_BUNDLED_V5, FAKE_USER, FAKE_BUNDLED_V6]) {
    try {
      assertNoSecret(combined, key, 'backend output');
      check(`backend output redacts ${key.slice(0, 24)}...`, true);
    } catch (err) {
      check(`backend output redacts ${key.slice(0, 24)}...`, false, err.message);
    }
  }

  // ---- Negative controls (intentionally "failing"): these PROVE the
  // redaction assertion above is meaningful. Each check below is expected
  // to PASS *because* assertNoSecret correctly THROWS/flags a planted
  // synthetic key. No production code is involved.
  console.log('NOTE: negative-control checks below intentionally plant a key and expect the assertion to catch it.');

  const planted = 'sk-test-fake-planted-000000000009-neg';

  // 1) In-memory: a buffer that contains the planted key must make
  //    assertNoSecret throw.
  try {
    assertNoSecret(`backend said: ${planted} (oops)`, planted, 'negative control (in-memory)');
    check('negative control: assertNoSecret throws on planted key', false,
      'assertNoSecret did NOT throw on a buffer containing the planted key');
  } catch {
    check('negative control: assertNoSecret throws on planted key', true, 'planted key caught as expected');
  }

  // 2) End-to-end: a trivial child process echoes the planted key through
  //    the SAME capture helper used for the backend, and the assertion must
  //    flag it.
  const negProc = spawn(process.execPath, ['-e', `process.stdout.write(${JSON.stringify(`echo: ${planted}\n`)})`], {
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true
  });
  const negCapture = attachCapture(negProc);
  await Promise.race([Promise.all([negCapture.exited, negCapture.drained]), wait(5000)]);
  try {
    assertNoSecret(negCapture.output.text, planted, 'negative control (child echo)');
    check('negative control: capture helper + assertNoSecret catch child echo', false,
      'planted key echoed by child process was NOT flagged');
  } catch (err) {
    check('negative control: capture helper + assertNoSecret catch child echo', true, 'planted key caught as expected');
  }

  if (failures > 0) process.exit(1);
  console.log('WS-KEYOPS PASS');
}

main();
