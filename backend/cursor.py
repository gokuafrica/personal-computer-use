"""Hide the native Windows cursor while the agent is in control.

The Electron overlay renders the violet agent pointer instead, so the real
(white) system cursor must disappear for the duration of a task. Restore uses
SPI_SETCURSORS, which reloads every system cursor from the registry and undoes
all SetSystemCursor swaps in one call — safe to run even when nothing was
swapped.
"""

from __future__ import annotations

import atexit

import ctypes
from ctypes import wintypes

_user32 = ctypes.windll.user32
_user32.CreateCursor.argtypes = (wintypes.HINSTANCE, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_user32.CreateCursor.restype = wintypes.HANDLE
_user32.SetSystemCursor.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_user32.SetSystemCursor.restype = wintypes.BOOL
_user32.DestroyCursor.argtypes = (wintypes.HANDLE,)
_user32.DestroyCursor.restype = wintypes.BOOL

OCR_ARROW = 32512
OCR_IBEAM = 32513
OCR_HAND = 32649
SPI_SETCURSORS = 0x0057

_MASK_W = 32
_hidden = False


def _blank_cursor():
    # All-ones AND mask + all-zeros XOR mask = fully transparent cursor.
    and_mask = b"\xff" * (_MASK_W * _MASK_W // 8)
    xor_mask = b"\x00" * (_MASK_W * _MASK_W // 8)
    return _user32.CreateCursor(None, 0, 0, _MASK_W, _MASK_W, and_mask, xor_mask)


def hide_native_cursor() -> None:
    """Swap arrow, I-beam and hand cursors for a blank one. Idempotent."""
    global _hidden
    if _hidden:
        return
    for flag in (OCR_ARROW, OCR_IBEAM, OCR_HAND):
        handle = _blank_cursor()
        if not handle or not _user32.SetSystemCursor(handle, flag):
            if handle:
                _user32.DestroyCursor(handle)
            restore_native_cursor()
            return
    _hidden = True


def restore_native_cursor() -> None:
    """Reload all system cursors from the registry. Idempotent and safe."""
    global _hidden
    _user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
    _hidden = False


atexit.register(restore_native_cursor)
