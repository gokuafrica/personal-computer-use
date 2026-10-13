"""Opt-in full backend test. Prepare a blank Notepad tab in the foreground first.

Starts an isolated authenticated WebSocket backend, uses live Luna, exercises
capture/action/recapture, then tests stop during inference. Never approves gates.
Verify the visible Notepad text independently after running this test.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import websockets

ROOT = Path(__file__).resolve().parents[2]
TEXT = "PCU GPT-6 Luna subscription test passed."


async def run():
    with tempfile.TemporaryDirectory(prefix='pcu-e2e-') as folder:
        root = Path(folder)
        (root / 'config.json').write_text(json.dumps({
            'provider': 'codex', 'codex': {'model': 'gpt-6-luna'}, 'max_steps': 8,
            'saveScreenshots': False, 'action_delay_s': 0.3,
        }))
        env = dict(os.environ, PCU_CONFIG_DIR=folder, PCU_TRAJECTORY_DIR=str(root / 'trajectories'), PCU_WS_PORT='18765')
        process = subprocess.Popen([sys.executable, str(ROOT / 'backend' / 'main.py')], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        started = time.monotonic()
        try:
            for _ in range(100):
                if (root / 'runtime_token').exists():
                    break
                if process.poll() is not None:
                    raise RuntimeError('Backend exited before startup')
                await asyncio.sleep(.1)
            token = (root / 'runtime_token').read_text().strip()
            async with websockets.connect('ws://127.0.0.1:18765', additional_headers={'X-PCU-Token': token}) as ws:
                await ws.send(json.dumps({'type':'start_task', 'id':'luna-notepad', 'instruction':
                    f'In the current blank Notepad tab, click the editor then type exactly: {TEXT} '
                    'Verify that exact text is visible and finish. Use only this tab. Do not open apps, switch tabs, save, or use terminal commands.'}))
                actions = 0
                while True:
                    message = json.loads(await asyncio.wait_for(ws.recv(), 90))
                    if message['type'] in ('log', 'action', 'task_done'):
                        print(json.dumps(message), flush=True)
                    if message['type'] == 'action':
                        actions += 1
                    if message['type'] == 'need_confirmation':
                        await ws.send(json.dumps({'type':'stop_task'}))
                        raise AssertionError('Unexpected confirmation; test stopped without approving')
                    if message['type'] == 'task_done':
                        assert message['success'] and actions >= 2, message
                        break
                print(f'PASS full backend screenshot/action/recapture task ({time.monotonic()-started:.1f}s)', flush=True)
                await ws.send(json.dumps({'type':'start_task', 'id':'luna-stop', 'instruction':'Describe the visible Notepad text, do not act.'}))
                await asyncio.sleep(.5)
                stop_start = time.monotonic()
                await ws.send(json.dumps({'type':'stop_task'}))
                while True:
                    message = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if message['type'] == 'task_done':
                        assert not message['success'], message
                        print(f'PASS stop during inference ({time.monotonic()-stop_start:.1f}s)', flush=True)
                        break
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    asyncio.run(run())
