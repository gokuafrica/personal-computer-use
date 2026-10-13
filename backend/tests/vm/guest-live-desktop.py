import asyncio,json,os,subprocess,sys,time,uuid
from pathlib import Path
import websockets
HOME=Path.home();ROOT=HOME/'Downloads'/'pcu-audit-live';ROOT.mkdir(exist_ok=True)
CFG=Path(os.environ['APPDATA'])/'personal-computer-use'
TEXT='Luna café Ω 🙂'
async def main():
    async with websockets.connect('ws://127.0.0.1:8765',additional_headers={'X-PCU-Token':(CFG/'runtime_token').read_text().strip()}) as ws:
        await ws.send('{"type":"stop_task"}')
    await asyncio.sleep(.5)
    fixture=subprocess.Popen(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(HOME/'Downloads'/'fixture.ps1'),'-Root',str(ROOT)],creationflags=subprocess.CREATE_NO_WINDOW)
    reports=[]
    try:
        for _ in range(150):
            if (ROOT/'fixture-result.json').exists():break
            await asyncio.sleep(.2)
        await asyncio.sleep(2)
        print('FIXTURE READY; awaiting native focus check',flush=True)
        for _ in range(600):
            if (ROOT/'start-live.txt').exists():break
            await asyncio.sleep(.2)
        assert (ROOT/'start-live.txt').exists(),'Native focus check not completed'
        for mode in (('gated',) if '--gated-only' in sys.argv else ('default','gated')):
            task_id='live-'+mode+'-'+uuid.uuid4().hex[:8]
            raw=json.loads((CFG/'config.json').read_text(encoding='utf-8-sig'));raw['confirm_actions']=mode=='gated';raw['max_steps']=12
            (CFG/'config.json').write_text(json.dumps(raw),encoding='utf-8')
            nonce=str(time.monotonic());(ROOT/'fixture-reset.json').write_text(nonce)
            for _ in range(50):
                if (ROOT/'fixture-ready.txt').exists() and (ROOT/'fixture-ready.txt').read_text()==nonce:break
                await asyncio.sleep(.1)
            token=(CFG/'runtime_token').read_text().strip();started=time.monotonic();confirmations=0;events=[]
            async with websockets.connect('ws://127.0.0.1:8765',additional_headers={'X-PCU-Token':token}) as ws:
                await ws.send(json.dumps({'type':'start_task','id':task_id,'instruction':f'Use only the foreground Local computer-use test window. Click its Submit test button exactly once, then click Test text and type exactly: {TEXT} Verify click count 1 and exact text before finishing. Do not open other apps or use shortcuts or save files.'}))
                while True:
                    m=json.loads(await asyncio.wait_for(ws.recv(),150));events.append(m)
                    if m['type'] in ('action','need_confirmation','task_done'):print(json.dumps(m),flush=True)
                    if m['type']=='need_confirmation':
                        confirmations+=1
                        assert mode=='gated'
                        # Leave approval to the real native status-card button.
                        print('WAITING FOR REAL APPROVE BUTTON',flush=True)
                    if m['type']=='task_done' and m.get('id')==task_id:break
                actual=json.loads((ROOT/'fixture-result.json').read_text(encoding='utf-8-sig'))
                report={'case':mode,'success':m['success'],'actual':actual,'confirmations':confirmations,'seconds':round(time.monotonic()-started,1)}
                reports.append(report);print(json.dumps(report),flush=True)
                (ROOT/(mode+'-events.json')).write_text(json.dumps(events))
                assert m['success'] and actual=={'clicks':1,'text':TEXT},report
                assert confirmations==(1 if mode=='gated' else 0),report
    finally:
        (HOME/'Downloads'/'pcu-vm-evidence'/'live-desktop.json').write_text(json.dumps(reports,indent=2))
        fixture.terminate();fixture.wait(5)
        raw=json.loads((CFG/'config.json').read_text(encoding='utf-8-sig'));raw['confirm_actions']=False
        (CFG/'config.json').write_text(json.dumps(raw),encoding='utf-8')
asyncio.run(main())
