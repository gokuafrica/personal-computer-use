"""Opt-in test against the running isolated Electron app and local fixture.

Start electron/audit-fixture.cjs, then the PCU app with PCU_CONFIG_DIR pointing
at the same pcu-audit-* directory. Activate the fixture. In gated mode, approve
the harmless fixture click using the real status card.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import websockets

TEXT = 'Luna café Ω 🙂'


async def run(root, gated):
    assert root.name.startswith('pcu-audit-'), 'Only isolated test directories are allowed'
    config_path = root / 'config.json'
    cfg = json.loads(config_path.read_text(encoding='utf-8-sig'))
    cfg['confirm_actions'] = gated
    config_path.write_text(json.dumps(cfg), encoding='utf-8')
    token = (root / 'runtime_token').read_text(encoding='utf-8').strip()
    before = json.loads((root / 'fixture-result.json').read_text(encoding='utf-8'))
    assert before == {'clicks':0,'text':''}, 'Restart the fixture before each run'
    started = time.monotonic()
    approvals = 0
    task_id = ('audit-gated-' if gated else 'audit-default-') + uuid.uuid4().hex[:8]
    async with websockets.connect('ws://127.0.0.1:8765', additional_headers={'X-PCU-Token':token}) as ws:
        await ws.send(json.dumps({'type':'start_task','id':task_id,
            'instruction':f'Use only the foreground Local computer-use test window. Click its Submit test button '
            f'exactly once, then click the Test text box and type exactly: {TEXT} '
            'Verify the click count is 1 and the text matches, then finish. '
            'Do not open apps, use shortcuts to switch windows, or save anything.'}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 150))
            if msg['type'] in ('need_confirmation','action','task_done','log'):
                print(json.dumps(msg, ensure_ascii=True), flush=True)
            if msg['type'] == 'need_confirmation':
                approvals += 1
                if not gated:
                    await ws.send(json.dumps({'type':'stop_task'}))
                    raise AssertionError('Default mode unexpectedly asked permission')
            if msg['type'] == 'task_done' and msg.get('id') == task_id:
                assert msg['success'], msg
                break
    actual = json.loads((root / 'fixture-result.json').read_text(encoding='utf-8'))
    assert actual == {'clicks':1,'text':TEXT}, actual
    assert approvals == (1 if gated else 0), approvals
    report = {'mode':'gated' if gated else 'default','success':True,'confirmations':approvals,
              'clicks':actual['clicks'],'unicodeTextExact':True,'seconds':round(time.monotonic()-started,1)}
    (root / ('gated-report.json' if gated else 'default-report.json')).write_text(json.dumps(report,indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('root',type=Path)
    parser.add_argument('--gated',action='store_true')
    args=parser.parse_args()
    asyncio.run(run(args.root,args.gated))
