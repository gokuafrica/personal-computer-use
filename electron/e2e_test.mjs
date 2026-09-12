import WebSocket from 'ws';
import { authHeaders } from './ws_token.mjs';

// E2E driver: starts one task, records every backend event with timestamps,
// and prints a summary. Exit code 0 = task succeeded.
// Note: the backend serves ONE client; if Electron reconnects and takes the
// stream, this driver will time out — check the trajectory file in that case.
const URL = 'ws://127.0.0.1:8765';
const INSTRUCTION = process.argv[2] || "Open Notepad and type 'hello from PCU'";
let ws;
try {
  ws = new WebSocket(URL, { headers: authHeaders() });
} catch (err) {
  console.error(err.message);
  process.exit(4);
}
const t0 = Date.now();
const events = [];
let started = false;

ws.on('open', () => ws.send(JSON.stringify({ type: 'get_status' })));

ws.on('message', (data) => {
  let msg;
  try { msg = JSON.parse(data.toString()); } catch { return; }
  if (!started) {
    started = true;
    ws.send(JSON.stringify({ type: 'start_task', id: 'e2e-1', instruction: INSTRUCTION }));
    return;
  }
  const dt = Date.now() - t0;
  events.push({ dt, msg });
  if (msg.type === 'action') {
    console.log(`[+${String(dt).padStart(6)}ms] ACTION ${msg.kind} ${msg.detail}` +
      (Number.isFinite(msg.x) ? ` @overlay(${msg.x},${msg.y})` : ''));
  } else if (msg.type === 'status') {
    console.log(`[+${String(dt).padStart(6)}ms] STATUS ${msg.state} step=${msg.step || '-'} :: ${msg.message}`);
  } else if (msg.type === 'log') {
    console.log(`[+${String(dt).padStart(6)}ms] LOG ${msg.line}`);
  } else if (msg.type === 'need_confirmation') {
    console.log(`[+${String(dt).padStart(6)}ms] GATE ${msg.detail}`);
    setTimeout(() => {
      console.log(`[+${String(Date.now() - t0).padStart(6)}ms] AUTO-APPROVE ${msg.id}`);
      ws.send(JSON.stringify({ type: 'confirm', id: msg.id, approved: true }));
    }, 8000);
  } else if (msg.type === 'task_done') {
    console.log(`[+${String(dt).padStart(6)}ms] TASK_DONE success=${msg.success} :: ${msg.summary}`);
    console.log(JSON.stringify({
      success: msg.success,
      total_events: events.length,
      duration_ms: dt,
    }, null, 2));
    process.exit(msg.success ? 0 : 1);
  }
});

ws.on('error', (err) => { console.error('WS error:', err.message); process.exit(2); });
setTimeout(() => {
  console.error('TIMEOUT: no task_done after 150s');
  process.exit(3);
}, 150000);
