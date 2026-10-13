"""VM-only black-box regression against installed Electron, backend and input.

Uses a loopback simulated VLM; it does NOT certify ChatGPT authentication/model.
"""
import asyncio, base64, io, json, os, re, subprocess, sys, threading, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import websockets
from PIL import Image

HOME=Path.home(); ROOT=HOME/'Downloads'/'pcu-audit-vm'; ROOT.mkdir(exist_ok=True)
INSTALL=Path(os.environ['LOCALAPPDATA'])/'Programs'/'personal-computer-use'
TEXT='Luna café Ω 🙂'
state={'mode':'normal','calls':0,'images':0,'error':None}
results=[]
def fixture_state():
    for _ in range(20):
        try: return json.loads((ROOT/'fixture-result.json').read_text(encoding='utf-8-sig'))
        except (OSError,ValueError):time.sleep(.03)
    raise AssertionError('Fixture state unreadable')
class Model(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        state['calls']+=1
        try:
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            content=payload['messages'][-1]['content']
            prompt=next(x['text'] for x in content if x['type']=='text')
            encoded=next(x['image_url']['url'] for x in content if x['type']=='image_url')
            im=Image.open(io.BytesIO(base64.b64decode(encoded.split(',',1)[1])));im.verify()
            state['images']+=1
            mode=state['mode']
            if mode=='slow':time.sleep(8)
            if mode=='http401':
                self.send_response(401);self.end_headers();self.wfile.write(b'{"error":{"message":"Simulated expired credential","type":"authentication_error"}}');return
            a=[];done=False
            if mode=='invalid':reply='invalid JSON for controlled test'
            elif mode=='limit':reply=json.dumps({'actions':[{'kind':'wait','amount':.01}],'done':False})
            elif mode=='slow':reply=json.dumps({'actions':[{'kind':'wait','amount':.01}],'done':False})
            else:
                assert 'window_title="Local computer-use test"' in prompt, prompt[:500]
                f=fixture_state()
                target='Submit test' if f['clicks']==0 else 'Test text'
                if f['clicks']==0 or not f['text']:
                    match=re.search(r'id=(\d+) role=[^\n]*?name="'+target+r'"[^\n]*?center=\(([^,]+),([^\)]+)\)',prompt)
                    assert match, 'Missing fixture UIA control '+target
                    click=({'kind':'click','x':float(match[2]),'y':float(match[3])} if mode=='pixel' else {'kind':'click_element','id':int(match[1])})
                    a=[click]
                    if f['clicks']:
                        a.append({'kind':'type','text':TEXT if mode!='stoptype' else 'x'*3000})
                else:
                    assert f=={'clicks':1,'text':TEXT},f
                    done=True
                reply=json.dumps({'actions':a,'done':done,'summary':'Local fixture regression'})
            body=json.dumps({'id':'vm-test','object':'chat.completion','created':0,'model':'vm-simulated','choices':[{'index':0,'message':{'role':'assistant','content':reply},'finish_reason':'stop'}]}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        except Exception as e:
            state['error']=str(e);self.send_response(400);self.end_headers()

def configure(mode):
    cfg={'provider':'openai_compat','openai_compat':{'base_url':'http://127.0.0.1:18991/v1','model':'vm-simulated','api_key':''},'confirm_actions':mode in ('gated','decline','stopgate'),'max_steps':3 if mode in ('invalid','limit') else 8,'action_delay_s':.1,'pointer_glide_s':.65,'cursor_overlay':True,'saveScreenshots':False}
    (ROOT/'config.json').write_text(json.dumps(cfg),encoding='utf-8')

async def task(mode):
    configure(mode);state.update(mode=mode,calls=0,images=0,error=None)
    confirmations=0;stopped=False;events=[];started=time.monotonic();stop_time=None
    nonce=str(time.monotonic())
    (ROOT/'fixture-reset.json').write_text(nonce)
    try:
        for _ in range(150):
            if (ROOT/'fixture-ready.txt').exists() and (ROOT/'fixture-ready.txt').read_text()==nonce:break
            await asyncio.sleep(.2)
        await asyncio.sleep(.5)
        assert fixture_state()=={'clicks':0,'text':''}
        token=(ROOT/'runtime_token').read_text().strip()
        async with websockets.connect('ws://127.0.0.1:8765',additional_headers={'X-PCU-Token':token}) as ws:
            await ws.send(json.dumps({'type':'start_task','id':'vm-'+mode,'instruction':'Only exercise the local fixture Submit test and Test text controls. No external effects.'}))
            while True:
                m=json.loads(await asyncio.wait_for(ws.recv(),75));events.append(m)
                if m['type']=='need_confirmation':
                    confirmations+=1
                    assert mode in ('gated','decline','stopgate'),m
                    if mode=='stopgate':
                        await ws.send(json.dumps({'type':'stop_task'}));stopped=True;stop_time=time.monotonic()
                    else:await ws.send(json.dumps({'type':'confirm','id':m['id'],'approved':mode=='gated'}))
                if not stopped and m['type']=='action' and ((mode=='stopclick' and m['kind']=='click_element') or (mode=='stoptype' and m['kind']=='type')):
                    await asyncio.sleep(.08 if mode=='stopclick' else .9)
                    await ws.send(json.dumps({'type':'stop_task'}));stopped=True;stop_time=time.monotonic()
                if mode=='slow' and not stopped and m['type']=='status' and m.get('state')=='running':
                    await asyncio.sleep(2);await ws.send(json.dumps({'type':'stop_task'}));stopped=True;stop_time=time.monotonic()
                if m['type']=='task_done' and m.get('id')=='vm-'+mode:break
            elapsed=round(time.monotonic()-started,2)
            stop_latency=round(time.monotonic()-stop_time,2) if stop_time else None
        await asyncio.sleep(1.3)
        actual=fixture_state();await asyncio.sleep(.6);assert fixture_state()==actual,'Input continued after completion'
        expect=mode in ('normal','gated','pixel','recovery')
        assert m['success']==expect,m
        if expect:assert actual=={'clicks':1,'text':TEXT},actual
        elif mode=='stoptype':assert actual['clicks']==1 and 0<len(actual['text'])<3000,actual
        else:assert actual=={'clicks':0,'text':''},actual
        assert confirmations==(1 if mode in ('gated','decline','stopgate') else 0),confirmations
        assert not state['error'],state['error']
        assert state['images']>0
        r={'case':mode,'pass':True,'seconds':elapsed,'modelCalls':state['calls'],'validScreenshots':state['images'],'confirmations':confirmations,'clicks':actual['clicks'],'textLength':len(actual['text']),'stopSeconds':stop_latency}
        results.append(r);print(json.dumps(r),flush=True)
    finally:
        (ROOT/(mode+'-events.json')).write_text(json.dumps(events),encoding='utf-8')

async def main():
    server=ThreadingHTTPServer(('127.0.0.1',18991),Model);threading.Thread(target=server.serve_forever,daemon=True).start()
    configure('normal')
    env=dict(os.environ,PCU_CONFIG_DIR=str(ROOT),PCU_TRAJECTORY_DIR=str(ROOT/'trajectories'),PCU_AUTO_QUIT_MS='300000')
    log=open(ROOT/'app.log','w',encoding='utf-8')
    app=subprocess.Popen([str(INSTALL/'Personal Computer Use.exe')],env=env,stdout=log,stderr=log)
    fproc=None
    try:
        for _ in range(100):
            if (ROOT/'runtime_token').exists():break
            await asyncio.sleep(.2)
        await asyncio.sleep(3)
        fproc=subprocess.Popen(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(HOME/'Downloads'/'fixture.ps1'),'-Root',str(ROOT)],creationflags=subprocess.CREATE_NO_WINDOW)
        for mode in ('normal','gated','decline','stopgate','stopclick','stoptype','pixel','http401','invalid','limit','slow','recovery'):
            try:await task(mode)
            except Exception as e:
                results.append({'case':mode,'pass':False,'error':repr(e),'modelError':state['error']});print(json.dumps(results[-1]),flush=True)
                # Stop a failed fixture run before the next independent case.
                try:
                    async with websockets.connect('ws://127.0.0.1:8765',additional_headers={'X-PCU-Token':(ROOT/'runtime_token').read_text().strip()}) as ws:await ws.send('{"type":"stop_task"}')
                except Exception:pass
                await asyncio.sleep(2)
    finally:
        (HOME/'Downloads'/'pcu-vm-evidence'/'interactions.json').write_text(json.dumps({'model':'loopback simulated VLM','installedVersion':'1.1.3','cases':results},indent=2),encoding='utf-8')
        server.shutdown()
        if fproc is not None:fproc.terminate();fproc.wait(5)
    assert all(r['pass'] for r in results), 'VM regression failures'

asyncio.run(main())
