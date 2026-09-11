import WebSocket from 'ws';
const ws = new WebSocket('ws://127.0.0.1:8765');
ws.on('open', () => ws.send(JSON.stringify({ type: 'get_status' })));
ws.on('message', (m) => { console.log(m.toString()); process.exit(0); });
ws.on('error', (e) => { console.log('ERR', e.message); process.exit(1); });
setTimeout(() => { console.log('NO REPLY'); process.exit(2); }, 5000);
