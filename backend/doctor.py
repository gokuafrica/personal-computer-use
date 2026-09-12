"""Diagnostics command inspired by cua-driver's `doctor`.

Run from the repo root:  python backend/doctor.py

Prints a plain-text PASS/FAIL/WARN checklist covering the Python env, imports,
DPI awareness, screen capture, input control, the WS port, config.json sanity,
and (for openai_compat) a tiny live connectivity call. Secrets are never
printed. Exits 0 when all critical checks pass, 1 otherwise.
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend import config as config_mod  # noqa: E402
from backend import secrets_filter  # noqa: E402

CONFIG_PATH = config_mod.CONFIG_PATH
HOST = "127.0.0.1"
PORT = 8765
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) personal-computer-use/0.1"


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def mask_key(key: str) -> str:
    """Mask an API key: show a short prefix and the last 4 chars only."""
    key = (key or "").strip()
    if not key:
        return "<empty>"
    if len(key) <= 4:
        return "…"
    prefix = key[:3]
    return f"{prefix}...{key[-4:]}"


class Doctor:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, bool]] = []  # tag, label, detail, critical

    def add(self, tag: str, label: str, detail: str = "", critical: bool = True) -> None:
        self.rows.append((tag, label, detail, critical))

    def ok(self, label: str, detail: str = "", critical: bool = True) -> None:
        self.add("PASS", label, detail, critical)

    def fail(self, label: str, detail: str = "", critical: bool = True) -> None:
        self.add("FAIL", label, detail, critical)

    def warn(self, label: str, detail: str = "") -> None:
        self.add("WARN", label, detail, False)

    def report(self) -> str:
        width = max(len(r[1]) for r in self.rows) if self.rows else 0
        lines = []
        for tag, label, detail, _critical in self.rows:
            line = f"[{tag}] {label.ljust(width)}"
            if detail:
                line += f"  {detail}"
            # Filter at the output boundary: no secret material reaches stdout.
            lines.append(secrets_filter.filter_text(line.rstrip()))
        return "\n".join(lines)

    def failed_critical(self) -> bool:
        return any(tag == "FAIL" and critical for tag, _l, _d, critical in self.rows)


def check_python(doc: Doctor) -> None:
    v = sys.version_info
    if v >= (3, 11):
        doc.ok("python version", f"{v.major}.{v.minor}.{v.micro} (>= 3.11)")
    else:
        doc.fail(
            "python version",
            f"{v.major}.{v.minor}.{v.micro} — need >= 3.11",
            critical=True,
        )


def check_imports(doc: Doctor) -> None:
    required = [
        ("mss", "mss"),
        ("PIL", "Pillow"),
        ("pyautogui", "pyautogui"),
        ("websockets", "websockets"),
        ("openai", "openai"),
        ("anthropic", "anthropic"),
    ]
    for module, pip_name in required:
        try:
            __import__(module)
        except Exception as exc:
            doc.fail(
                f"import {module}",
                f"missing — pip install {pip_name} ({type(exc).__name__})",
                critical=True,
            )
        else:
            doc.ok(f"import {module}")

    try:
        __import__("uiautomation")
        doc.ok("import uiautomation", "element grounding available", critical=False)
    except Exception:
        doc.warn("import uiautomation", "missing (element grounding optional) — pip install uiautomation")


def check_dpi(doc: Doctor) -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:
            pass
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())  # type: ignore[attr-defined]
    except Exception:
        try:
            import ctypes.wintypes  # noqa: F401  (ensure wintypes loaded for win32 API)

            hwnd = ctypes.windll.user32.GetDesktopWindow()
            dpi = int(ctypes.windll.user32.GetDpiForWindow(hwnd))  # type: ignore[attr-defined]
        except Exception as exc:
            doc.warn("dpi awareness", f"could not query DPI: {type(exc).__name__}")
            return
    if dpi > 96:
        doc.warn("dpi awareness", f"per-monitor-v2 set, system DPI {dpi} (>96, scaled display)")
    else:
        doc.warn("dpi awareness", f"per-monitor-v2 set, system DPI {dpi} (default scaling)")


def check_capture(doc: Doctor) -> None:
    try:
        import mss

        mss_cls = getattr(mss, "MSS", mss.mss)
        with mss_cls() as sct:
            virtual = sct.monitors[0]
            shot = sct.grab(virtual)
            w, h = int(shot.width), int(shot.height)
        if w > 0 and h > 0:
            doc.ok("screen capture (mss)", f"virtual screen {w}x{h}")
        else:
            doc.fail("screen capture (mss)", f"grab returned zero-size image {w}x{h}", critical=True)
    except Exception as exc:
        doc.fail("screen capture (mss)", f"{type(exc).__name__}: {exc}", critical=True)


def check_pyautogui(doc: Doctor) -> None:
    try:
        import pyautogui  # noqa: F401

        size = pyautogui.size()
        doc.ok(
            "pyautogui",
            f"screen size {size.width}x{size.height}; FAILSAFE={bool(pyautogui.FAILSAFE)}"
            " (move mouse to top-left corner to abort)",
        )
    except Exception as exc:
        doc.fail("pyautogui", f"{type(exc).__name__}: {exc}", critical=True)


def check_port(doc: Doctor) -> None:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            pass
        doc.ok(f"ws port {HOST}:{PORT}", "something is listening (backend running)")
    except OSError:
        doc.ok(f"ws port {HOST}:{PORT}", "free (backend not running)", critical=False)


def check_config(doc: Doctor) -> dict[str, object] | None:
    if not CONFIG_PATH.is_file():
        doc.fail("config.json", "missing — start the app once or create it manually", critical=True)
        return None
    try:
        json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        doc.fail("config.json", f"unreadable/invalid JSON: {exc}", critical=True)
        return None

    doc.ok("config.json", "exists and parses")

    # Runtime view: legacy plaintext is migrated on load and the decrypted
    # key is materialized into the active provider section (memory only).
    cfg = config_mod.load()

    status = config_mod.key_status()
    if status["encrypted"]:
        doc.ok("key storage", f"DPAPI-encrypted, keySource={status['keySource']!r}, "
                              f"keyVersion={status['keyVersion']!r}")
    elif status["legacyPlaintext"]:
        doc.warn("key storage", "plaintext present but DPAPI encryption failed "
                                "(see config load); keySource="
                                f"{status['keySource']!r}")
    else:
        doc.warn("key storage", "no key stored yet")

    provider = str(cfg.get("provider", "")).strip()
    if provider in ("openai", "anthropic", "openai_compat"):
        doc.ok("provider", provider)
    else:
        doc.fail("provider", f"{provider!r} not one of openai|anthropic|openai_compat", critical=True)
        return cfg

    section = cfg.get(provider)
    if not isinstance(section, dict):
        doc.fail(f"config {provider}", "section missing", critical=True)
        return cfg

    api_key = str(section.get("api_key", "") or "")
    if api_key.strip():
        doc.ok(f"config {provider}.api_key", f"present ({mask_key(api_key)})")
    else:
        doc.fail(f"config {provider}.api_key", "empty — set it in settings", critical=True)

    if provider == "openai_compat":
        base_url = str(section.get("base_url", "") or "").strip()
        model = str(section.get("model", "") or "").strip()
        if base_url:
            doc.ok("config openai_compat.base_url", base_url)
        else:
            doc.fail("config openai_compat.base_url", "empty", critical=True)
        if model:
            doc.ok("config openai_compat.model", model)
        else:
            doc.fail("config openai_compat.model", "empty", critical=True)

    hotkey = cfg.get("hotkey")
    if isinstance(hotkey, str) and hotkey.strip():
        doc.ok("hotkey", hotkey)
    else:
        doc.fail("hotkey", "empty or not a string", critical=True)

    return cfg


def check_provider_live(doc: Doctor, cfg: dict[str, object] | None) -> None:
    if cfg is None:
        doc.warn("provider connectivity", "skipped (config unavailable)")
        return
    provider = str(cfg.get("provider", ""))
    if provider not in ("openai", "anthropic", "openai_compat"):
        doc.warn("provider connectivity", "skipped (unknown provider)")
        return
    section = cfg.get(provider)
    if not isinstance(section, dict):
        doc.warn("provider connectivity", "skipped (section missing)")
        return
    api_key = str(section.get("api_key", "") or "")
    if not api_key.strip():
        doc.warn("provider connectivity", "skipped (no key)")
        return

    if provider in ("openai", "anthropic"):
        doc.ok("provider connectivity", f"{provider}: key present (not tested live)")
        return

    base_url = str(section.get("base_url", "") or "").strip().rstrip("/")
    model = str(section.get("model", "") or "").strip()
    url = base_url + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 5,
    }).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "x-opencode-session": str(uuid.uuid4()),
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        latency_ms = (time.perf_counter() - t0) * 1000.0
        replied = ""
        try:
            replied = str(data["choices"][0]["message"]["content"]).strip().replace("\n", " ")[:40]
        except Exception:
            pass
        doc.ok(
            "provider connectivity",
            f"{url} -> {resp.status}, model {data.get('model', model)!r}, "
            f"{latency_ms:.0f} ms" + (f", replied {replied!r}" if replied else ""),
        )
    except urllib.error.HTTPError as exc:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        doc.fail(
            "provider connectivity",
            f"HTTP {exc.code} {exc.reason} from {url} after {latency_ms:.0f} ms "
            "(check base_url/key/model; body not printed to avoid leaking secrets)",
            critical=True,
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        doc.fail(
            "provider connectivity",
            f"{type(exc).__name__}: {exc} ({url}, after {latency_ms:.0f} ms)",
            critical=True,
        )


def main() -> int:
    doc = Doctor()
    check_python(doc)
    check_dpi(doc)
    check_imports(doc)
    check_capture(doc)
    check_pyautogui(doc)
    check_port(doc)
    cfg = check_config(doc)
    check_provider_live(doc, cfg)

    n_pass = sum(1 for r in doc.rows if r[0] == "PASS")
    n_fail = sum(1 for r in doc.rows if r[0] == "FAIL")
    n_warn = sum(1 for r in doc.rows if r[0] == "WARN")
    status = "HEALTHY" if not doc.failed_critical() else "UNHEALTHY"
    print(doc.report())
    print(f"\nSummary: {n_pass} passed, {n_fail} failed, {n_warn} warnings -> {status} "
          f"(utc {_now_iso()})")
    return 0 if not doc.failed_critical() else 1


if __name__ == "__main__":
    sys.exit(main())
