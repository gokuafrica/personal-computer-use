"""Opt-in live subscription image/schema test; does not operate the desktop.

Run explicitly: python backend/tests/test_codex_live.py
"""
import asyncio
import base64
import io
from pathlib import Path
import sys
import time
import secrets
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from PIL import Image, ImageDraw
from backend.providers.codex_provider import CodexProvider


async def main():
    image = Image.new("RGB", (800, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 300, 180), fill="navy")
    draw.text((130, 130), "Continue", fill="white", font_size=25)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    provider = CodexProvider({"codex": {"model": "gpt-6-luna"}})
    start = time.monotonic()
    result = await asyncio.wait_for(provider.run_step(
        base64.b64encode(stream.getvalue()).decode(), 800, 400,
        "Click the blue Continue button. This is a synthetic screenshot test.", None,
        {"window_title": "Synthetic test", "elements": [{"id": 7, "role": "Button", "name": "Continue", "center": [200, 140]}]},
    ), timeout=60)
    assert not result.done, result
    assert result.actions == [{"kind": "click_element", "id": 7}] or (
        len(result.actions) == 1 and result.actions[0]["kind"] == "click"
        and 100 <= result.actions[0]["x"] <= 300 and 100 <= result.actions[0]["y"] <= 180), result
    print(f"PASS subscription + gpt-6-luna + image + schema + UI grounding ({time.monotonic()-start:.1f}s)")
    print(result.actions)

    # No accessibility tree, target coordinates or answer in the prompt.
    # A fresh random code prevents a canned answer from passing this test.
    code = 'VISION-' + secrets.token_hex(3).upper()
    left = 100 + secrets.randbelow(350)
    top = 170 + secrets.randbelow(80)
    photo = Image.new('RGB', (800, 400), 'white')
    drawing = ImageDraw.Draw(photo)
    drawing.text((60, 40), code, fill='black', font_size=34)
    drawing.rectangle((left, top, left + 200, top + 80), fill='green')
    drawing.text((left + 20, top + 25), 'Continue', fill='white', font_size=26)
    image_bytes = io.BytesIO()
    photo.save(image_bytes, format='PNG')
    vision = await asyncio.wait_for(provider.run_step(
        base64.b64encode(image_bytes.getvalue()).decode(), 800, 400,
        'Click the green button. '
        'This is a synthetic screenshot test.', None, a11y_context=None), 60)
    assert len(vision.actions) == 1 and vision.actions[0]['kind'] == 'click', vision
    click = vision.actions[0]
    assert left <= click['x'] <= left + 200 and top <= click['y'] <= top + 80, vision
    readback = await asyncio.wait_for(provider.run_step(
        base64.b64encode(image_bytes.getvalue()).decode(), 800, 400,
        'Read the code printed above the green button and report that exact code in your '
        'completion summary. This task is observation only; do not click or type.',
        None, a11y_context=None), 60)
    assert readback.done and code in readback.summary, f'Screenshot-only OCR expected {code}, got {readback}'
    print('PASS screenshot-only vision: fresh random-code OCR and target coordinates, no UIA')

    # Cancel a real running native Codex process, after its creation is observed.
    original_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    children = []
    folders = []
    async def observed_spawn(*args, **kwargs):
        child = await original_spawn(*args, **kwargs)
        children.append(child)
        folders.append(Path(kwargs['cwd']))
        spawned.set()
        return child
    with patch('asyncio.create_subprocess_exec', side_effect=observed_spawn):
        task = asyncio.create_task(provider.run_step(
            base64.b64encode(stream.getvalue()).decode(), 800, 400,
            'Describe the screenshot carefully without taking an action.', None))
        await asyncio.wait_for(spawned.wait(), 10)
        await asyncio.sleep(.2)
        assert children[0].returncode is None, 'Process finished before cancellation could be tested'
        task.cancel()
        try:
            await task
            raise AssertionError('Cancellation did not propagate')
        except asyncio.CancelledError:
            pass
    assert children[0].returncode is not None
    assert all(not folder.exists() for folder in folders)
    print('PASS live cancellation: native process stopped and temporary image removed')


if __name__ == "__main__":
    asyncio.run(main())
