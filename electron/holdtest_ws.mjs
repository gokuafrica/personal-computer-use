import WebSocket from 'ws';
import fs from 'fs';

const URL = 'ws://127.0.0.1:8765';
const ws = new WebSocket(URL);
let started = false;
let done = false;
const lines = [];

const timeout = setTimeout(() => {
  console.log('TIMEOUT reached, sending stop_task');
  ws.send(JSON.stringify({ type: 'stop_task' }));
  setTimeout(() => process.exit(2), 5000);
}, 120000);

ws.on('open', () => {
  ws.send(JSON.stringify({ type: 'get_status' }));
});

ws.on('message', (raw) => {
  const msg = JSON.parse(raw.toString());
  const type = msg.type;
  let detail = '';
  if (type === 'log') detail = msg.line;
  else if (type === 'status') detail = `${msg.state} ${msg.message || ''} step=${msg.step ?? ''}`;
  else if (type === 'action') detail = `${msg.kind} ${msg.detail}`;
  else if (type === 'task_done') detail = `success=${msg.success} summary=${msg.summary}`;
  else detail = JSON.stringify(msg).slice(0, 200);
  const line = `[${type}] ${detail}`;
  lines.push(line);
  console.log(line);
  fs.writeFileSync('holdtest_events.log', lines.join('\n'));

  if (type === 'status' && msg.state === 'idle' && !started) {
    started = true;
    ws.send(JSON.stringify({ type: 'start_task', id: 'holdtest', instruction: 'Open Notepad and type hold test done' }));
  }
  if (type === 'task_done') {
    done = true;
    clearTimeout(timeout);
    console.log(`FINAL: success=${msg.success} summary=${msg.summary}`);
    setTimeout(() => process.exit(msg.success ? 0 : 1), 500);
  }
});

ws.on('error', (e) => { console.log('WS error:', e.message); process.exit(3); });
ws.on('close', () => { if (!done) { console.log('WS closed before task_done'); process.exit(4); } });
