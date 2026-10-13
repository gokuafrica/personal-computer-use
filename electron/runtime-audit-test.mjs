// Execute production functions in a small Electron/DOM harness; no desktop input.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('./main.js', import.meta.url), 'utf8');
function section(start, end) { return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start))); }
let shows = 0, hides = 0, barMessages = 0, createdOptions;
const window = {
  isDestroyed: () => false, isVisible: () => true,
  showInactive: () => shows++, show: () => { throw Error('Focus stolen'); },
  hide: () => hides++, getBounds: () => ({ x: 100, y: 100, width: 420, height: 340 }),
  webContents: { send: () => {}, isLoading: () => false },
  setAlwaysOnTop: () => {}, setPosition: () => {}, loadFile: () => {}, on: () => {}
};
const context = vm.createContext({
  statusWin: window, overlayWin: null,
  barWin: { isDestroyed: () => false, webContents: { send: () => barMessages++ } },
  screen: { screenToDipPoint: p => p, getPrimaryDisplay: () => ({ workArea: {x:0,y:0,width:1000,height:800} }) },
  convertActionPoint: () => {}, resolveKeyOp: () => {}, showOverlay: () => {}, hideOverlay: () => {},
  hideStatusTimer: null, clearTimeout: () => {}, setTimeout: () => {},
  path: { join: (...p) => p.join('/') }, __dirname: '/app', quitting: false,
  BrowserWindow: function(options) { createdOptions = options; return window; }
});
vm.runInContext(section('function handleBackendMessage', '// ---------- instruction bar'), context);
context.handleBackendMessage({type:'status',state:'running'});
assert.equal(shows, 1);
assert.equal(barMessages, 1);
context.handleBackendMessage({type:'need_confirmation'});
assert.equal(shows, 2);
context.handleBackendMessage({type:'action',x:200,y:200});
assert.equal(hides, 1, 'card must not intercept target click');
context.handleBackendMessage({type:'action',x:20,y:20});
assert.equal(hides, 1);
context.createStatusWindow();
assert.equal(createdOptions.focusable, false, 'approval click must not focus status card');
console.log('PASS status/approval focus, overlapping click target, instruction bar broadcasts');

async function checkSubmission(result, rejected = false) {
  const callbacks = {};
  const elements = Object.fromEntries(['instruction','hint','running','bar'].map(id => [id, {
    value: id === 'instruction' ? 'keep my task' : '', disabled:false, textContent:'',
    classList:{toggle(){}}, focus(){}, select(){},
    addEventListener: (event, fn) => {callbacks[id+event] = fn;}
  }]));
  let hidden = false;
  const pcu = {hideBar:()=>{hidden=true;}, submitInstruction:()=> rejected ? Promise.reject(Error('offline')) : Promise.resolve(result),
    onBackendMessage(){},onBackendConnection(){},onBarShown(){}};
  vm.runInNewContext(fs.readFileSync(new URL('./renderer/instruction-bar.js', import.meta.url),'utf8'), {
    document:{getElementById:id=>elements[id]},window:{pcu}
  });
  callbacks.instructionkeydown({key:'Enter'});
  await new Promise(resolve=>setImmediate(resolve));
  if (result?.ok && !rejected) {
    assert.equal(elements.instruction.value,''); assert.equal(hidden,true);
  } else {
    assert.equal(elements.instruction.value,'keep my task'); assert.equal(hidden,false);
    assert.equal(elements.instruction.disabled,false);
  }
}
await checkSubmission({ok:false});
await checkSubmission(null,true);
await checkSubmission({ok:true});
console.log('PASS disconnected submission preserves instruction; successful submission clears it');
