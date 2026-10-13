"""Mouse and keyboard execution via pyautogui.

Must be imported AFTER per-monitor-v2 DPI awareness is set (main.py does that
before importing this module). Raises ``pyautogui.FailSafeException`` when the
mouse is moved to a screen corner, which the agent loop treats as an abort.
"""

from __future__ import annotations

import math
import ctypes
from ctypes import wintypes
import time
from typing import Callable

import pyautogui

pyautogui.PAUSE = 0.05
pyautogui.FAILSAFE = True

FailSafeException = pyautogui.FailSafeException

WHEEL_DELTA = 120


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", _InputUnion)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int)
_user32.SendInput.restype = wintypes.UINT


def _inject_pair(down: _Input, up: _Input) -> None:
    """Checked Windows input: never report a silently discarded click as success."""
    pyautogui.failSafeCheck()
    events = (_Input * 2)(down, up)
    count = _user32.SendInput(2, events, ctypes.sizeof(_Input))
    if count != 2:
        if count == 1:
            # Release a partially injected press before stopping the task.
            _user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(_Input))
        raise RuntimeError(
            "Windows blocked mouse or keyboard input. Stop and check whether "
            "the target app is running as administrator or the desktop is locked."
        )


def _click(down_flag: int, up_flag: int) -> None:
    _inject_pair(_Input(type=0, mi=_MouseInput(dwFlags=down_flag)),
                 _Input(type=0, mi=_MouseInput(dwFlags=up_flag)))

_KEY_ALIASES = {
    "win": "winleft",
    "cmd": "winleft",
    "super": "winleft",
    "meta": "winleft",
    "control": "ctrl",
    "esc": "escape",
    "delete": "del",
}


def _map_key(name: str) -> str:
    normalized = name.strip().lower()
    return _KEY_ALIASES.get(normalized, normalized)


def parse_combo(combo: str) -> list[str]:
    """Parse a combo like ``ctrl+s``, ``alt+f4`` or ``cmd+shift+p`` into pyautogui keys."""
    parts = [part for part in combo.split("+") if part.strip()]
    if not parts:
        raise ValueError(f"empty key combo: {combo!r}")
    return [_map_key(part) for part in parts]


def move(x: int, y: int, duration: float = 0.0) -> None:
    """Move the pointer. With duration > 0 the move is a distance-aware tween
    (longer moves take visibly longer, capped) so travel reads as human."""
    x, y = int(x), int(y)
    if duration <= 0:
        pyautogui.moveTo(x, y, duration=0)
        return
    cur = pyautogui.position()
    dist = math.hypot(x - cur.x, y - cur.y)
    # ~0.45s per 400px baseline from the caller's duration, clamped so tiny
    # nudges don't feel sluggish and cross-screen moves don't drag on.
    effective = min(1.2, max(0.15, dist * duration / 400.0))
    pyautogui.moveTo(x, y, duration=effective, tween=pyautogui.easeInOutQuad)


def left_click() -> None:
    _click(0x0002, 0x0004)


def double_click() -> None:
    left_click()
    time.sleep(0.08)
    left_click()


def right_click() -> None:
    _click(0x0008, 0x0010)


def scroll(amount: int, direction: str = "down") -> None:
    notches = max(1, abs(int(amount or 1)))
    clicks = notches * WHEEL_DELTA
    direction = direction.lower()
    if direction == "up":
        pyautogui.scroll(clicks)
    elif direction == "down":
        pyautogui.scroll(-clicks)
    elif direction == "left":
        pyautogui.hscroll(-clicks)
    elif direction == "right":
        pyautogui.hscroll(clicks)
    else:
        raise ValueError(f"unknown scroll direction: {direction!r}")


def type_text(text: str, stopped: Callable[[], bool] = lambda: False) -> None:
    """Type Unicode without a clipboard; stop between characters on cancellation."""
    for character in text.replace("\r\n", "\n").replace("\r", "\n"):
        if stopped():
            return
        if character in ("\n", "\t"):
            vk = 0x0D if character == "\n" else 0x09
            _inject_pair(_Input(type=1, ki=_KeyboardInput(wVk=vk)),
                         _Input(type=1, ki=_KeyboardInput(wVk=vk, dwFlags=2)))
        else:
            encoded = character.encode("utf-16-le")
            for offset in range(0, len(encoded), 2):
                unit = int.from_bytes(encoded[offset:offset + 2], "little")
                _inject_pair(_Input(type=1, ki=_KeyboardInput(wScan=unit, dwFlags=4)),
                             _Input(type=1, ki=_KeyboardInput(wScan=unit, dwFlags=6)))
        time.sleep(0.01)


def press_key(combo: str) -> None:
    keys = parse_combo(combo)
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)
