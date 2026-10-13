// Isolated local-only test surface. Not included in installers.
const {app, BrowserWindow, ipcMain} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const root = process.env.PCU_AUDIT_DIR;
if (!root || !path.basename(root).startsWith('pcu-audit-')) throw Error('Isolated audit directory required');
app.setPath('userData', path.join(root, 'fixture-user-data'));
app.commandLine.appendSwitch('force-renderer-accessibility');
let win;
ipcMain.on('result', (event, value) => {
  if (event.sender !== win.webContents) return;
  fs.writeFileSync(path.join(root, 'fixture-result.json'), JSON.stringify(value));
});
app.whenReady().then(async () => {
  win = new BrowserWindow({title:'PCU audit fixture',width:900,height:650,x:50,y:50,
    webPreferences:{nodeIntegration:true,contextIsolation:false}});
  await win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(`<!doctype html>
    <title>PCU audit fixture</title><style>body{font:24px Arial;padding:40px;background:#f5f7fa}
    button{font:24px Arial;background:#14783d;color:white;padding:18px 35px}
    textarea{display:block;width:650px;height:140px;font:26px Arial;margin-top:12px}</style>
    <h1>Local computer-use test</h1><p>Nothing is sent or saved outside this test.</p>
    <button id="submit">Submit test</button><p id="count">Clicks: 0</p>
    <label for="text">Test text</label><textarea id="text"></textarea>
    <script>const {ipcRenderer}=require('electron');let clicks=0;
    const text=document.getElementById('text');
    const report=()=>ipcRenderer.send('result',{clicks,text:text.value});
    document.getElementById('submit').onclick=()=>{clicks++;document.getElementById('count').textContent='Clicks: '+clicks;report();};
    text.oninput=report;report();</script>`));
  win.show();
});
app.on('window-all-closed',()=>app.quit());
