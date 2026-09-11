"""Load/save config.json. API keys are never logged.

Config location: PCU_CONFIG_DIR env var if set (used when packaged), else the
repo root (dev). If no config exists at the active location but one exists at
the legacy repo-root location, it is copied over so keys are never lost.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LEGACY_CONFIG_PATH = ROOT / "config.json"


def _config_dir() -> Path:
    env_dir = os.environ.get("PCU_CONFIG_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return ROOT


CONFIG_PATH = _config_dir() / "config.json"

DEFAULTS: dict[str, Any] = {
    "provider": "openai",
    "openai": {"api_key": "", "model": "computer-use-preview"},
    "anthropic": {"api_key": "", "model": "claude-3-7-sonnet-latest"},
    "openai_compat": {"base_url": "", "api_key": "", "model": ""},
    "hotkey": "Control+Alt+K",
    "max_steps": 40,
    "action_delay_s": 0.4,
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _legacy_has_key(raw: Any) -> bool:
    """True only when a provider section carries a non-empty api_key."""
    if not isinstance(raw, dict):
        return False
    for section in ("openai", "anthropic", "openai_compat"):
        sub = raw.get(section)
        if isinstance(sub, dict):
            key = sub.get("api_key")
            if isinstance(key, str) and key.strip():
                return True
    return False


def _active_is_default_empty(raw: Any) -> bool:
    """True when no provider section holds a non-empty api_key."""
    return not _legacy_has_key(raw)


def _migrate_legacy() -> None:
    """Copy the legacy repo-root config.json to the active location if needed.

    Runs before any defaults are written. Copies when no active config
    exists yet. Overwrites an existing active config only when it is
    untouched defaults (all api keys empty) and the legacy one holds a real
    key — this repairs installs where defaults were written before
    migration. An empty-key legacy never overwrites an active config that
    has keys.
    """
    if CONFIG_PATH == LEGACY_CONFIG_PATH:
        return
    if not LEGACY_CONFIG_PATH.exists():
        return
    try:
        legacy = json.loads(LEGACY_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    try:
        if not CONFIG_PATH.exists():
            should_copy = True
        else:
            active = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # A corrupt/unreadable active config is treated as empty so a
            # valid legacy config can still rescue it; keys are never logged.
            should_copy = _active_is_default_empty(active) and _legacy_has_key(legacy)
    except (OSError, json.JSONDecodeError):
        should_copy = _legacy_has_key(legacy)
    if not should_copy:
        return
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            LEGACY_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        print(f"[config] migrated legacy config.json -> {CONFIG_PATH}", flush=True)
    except OSError as exc:
        print(f"[config] migration failed: {exc}", flush=True)


def load() -> dict[str, Any]:
    """Read config.json, falling back to defaults for missing/invalid fields."""
    _migrate_legacy()
    defaults = json.loads(json.dumps(DEFAULTS))
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(raw, dict):
        return defaults
    return _merge(defaults, raw)


def save(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
