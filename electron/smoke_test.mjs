import WebSocket from 'ws';
import { authHeaders } from './ws_token.mjs';

const URL = 'ws://127.0.0.1:8765';
let ws;
try {
  ws = new WebSocket(URL, { headers: authHeaders() });
} catch (err) {
  console.error(err.message);
  process.exit(4);
}

const messages = [];
let doneCount = 0;
let doneAt = null;
let taskStarted = false;
const COLLECT_MS = 8000;
let finishTimer = null;

function counts() {
  let statusBefore = 0, statusAfter = 0, logBefore = 0, logAfter = 0;
  for (const m of messages) {
    if (doneAt !== null && m.ts > doneAt) {
      if (m.type === 'status') statusAfter++;
      if (m.type === 'log') logAfter++;
    } else {
      if (m.type === 'status') statusBefore++;
      if (m.type === 'log') logBefore++;
    }
  }
  return { statusBefore, statusAfter, logBefore, logAfter };
}

function report(exitCode) {
  const c = counts();
  console.log(JSON.stringify({
    task_done: doneCount,
    ...c,
    total: messages.length,
  }));
  if (process.env.DUMP) {
    for (const m of messages.slice(0, 60)) console.log('MSG', JSON.stringify(m.msg).slice(0, 140));
    console.log('... last 20:');
    for (const m of messages.slice(-20)) console.log('MSG', JSON.stringify(m.msg).slice(0, 140));
  }
  process.exit(exitCode);
}

ws.on('open', () => {
  ws.send(JSON.stringify({ type: 'get_status' }));
});

ws.on('message', (data) => {
  let msg;
  try { msg = JSON.parse(data.toString()); } catch { return; }
  messages.push({ type: msg.type, msg, ts: Date.now() });
  if (msg.type === 'status' && !taskStarted) {
    taskStarted = true;
    ws.send(JSON.stringify({ type: 'start_task', id: 'smoke-1', instruction: 'open notepad' }));
  }
  if (msg.type === 'task_done') {
    doneCount++;
    if (doneAt === null) doneAt = Date.now();
    if (finishTimer === null) {
      finishTimer = setTimeout(() => {
        const c = counts();
        const totalStatus = c.statusBefore + c.statusAfter;
        const ok = doneCount === 1 && c.statusAfter === 0 && c.logAfter === 0 && totalStatus <= 30;
        console.error(ok ? 'SMOKE PASS' : 'SMOKE FAIL');
        report(ok ? 0 : 1);
      }, COLLECT_MS);
    }
  }
});

ws.on('error', (err) => { console.error('WS error:', err.message); process.exit(2); });
setTimeout(() => { console.error('TIMEOUT waiting for task_done'); report(3); }, 60000);
