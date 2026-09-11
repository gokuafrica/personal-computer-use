"""Mouse and keyboard execution via pyautogui.

Must be imported AFTER per-monitor-v2 DPI awareness is set (main.py does that
before importing this module). Raises ``pyautogui.FailSafeException`` when the
mouse is moved to a screen corner, which the agent loop treats as an abort.
"""

from __future__ import annotations

import math

import pyautogui

pyautogui.PAUSE = 0.05
pyautogui.FAILSAFE = True

FailSafeException = pyautogui.FailSafeException

WHEEL_DELTA = 120

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
    pyautogui.click()


def double_click() -> None:
    pyautogui.doubleClick()


def right_click() -> None:
    pyautogui.rightClick()


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


def type_text(text: str) -> None:
    # pyautogui.write only types characters representable on the current
    # keyboard layout; non-ASCII text is typed best-effort for the MVP.
    pyautogui.write(text, interval=0.01)


def press_key(combo: str) -> None:
    keys = parse_combo(combo)
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)
