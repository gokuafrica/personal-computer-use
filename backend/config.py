"""Load/save config.json. API keys are never logged or persisted as plaintext.

Config location: PCU_CONFIG_DIR env var if set (used when packaged), else the
repo root (dev). If no config exists at the active location but one exists at
the legacy repo-root location, it is copied over so keys are never lost.

Secret storage contract (shared with the Electron main process):
- ``apiKeyEncrypted``: base64 DPAPI blob (user scope, no extra entropy) of the
  active provider key. Interoperable with Electron safeStorage on Windows.
- ``keySource``: "bundled" (provisioned with the installer) or "user"
  (explicitly supplied). Rotation never silently overwrites a user key.
  A legacy plaintext key that migrates is provisionally "user"; if it
  equals the provisioned bundled key at consume time it is relabeled
  "bundled" (it WAS the previously shipped installer key), so future
  installer versions rotate normally instead of being blocked forever.
- ``keyVersion``: int or "v1"-style string identifying the provisioned key.
- ``bundledKeyEncrypted``/``bundledKeyVersion``: DPAPI backup of the
  last-known BUNDLED key, maintained by the backend itself. Refreshed when a
  bundled key is adopted (provision consumption, bundled rotation); user
  rotations leave it untouched so a later restore still finds it. This is
  NOT an additional plaintext copy — it lives in the same single
  DPAPI-protected store — and exists so the WS verb   ``restore_bundled``
  ("use built-in key") can restore the bundled key after the installer's
  provision envelope is consumed and deleted.
- Plaintext ``apiKey``/``api_key`` values are NEVER written to disk. On load,
  legacy plaintext is encrypted, the plaintext fields are removed, and the
  result is persisted. The decrypted key is materialized in memory into the
  active provider's ``api_key`` so providers/doctor keep working unchanged.
- Provision file (written by the NSIS installer next to config.json):
  ``provision.json`` = versioned envelope
  {"schema": 1, "keyVersion": <int>, "blob": "<base64 CryptProtectData
  output>", "blob_sha256": "<hex sha256 of the DECODED blob bytes>"}. The
  envelope is validated (schema + blob hash) BEFORE unprotecting; any
  validation failure is deleted and treated as "no provisioned key" (fail
  closed). A legacy two-file pair from older installers (``provision.blob``
  = RAW CryptProtectData output (current user, no extra entropy, no
  base64/prefix) over the same byte form ``secret_store.protect`` produces
  for the key string (UTF-8; UTF-16LE is tolerated on read), and
  ``provision.meta.json`` = {"keyVersion": <int>, "keySource": "bundled"})
  is still consumed when the envelope is absent, but requires BOTH files: a
  one-member pair (the interruption state the old two-rename commit could
  leave behind) is deleted and fails closed. Consumed at load via
  rotate_key semantics: no stored key -> store as bundled; stored bundled
  key with a different keyVersion -> replace; keySource "user" ->
  preserved. After a durable decision (replaced / same / kept) the envelope
  and all legacy names are deleted. On unprotect or encryption failure the
  provision file is RETAINED (retry next launch) and the existing stored
  credential is left untouched. Missing files are ignored; ".tmp" staging
  leftovers of an interrupted installer write are swept on every consume.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from backend import secret_store
from backend import secrets_filter

ROOT = Path(__file__).resolve().parent.parent
LEGACY_CONFIG_PATH = ROOT / "config.json"

KEY_ENC_FIELD = "apiKeyEncrypted"
KEY_SOURCE_FIELD = "keySource"
KEY_VERSION_FIELD = "keyVersion"
BUNDLED_KEY_ENC_FIELD = "bundledKeyEncrypted"
BUNDLED_KEY_VERSION_FIELD = "bundledKeyVersion"
KEY_SOURCE_BUNDLED = "bundled"
KEY_SOURCE_USER = "user"

PROVISION_ENVELOPE_NAME = "provision.json"
PROVISION_ENVELOPE_SCHEMA = 1
PROVISION_BLOB_NAME = "provision.blob"
PROVISION_META_NAME = "provision.meta.json"

# ".tmp" staging names of the installer write (envelope) and of older
# installers (legacy pair); never authoritative, always swept.
_PROVISION_TMP_NAMES = (
    PROVISION_ENVELOPE_NAME + ".tmp",
    PROVISION_BLOB_NAME + ".tmp",
    PROVISION_META_NAME + ".tmp",
)
_PROVISION_FINAL_NAMES = (
    PROVISION_ENVELOPE_NAME,
    PROVISION_BLOB_NAME,
    PROVISION_META_NAME,
)

PROVIDER_SECTIONS = ("openai", "anthropic", "openai_compat")


def _config_dir() -> Path:
    env_dir = os.environ.get("PCU_CONFIG_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return ROOT


CONFIG_PATH = _config_dir() / "config.json"


def _provision_envelope_path() -> Path:
    return CONFIG_PATH.parent / PROVISION_ENVELOPE_NAME


def _provision_blob_path() -> Path:
    return CONFIG_PATH.parent / PROVISION_BLOB_NAME


def _provision_meta_path() -> Path:
    return CONFIG_PATH.parent / PROVISION_META_NAME

DEFAULTS: dict[str, Any] = {
    "provider": "openai",
    "openai": {"api_key": "", "model": "computer-use-preview"},
    "anthropic": {"api_key": "", "model": "claude-3-7-sonnet-latest"},
    "openai_compat": {"base_url": "", "api_key": "", "model": ""},
    "hotkey": "Control+Alt+K",
    "max_steps": 40,
    "action_delay_s": 0.4,
    "saveScreenshots": False,
    KEY_ENC_FIELD: "",
    KEY_SOURCE_FIELD: "",
    KEY_VERSION_FIELD: None,
    BUNDLED_KEY_ENC_FIELD: "",
    BUNDLED_KEY_VERSION_FIELD: None,
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
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
    for section in PROVIDER_SECTIONS:
        sub = raw.get(section)
        if isinstance(sub, dict):
            key = sub.get("api_key")
            if isinstance(key, str) and key.strip():
                return True
    if isinstance(raw.get("apiKey"), str) and raw["apiKey"].strip():
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


def _read_disk() -> dict[str, Any] | None:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _first_plaintext_key(cfg: dict[str, Any]) -> str:
    for section in (str(cfg.get("provider") or ""), *PROVIDER_SECTIONS):
        sub = cfg.get(section)
        if isinstance(sub, dict):
            key = sub.get("api_key")
            if isinstance(key, str) and key.strip():
                return key
    top = cfg.get("apiKey")
    if isinstance(top, str) and top.strip():
        return top
    return ""


def _strip_plaintext_on_disk(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return an on-disk copy with no plaintext key material anywhere."""
    disk = copy.deepcopy(cfg)
    _strip_plaintext_in_place(disk)
    return disk


def _strip_plaintext_in_place(cfg: dict[str, Any]) -> None:
    """Remove plaintext key material from a config dict (mutates)."""
    cfg.pop("apiKey", None)
    for section in PROVIDER_SECTIONS:
        sub = cfg.get(section)
        if isinstance(sub, dict):
            sub["api_key"] = ""


def _encrypt_into(cfg: dict[str, Any]) -> bool:
    """Encrypt any legacy plaintext key into KEY_ENC_FIELD. True if changed."""
    if cfg.get(KEY_ENC_FIELD):
        return False
    plaintext = _first_plaintext_key(cfg)
    if not plaintext:
        return False
    try:
        cfg[KEY_ENC_FIELD] = secret_store.protect(plaintext)
    except OSError:
        return False
    if not cfg.get(KEY_SOURCE_FIELD):
        cfg[KEY_SOURCE_FIELD] = KEY_SOURCE_USER
    if cfg.get(KEY_VERSION_FIELD) is None:
        cfg[KEY_VERSION_FIELD] = 0
    return True


def decrypt_key(cfg: dict[str, Any]) -> str:
    """Decrypt the stored blob; empty string when absent/unreadable."""
    blob = cfg.get(KEY_ENC_FIELD)
    if not isinstance(blob, str) or not blob:
        return ""
    return secret_store.unprotect(blob) or ""


def _materialize_runtime(cfg: dict[str, Any]) -> dict[str, Any]:
    """Put the decrypted key into the ACTIVE provider section (memory only)."""
    key = decrypt_key(cfg)
    provider = str(cfg.get("provider") or "")
    if key and provider:
        sub = cfg.get(provider)
        if isinstance(sub, dict) and not (sub.get("api_key") or "").strip():
            sub["api_key"] = key
    return cfg


def _warn(message: str) -> None:
    """Stdout warning that never carries key material."""
    print(f"[config] {secrets_filter.filter_text(message)}", flush=True)


def _decode_provision_bytes(data: bytes) -> str:
    """Decode decrypted provision bytes the way protect()/unprotect() would.

    Canonical form is the UTF-8 byte string protect() produces; UTF-16LE
    (BOM included) is tolerated so an installer that wrote wide chars still
    round-trips the same key string. UTF-16LE of ASCII text is valid UTF-8
    with embedded NULs, so NUL presence switches to the wide-char decoding.
    """
    try:
        text = data.decode("utf-8")
        if "\x00" not in text:
            return text
    except UnicodeDecodeError:
        pass
    text = data.decode("utf-16-le", errors="replace")
    return text.lstrip("\ufeff").replace("\x00", "")


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _delete_provision_temps() -> None:
    for name in _PROVISION_TMP_NAMES:
        _unlink_quiet(CONFIG_PATH.parent / name)


def _delete_provision_files() -> None:
    """Delete the envelope, the legacy pair, and every .tmp staging name."""
    for name in _PROVISION_FINAL_NAMES + _PROVISION_TMP_NAMES:
        _unlink_quiet(CONFIG_PATH.parent / name)


def _reclassify_migrated_bundled(
    provision_key: str, version: int | str | None
) -> None:
    """Correct keySource for a legacy plaintext that WAS the bundled key.

    A legacy plaintext config written by an older bundled installer carries
    the previously-bundled key, but _encrypt_into() can only label migrated
    keys "user" (it cannot know where the plaintext came from). Left as
    "user" that label would permanently block installer-driven rotation on
    a recipient machine. Marker for the migrated state: keySource "user"
    with keyVersion 0 (the value _encrypt_into assigns when the version is
    unknown). When such a stored key decrypts to EXACTLY the provisioned
    key, it is reclassified as bundled with the meta keyVersion, so future
    installer versions rotate normally. A genuinely user-supplied key never
    matches the provisioned value and stays "user" untouched.
    """
    cfg = _read_disk()
    if cfg is None:
        return
    if str(cfg.get(KEY_SOURCE_FIELD) or "") != KEY_SOURCE_USER:
        return
    stored_version = cfg.get(KEY_VERSION_FIELD)
    if stored_version is None or str(stored_version) != "0":
        return
    if not cfg.get(KEY_ENC_FIELD):
        return
    stored = decrypt_key(cfg)
    if not stored or stored != provision_key:
        return
    cfg[KEY_SOURCE_FIELD] = KEY_SOURCE_BUNDLED
    if version is not None:
        cfg[KEY_VERSION_FIELD] = version
    try:
        save(cfg)
    except (OSError, ConfigWriteError):
        # Leave the decision to a later launch: the provision files are
        # still present and the stored credential is untouched.
        return


_provision_consuming = False


def _consume_provision() -> None:
    """Apply the installer's provision.json envelope per the shared contract.

    Idempotent: a successful durable decision deletes the envelope (and all
    legacy names), so later loads are no-ops while the files are missing.
    Retained on any transient failure so the next launch retries; the
    existing stored credential is never touched by a failed consumption.
    """
    global _provision_consuming
    if _provision_consuming:
        return
    _provision_consuming = True
    try:
        _consume_provision_impl()
    finally:
        _provision_consuming = False


def _consume_provision_impl() -> None:
    # ".tmp" staging leftovers of an interrupted installer write are never
    # authoritative: swept on every consume attempt, before any format
    # dispatch.
    _delete_provision_temps()
    if _provision_envelope_path().exists():
        _consume_provision_envelope()
    else:
        _consume_provision_legacy()


def _consume_provision_envelope() -> None:
    """Consume the versioned provision.json envelope (schema 1).

    The envelope is fully validated (schema, base64 blob, blob_sha256 over
    the DECODED blob bytes) BEFORE any DPAPI call. A validation failure is
    not a transient condition, so the envelope and all legacy staging files
    are deleted and consumption fails closed - the app starts without the
    built-in key, exactly as with a missing provision. Only a transient
    DPAPI unprotect/encryption failure retains the envelope for the next
    launch, mirroring the legacy behavior.
    """
    try:
        raw_text = _provision_envelope_path().read_text(encoding="utf-8")
        envelope = json.loads(raw_text)
    except (OSError, ValueError):
        envelope = None
    if (not isinstance(envelope, dict)
            or envelope.get("schema") != PROVISION_ENVELOPE_SCHEMA
            or not isinstance(envelope.get("blob"), str)
            or not isinstance(envelope.get("blob_sha256"), str)):
        _warn("provision envelope invalid; removed, starting without the "
              "built-in key")
        _delete_provision_files()
        return
    try:
        blob = base64.b64decode(envelope["blob"], validate=True)
    except ValueError:
        _warn("provision envelope blob is not base64; removed, starting "
              "without the built-in key")
        _delete_provision_files()
        return
    if hashlib.sha256(blob).hexdigest() != envelope["blob_sha256"].lower():
        _warn("provision envelope blob_sha256 mismatch; removed, starting "
              "without the built-in key")
        _delete_provision_files()
        return
    try:
        data = secret_store.unprotect_bytes(envelope["blob"])
    except OSError as exc:
        _warn(f"provision envelope present but secure storage unavailable "
              f"({exc}); retained for next launch, existing credential "
              "untouched")
        return
    if data is None or not data:
        _warn("provision envelope blob could not be decrypted; retained for "
              "next launch, existing credential untouched")
        return
    key = _decode_provision_bytes(data).strip()
    if not key:
        _warn("provision envelope decoded to an empty key; retained for "
              "next launch, existing credential untouched")
        return
    version = envelope.get("keyVersion")
    if isinstance(version, bool) or not isinstance(version, (int, str)):
        version = None
    _adopt_provision_key(key, version)


def _consume_provision_legacy() -> None:
    """Consume the legacy two-file pair of older installers.

    Backward compatibility only: the pair requires BOTH files. A pair with
    one member missing is exactly the interruption state the old two-rename
    commit could leave behind (a NEW blob paired with OLD metadata distorts
    later rotation logic), so it is deleted and consumption fails closed -
    the app starts without the built-in key. A transient DPAPI
    unprotect/encryption failure retains the pair for the next launch, as
    before.
    """
    blob_path = _provision_blob_path()
    meta_path = _provision_meta_path()
    if not blob_path.exists() and not meta_path.exists():
        return  # no legacy provision: no-op
    if not blob_path.exists() or not meta_path.exists():
        _warn("legacy provision pair incomplete; removed, starting without "
              "the built-in key")
        _delete_provision_files()
        return
    try:
        raw = blob_path.read_bytes()
    except OSError:
        _warn("legacy provision blob unreadable; removed, starting without "
              "the built-in key")
        _delete_provision_files()
        return
    try:
        data = secret_store.unprotect_bytes(
            base64.b64encode(raw).decode("ascii")
        )
    except OSError as exc:
        _warn(f"provision.blob present but secure storage unavailable ({exc}); "
              "retained for next launch, existing credential untouched")
        return
    if data is None or not data:
        _warn("provision.blob could not be decrypted; retained for next "
              "launch, existing credential untouched")
        return
    key = _decode_provision_bytes(data).strip()
    if not key:
        _warn("provision.blob decoded to an empty key; retained for next "
              "launch, existing credential untouched")
        return
    version: int | str | None = None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(meta, dict) and meta.get("keyVersion") is not None:
            version = meta["keyVersion"]
    except (OSError, ValueError):
        # Undecodable meta next to a decryptable blob: fail closed.
        _warn("legacy provision metadata undecodable; removed, starting "
              "without the built-in key")
        _delete_provision_files()
        return
    _adopt_provision_key(key, version)


def _adopt_provision_key(key: str, version: int | str | None) -> None:
    """Rotate the provisioned key in and, once durable, sweep all files."""
    # A just-migrated legacy plaintext whose value equals the provisioned
    # key was the previously-bundled key: relabel it bundled so rotation
    # proceeds. Genuinely user-supplied keys differ and stay "user".
    _reclassify_migrated_bundled(key, version)
    try:
        outcome = rotate_key(key, version, KEY_SOURCE_BUNDLED)
    except OSError:
        # Failed protection during rotation: previous encrypted key intact,
        # provision files retained for the next launch.
        _warn("provision key could not be encrypted; retained for next "
              "launch, existing credential untouched")
        return
    if outcome in ("replaced", "same", "kept"):
        # Durable decision reached (and persisted, or already reflected in
        # the stored config): the seed must not linger on disk.
        _delete_provision_files()


def _load_merged() -> dict[str, Any]:
    """Defaults merged with the on-disk config (or just defaults)."""
    defaults = copy.deepcopy(DEFAULTS)
    raw = _read_disk()
    return _merge(defaults, raw) if raw is not None else defaults


def load() -> dict[str, Any]:
    """Read config.json, falling back to defaults for missing/invalid fields.

    Legacy plaintext keys are migrated to DPAPI storage on first load: the
    plaintext is encrypted into ``apiKeyEncrypted``, removed from the disk
    copy, and the sanitized file is persisted. The returned dict holds the
    decrypted key ONLY in memory (active provider section).

    Fail-closed: when DPAPI protection is unavailable and a legacy plaintext
    key exists with no encrypted copy yet, the disk file is left byte-identical
    (no rewrite), the plaintext stays materialized in memory so the app keeps
    working, and a redacted warning is printed. Pre-existing plaintext on
    disk in this path is pre-existing state, not newly-persisted plaintext —
    the guarantee is that no NEW plaintext is written and no credential is
    destroyed.

    Provision consumption runs once the legacy-migration state is settled;
    the merged view is re-read afterwards so a freshly provisioned/rotated
    key is returned to the caller.
    """
    _migrate_legacy()
    merged = _load_merged()
    _encrypt_into(merged)
    plaintext = _first_plaintext_key(merged)
    if plaintext and merged.get(KEY_ENC_FIELD):
        # Encrypted copy secured: strip plaintext everywhere and persist.
        _strip_plaintext_in_place(merged)
        save(merged)
        _consume_provision()
        merged = _load_merged()
    elif plaintext:
        # Protection failed and there is no encrypted copy: never strip the
        # in-memory key and never rewrite the file (that would destroy the
        # credential). Provision consumption waits for the next launch.
        _warn("secure storage unavailable; legacy plaintext config.json left "
              "as-is, plaintext key kept in memory only")
    else:
        _consume_provision()
        merged = _load_merged()
    return _materialize_runtime(merged)


class ConfigWriteError(RuntimeError):
    """Raised when a save would rewrite a key-bearing config without any
    credential (encryption unavailable and no existing encrypted blob)."""


def save(cfg: dict[str, Any]) -> None:
    """Persist config with NO plaintext key (sections keep empty api_key).

    Any plaintext key present in ``cfg`` but not yet encrypted is encrypted
    first so save() never loses the key it is stripping. Fail-closed: if the
    write would carry no credential at all (no ``apiKeyEncrypted`` and
    plaintext that cannot be protected), the write is REFUSED by raising
    ConfigWriteError — a config that previously contained a credential is
    never rewritten credential-less, and no new plaintext is persisted.
    A config that never had a key (fresh defaults) still saves normally.
    Provision files are never written by save(); they live as
    sibling files and are deleted after consumption.
    """
    _encrypt_into(cfg)
    disk = _strip_plaintext_on_disk(cfg)
    if not disk.get(KEY_ENC_FIELD) and _first_plaintext_key(cfg):
        raise ConfigWriteError(
            "secure storage unavailable; refusing to write config.json "
            "without the credential"
        )
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(disk, indent=2), encoding="utf-8"
    )


def _store_bundled_backup(
    cfg: dict[str, Any], key: str, version: int | str | None
) -> None:
    """DPAPI-backup the last-known bundled key in the same protected store.

    Why: the installer's provision envelope is deleted after its first
    durable consumption, so nothing on disk would otherwise remember the
    bundled key for the "use built-in key" action (WS verb restore_bundled). The
    backup is a DPAPI blob — the same protection as apiKeyEncrypted, never
    plaintext — refreshed only when a bundled key is adopted; user
    rotations leave it untouched so a later restore still finds it.
    """
    cfg[BUNDLED_KEY_ENC_FIELD] = secret_store.protect(key)
    cfg[BUNDLED_KEY_VERSION_FIELD] = version


def rotate_key(
    new_key: str,
    new_version: int | str | None = None,
    key_source: str = KEY_SOURCE_BUNDLED,
) -> str:
    """Provision/rotate the stored key. Returns "replaced" | "kept" | "same".

    - keySource "user" and a stored key: NEVER overwritten by a BUNDLED
      rotation ("kept") — installer-driven rotation must not clobber it.
    - keySource "user" incoming: an explicit user action (Settings), so it
      replaces whatever is stored (identical value -> "same").
    - keySource "bundled" incoming on a stored bundled key: replaced when
      the incoming keyVersion differs from the stored one (or there is no
      stored key yet).

    A successful BUNDLED adoption additionally stores the same key under
    ``bundledKeyEncrypted`` (+ ``bundledKeyVersion``): see
    _store_bundled_backup. User rotations leave that backup untouched.
    """
    new_key = (new_key or "").strip()
    if not new_key:
        return "kept"
    cfg = load()
    stored = decrypt_key(cfg)
    source = str(cfg.get(KEY_SOURCE_FIELD) or "")
    stored_version = cfg.get(KEY_VERSION_FIELD)
    if stored:
        if source == KEY_SOURCE_USER and key_source != KEY_SOURCE_USER:
            return "kept"
        if source == KEY_SOURCE_USER and key_source == KEY_SOURCE_USER \
                and stored == new_key:
            return "same"
        if source == KEY_SOURCE_BUNDLED and key_source == KEY_SOURCE_BUNDLED:
            if stored_version is not None and new_version is not None \
                    and str(stored_version) == str(new_version):
                # Same bundled key as stored. Heal a missing backup
                # (installs provisioned before bundledKeyEncrypted existed);
                # otherwise this is a no-op.
                if not cfg.get(BUNDLED_KEY_ENC_FIELD):
                    _store_bundled_backup(cfg, new_key, stored_version)
                    save(cfg)
                return "same"
    cfg[KEY_ENC_FIELD] = secret_store.protect(new_key)
    if new_version is not None:
        cfg[KEY_VERSION_FIELD] = new_version
    cfg[KEY_SOURCE_FIELD] = (
        key_source if key_source else str(cfg.get(KEY_SOURCE_FIELD) or KEY_SOURCE_USER)
    )
    if key_source == KEY_SOURCE_BUNDLED:
        _store_bundled_backup(cfg, new_key, new_version)
    save(cfg)
    return "replaced"


def restore_bundled() -> dict[str, Any]:
    """Restore the last-known bundled key as the active credential.

    Used by the "use built-in key" action (WS verb restore_bundled). This is
    an explicit user action, so replacing a stored user key is allowed.

    Fail-closed: when no backup exists, the backup cannot be decrypted, or
    persistence fails, the currently stored credential is left untouched
    and the failure is reported as {"ok": False, "error": <reason>}. The
    error text is safe to surface and never contains key material.
    """
    cfg = load()
    backup = cfg.get(BUNDLED_KEY_ENC_FIELD)
    if not isinstance(backup, str) or not backup:
        return {"ok": False,
                "error": "no built-in key stored on this installation"}
    if not secret_store.unprotect(backup):
        return {"ok": False,
                "error": "the stored built-in key is unreadable; "
                         "current key kept"}
    cfg[KEY_ENC_FIELD] = backup
    cfg[KEY_SOURCE_FIELD] = KEY_SOURCE_BUNDLED
    cfg[KEY_VERSION_FIELD] = cfg.get(BUNDLED_KEY_VERSION_FIELD)
    try:
        save(cfg)
    except (OSError, ConfigWriteError) as exc:
        return {"ok": False,
                "error": f"could not persist the built-in key: {exc}"}
    return {"ok": True, "error": None}


def key_status() -> dict[str, Any]:
    """Diagnostic view of the key store: never contains key material."""
    cfg = _read_disk() or {}
    blob = cfg.get(KEY_ENC_FIELD)
    plaintext = _first_plaintext_key(cfg)
    return {
        "configured": bool(cfg.get(KEY_ENC_FIELD)) or bool(plaintext),
        "encrypted": bool(cfg.get(KEY_ENC_FIELD)),
        "legacyPlaintext": bool(plaintext),
        "keySource": str(cfg.get(KEY_SOURCE_FIELD) or ""),
        "keyVersion": cfg.get(KEY_VERSION_FIELD),
        "hasBundledBackup": bool(cfg.get(BUNDLED_KEY_ENC_FIELD)),
        "provider": str(cfg.get("provider") or ""),
    }
