"""DPAPI secret storage (Windows CryptProtectData, user scope, no extra entropy).

Blobs are interoperable with Electron safeStorage on Windows, which uses
DPAPI without additional entropy. protect() -> base64 string, unprotect() ->
plaintext or None. Never log plaintext or blob values.
"""

from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _CRYPTPROTECTDATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _blob(data: bytes) -> _CRYPTPROTECTDATA_BLOB:
    buf = (ctypes.c_byte * len(data)).from_buffer_copy(data if data else b"\x00")
    # Empty input is padded with one zero byte; cbData stays 0 so DPAPI still
    # round-trips an empty plaintext correctly.
    blob = _CRYPTPROTECTDATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    return blob


def _bytes_from_blob(blob: _CRYPTPROTECTDATA_BLOB) -> bytes:
    if blob.cbData == 0:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def protect(plaintext: str) -> str:
    """Encrypt plaintext with DPAPI (user scope); returns a base64 blob string."""
    if sys.platform != "win32":
        raise OSError("DPAPI secret storage requires Windows")
    data = plaintext.encode("utf-8")
    in_blob = _blob(data)
    out_blob = _CRYPTPROTECTDATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    ):
        raise OSError("CryptProtectData failed")
    try:
        return base64.b64encode(_bytes_from_blob(out_blob)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def unprotect(blob_b64: str) -> str | None:
    """Decrypt a base64 DPAPI blob; returns None on any failure."""
    if sys.platform != "win32":
        raise OSError("DPAPI secret storage requires Windows")
    try:
        data = base64.b64decode(blob_b64)
    except (ValueError, TypeError):
        return None
    in_blob = _blob(data)
    out_blob = _CRYPTPROTECTDATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    ):
        return None
    try:
        return _bytes_from_blob(out_blob).decode("utf-8", errors="replace")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def unprotect_bytes(blob_b64: str) -> bytes | None:
    """Decrypt a base64 DPAPI blob to raw bytes; None on any failure.

    Used by provision-blob consumption so the caller can apply the same
    string decoding rules as protect() (UTF-8) with a UTF-16LE fallback.
    """
    if sys.platform != "win32":
        raise OSError("DPAPI secret storage requires Windows")
    try:
        data = base64.b64decode(blob_b64)
    except (ValueError, TypeError):
        return None
    in_blob = _blob(data)
    out_blob = _CRYPTPROTECTDATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    ):
        return None
    try:
        return _bytes_from_blob(out_blob)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
