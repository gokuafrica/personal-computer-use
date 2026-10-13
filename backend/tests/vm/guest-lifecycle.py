import asyncio, hashlib, json, os, subprocess, time
from pathlib import Path
import websockets
HOME=Path.home();EVID=HOME/'Downloads'/'pcu-vm-evidence'
ROOT=HOME/'Downloads'/'pcu-audit-lifecycle';ROOT.mkdir(exist_ok=True)
INSTALL=Path(os.environ['LOCALAPPDATA'])/'Programs'/'personal-computer-use'
EXE=INSTALL/'Personal Computer Use.exe'
def ps(code):
    return subprocess.check_output(['powershell.exe','-NoProfile','-Command',code],text=True,creationflags=subprocess.CREATE_NO_WINDOW).strip()
def backend_pids():
    raw=ps("@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object {$_.CommandLine -like '*resources\\backend\\main.py*'} | Select-Object -ExpandProperty ProcessId) | ConvertTo-Json -Compress")
    data=json.loads(raw) if raw else [];return data if isinstance(data,list) else [data]
def launch(ms):
    env=dict(os.environ,PCU_CONFIG_DIR=str(ROOT),PCU_AUTO_QUIT_MS=str(ms),PCU_TRAJECTORY_DIR=str(ROOT/'trajectories'))
    return subprocess.Popen([str(EXE)],env=env,stdout=open(ROOT/f'app-{ms}.log','w'),stderr=subprocess.STDOUT)
def wait_for(f,seconds=15):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=f()
        if value:return value
        time.sleep(.2)
    raise AssertionError('condition timed out')
async def connect_checks(token):
    checks=[]
    for name,headers,origin in [('missing',{},None),('wrong',{'X-PCU-Token':'invalid-vm-test'},None),('foreign-origin',{'X-PCU-Token':token},'https://example.invalid')]:
        try:
            async with websockets.connect('ws://127.0.0.1:8765',additional_headers=headers,origin=origin):pass
            raise AssertionError(name+' accepted')
        except websockets.exceptions.InvalidStatus as e:checks.append({'case':name,'rejected':True,'httpStatus':e.response.status_code})
    async with websockets.connect('ws://127.0.0.1:8765',additional_headers={'X-PCU-Token':token}) as ws:
        await ws.recv();await ws.send('{invalid');await ws.send('[]');await ws.send('{"type":"get_status"}')
        found=False
        for _ in range(5):
            m=json.loads(await asyncio.wait_for(ws.recv(),3))
            if m['type']=='status':found=True;break
        assert found
    return checks
(ROOT/'config.json').write_text(json.dumps({'provider':'openai_compat','openai_compat':{'base_url':'http://127.0.0.1:18991/v1','model':'vm-test'},'confirm_actions':False}))
token_file=ROOT/'runtime_token'
app=launch(45000)
wait_for(token_file.exists);first_token=token_file.read_text().strip();time.sleep(2)
report={'auth':asyncio.run(connect_checks(first_token)),'malformedWsRecovery':True}
first=backend_pids();assert len(first)==1,first
duplicate=launch(8000);duplicate.wait(8);time.sleep(1)
assert backend_pids()==first,'Duplicate app spawned backend'
report['singleInstance']=True
subprocess.run(['taskkill','/PID',str(first[0]),'/F'],check=True,capture_output=True)
new=wait_for(lambda: [p for p in backend_pids() if p!=first[0]],20)
assert len(new)==1
time.sleep(2)
second_token=token_file.read_text().strip();assert second_token!=first_token
report['backendCrashRestartsOnce']=True;report['runtimeTokenRotates']=True
report['newTokenAuth']=asyncio.run(connect_checks(second_token))
subprocess.run(['taskkill','/PID',str(new[0]),'/F'],check=True,capture_output=True)
time.sleep(6);assert not backend_pids();report['secondCrashDoesNotLoop']=True
app.wait(30);time.sleep(1);assert not token_file.exists();assert not backend_pids()
report['quitRemovesTokenAndBackend']=True
app2=launch(10000);wait_for(token_file.exists);third_token=token_file.read_text().strip();assert third_token!=second_token
app2.wait(20);time.sleep(1);assert not token_file.exists();assert not backend_pids()
report['restartAfterCrash']=True
(EVID/'lifecycle.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)
