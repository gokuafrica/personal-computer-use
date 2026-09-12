"""Unit tests for the security-hardening changes (Findings 3-6, backend side).

Covers:
- DPAPI secret_store roundtrip (Windows only)
- config legacy-plaintext migration (plaintext removed from disk)
- key rotation (bundled replace, user preserved, same-version no-op)
- secrets_filter redaction (exact key, patterns, nested structures)
- WS token/origin rejection logic (pure function)
- screenshot persistence gating and raw-provider-dump gating

Run:  python -m unittest backend.tests.test_security -v
Fake keys only (never touch real config.json contents).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

FAKE_KEY_A = "sk-test-fake-key-a-1234567890abcdef"
FAKE_KEY_B = "sk-test-fake-key-b-fedcba0987654321"
FAKE_KEY_C = "sk-synthetic-provision-key-0123456789abcdef"
OPAQUE_SECRET = "zebra-opaque-secret-value-not-a-pattern"
# Arbitrary typed credential: NOT registered with the filter and NOT shaped
# like any token pattern, so only construction-time stripping protects it.
TYPED_SECRET = "Sup3rSecret!PIN"


class _BreakProtect:
    """Context manager: secret_store.protect raises (DPAPI unavailable)."""

    def __enter__(self) -> "_BreakProtect":
        from backend import secret_store

        self._secret_store = secret_store
        self._orig = secret_store.protect

        def _fail(_plaintext: str) -> str:
            raise OSError("synthetic DPAPI protection failure")

        secret_store.protect = _fail
        return self

    def __exit__(self, *_exc: object) -> None:
        self._secret_store.protect = self._orig


def _temp_config(monkey: "ConfigPatcher") -> Path:
    """Point config_mod at an isolated temp dir; returns the config path."""
    import backend.config as config_mod

    tmp = Path(tempfile.mkdtemp(prefix="pcu-test-config-"))
    monkey.config_path = tmp / "config.json"
    monkey.legacy_path = tmp / "legacy-config.json"
    return monkey.config_path


def _write_envelope(
    tmp: Path, key: str, version: int | str | None, *, blob: bytes | None = None
) -> bytes:
    """Write a provision.json envelope for a SYNTHETIC key.

    Returns the raw (decoded) DPAPI blob bytes. ``blob`` overrides the
    protected bytes of ``key`` (used to build self-consistent garbage whose
    sha256 matches but which DPAPI cannot decrypt).
    """
    import base64
    import hashlib

    from backend import secret_store

    raw = blob if blob is not None else base64.b64decode(secret_store.protect(key))
    envelope = {
        "schema": 1,
        "keyVersion": version,
        "blob": base64.b64encode(raw).decode("ascii"),
        "blob_sha256": hashlib.sha256(raw).hexdigest(),
    }
    (tmp / "provision.json").write_text(json.dumps(envelope), encoding="utf-8")
    return raw


class ConfigPatcher:
    def __init__(self, case: unittest.TestCase) -> None:
        import backend.config as config_mod

        self.case = case
        self.config_mod = config_mod
        self.old_config = config_mod.CONFIG_PATH
        self.old_legacy = config_mod.LEGACY_CONFIG_PATH

    def __enter__(self) -> "ConfigPatcher":
        return self

    def apply(self, config_path: Path, legacy_path: Path) -> None:
        self.config_path = config_path
        self.legacy_path = legacy_path
        self.config_mod.CONFIG_PATH = config_path
        self.config_mod.LEGACY_CONFIG_PATH = legacy_path

    def __exit__(self, *_exc: object) -> None:
        self.config_mod.CONFIG_PATH = self.old_config
        self.config_mod.LEGACY_CONFIG_PATH = self.old_legacy


class SecretStoreTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_roundtrip(self):
        from backend import secret_store

        blob = secret_store.protect(FAKE_KEY_A)
        self.assertIsInstance(blob, str)
        self.assertNotIn(FAKE_KEY_A, blob)
        self.assertEqual(secret_store.unprotect(blob), FAKE_KEY_A)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_unprotect_garbage_returns_none(self):
        from backend import secret_store

        self.assertIsNone(secret_store.unprotect("not-a-blob!!"))


class ConfigMigrationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_legacy_plaintext_migrated_and_removed(self):
        with ConfigPatcher(self) as patcher:
            tmp = Path(tempfile.mkdtemp(prefix="pcu-test-migrate-"))
            patcher.apply(tmp / "config.json", tmp / "legacy.json")
            (patcher.config_path).write_text(json.dumps({
                "provider": "openai_compat",
                "openai_compat": {
                    "api_key": FAKE_KEY_A, "base_url": "https://example.test",
                    "model": "m",
                },
            }), encoding="utf-8")

            cfg = patcher.config_mod.load()

            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertNotIn(FAKE_KEY_A, json.dumps(disk))
            self.assertTrue(disk.get("apiKeyEncrypted"))
            self.assertEqual(disk["openai_compat"]["api_key"], "")
            self.assertEqual(disk["keySource"], "user")
            # Runtime view materializes the decrypted key in memory.
            self.assertEqual(cfg["openai_compat"]["api_key"], FAKE_KEY_A)
            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_A)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_save_never_writes_plaintext(self):
        with ConfigPatcher(self) as patcher:
            tmp = Path(tempfile.mkdtemp(prefix="pcu-test-save-"))
            patcher.apply(tmp / "config.json", tmp / "legacy.json")
            patcher.config_mod.save({
                "provider": "openai",
                "openai": {"api_key": FAKE_KEY_A, "model": "m"},
            })
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertNotIn(FAKE_KEY_A, json.dumps(disk))
            self.assertEqual(disk["openai"]["api_key"], "")
            self.assertTrue(disk.get("apiKeyEncrypted"))

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_key_status_view_has_no_key_material(self):
        with ConfigPatcher(self) as patcher:
            tmp = Path(tempfile.mkdtemp(prefix="pcu-test-status-"))
            patcher.apply(tmp / "config.json", tmp / "legacy.json")
            patcher.config_mod.save({
                "provider": "openai",
                "openai": {"api_key": FAKE_KEY_A, "model": "m"},
                "keySource": "bundled",
                "keyVersion": 3,
            })
            status = patcher.config_mod.key_status()
            self.assertTrue(status["configured"])
            self.assertTrue(status["encrypted"])
            self.assertEqual(status["keySource"], "bundled")
            self.assertEqual(status["keyVersion"], 3)
            self.assertNotIn(FAKE_KEY_A, json.dumps(status))


class RotationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def _seed_bundled(self, patcher: ConfigPatcher, key: str, version: int) -> None:
        patcher.config_mod.save({
            "provider": "openai",
            "openai": {"api_key": "", "model": "m"},
            "apiKeyEncrypted": patcher.config_mod.secret_store.protect(key),
            "keySource": "bundled",
            "keyVersion": version,
        })

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_bundled_rotation_replaces_key(self):
        with ConfigPatcher(self) as patcher:
            tmp = Path(tempfile.mkdtemp(prefix="pcu-test-rot-"))
            patcher.apply(tmp / "config.json", tmp / "legacy.json")
            self._seed_bundled(patcher, FAKE_KEY_A, 1)

            outcome = patcher.config_mod.rotate_key(FAKE_KEY_B, 2)

            self.assertEqual(outcome, "replaced")
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertEqual(disk["keyVersion"], 2)
            self.assertEqual(disk["keySource"], "bundled")
            self.assertNotIn(FAKE_KEY_A, json.dumps(disk))
            cfg = patcher.config_mod.load()
            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_B)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_user_key_never_overwritten(self):
        with ConfigPatcher(self) as patcher:
            tmp = Path(tempfile.mkdtemp(prefix="pcu-test-rot2-"))
            patcher.apply(tmp / "config.json", tmp / "legacy.json")
            self._seed_bundled(patcher, FAKE_KEY_A, 1)
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            disk["keySource"] = "user"
            patcher.config_path.write_text(json.dumps(disk), encoding="utf-8")

            outcome = patcher.config_mod.rotate_key(FAKE_KEY_B, 2)

            self.assertEqual(outcome, "kept")
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            cfg = patcher.config_mod.load()
            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_A)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_bundled_same_version_is_noop(self):
        with ConfigPatcher(self) as patcher:
            tmp = Path(tempfile.mkdtemp(prefix="pcu-test-rot3-"))
            patcher.apply(tmp / "config.json", tmp / "legacy.json")
            self._seed_bundled(patcher, FAKE_KEY_A, 5)

            outcome = patcher.config_mod.rotate_key(FAKE_KEY_B, "5")

            self.assertEqual(outcome, "same")
            cfg = patcher.config_mod.load()
            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_A)


class SecretsFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from backend import secrets_filter

        secrets_filter.clear_secrets()
        secrets_filter.register_secret(FAKE_KEY_A)
        secrets_filter.register_secret(OPAQUE_SECRET)

    @classmethod
    def tearDownClass(cls) -> None:
        from backend import secrets_filter

        secrets_filter.clear_secrets()

    def test_exact_secret_redacted(self):
        from backend import secrets_filter

        text = f"using key {FAKE_KEY_A} and also {OPAQUE_SECRET} inside"
        out = secrets_filter.redact_text(text)
        self.assertNotIn(FAKE_KEY_A, out)
        self.assertNotIn(OPAQUE_SECRET, out)
        self.assertIn("[REDACTED]", out)

    def test_common_token_patterns_redacted(self):
        from backend import secrets_filter

        cases = [
            "sk-proj-abcdefghijklmnopqrstuvwx",
            "ghp_" + "a" * 36,
            "AKIA" + "IOSFODNN7EXAMPLEX"[:16],
            "xoxb-123456789-abcdef",
            "Authorization: Bearer abcdef0123456789abcdef01",
        ]
        for text in cases:
            out = secrets_filter.redact_text(text)
            self.assertNotEqual(out, text, f"not redacted: {text!r}")
            self.assertIn("[REDACTED]", out)

    def test_pem_block_redacted(self):
        from backend import secrets_filter

        pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nmore\n"
               "-----END RSA PRIVATE KEY-----")
        out = secrets_filter.redact_text(f"key follows: {pem}")
        self.assertNotIn("PRIVATE KEY-----\nMIIEow", out)
        self.assertIn("[REDACTED]", out)

    def test_long_token_near_keylike_name_redacted(self):
        from backend import secrets_filter

        long_hex = "a1b2c3d4" * 8
        out = secrets_filter.redact_text(f'api_key = "{long_hex}"')
        self.assertNotIn(long_hex, out)
        out2 = secrets_filter.redact_text(f'"token": "{long_hex}"')
        self.assertNotIn(long_hex, out2)

    def test_nested_structure_redacted_json_safe(self):
        from backend import secrets_filter

        record = {
            "type": "log",
            "line": f"Task started: {FAKE_KEY_A}",
            "nested": {"auth": f"Bearer {FAKE_KEY_A}",
                       "list": [OPAQUE_SECRET, 1, True, None]},
        }
        out = secrets_filter.redact_obj(record)
        dumped = json.dumps(out)
        self.assertNotIn(FAKE_KEY_A, dumped)
        self.assertNotIn(OPAQUE_SECRET, dumped)
        self.assertEqual(out["nested"]["list"][1], 1)
        self.assertIs(out["nested"]["list"][2], True)
        self.assertIsNone(out["nested"]["list"][3])

    def test_non_string_passthrough(self):
        from backend import secrets_filter

        self.assertEqual(secrets_filter.redact_text(42), 42)
        self.assertEqual(secrets_filter.redact_obj(3.5), 3.5)

    def test_base64_encoded_forms_redacted(self):
        import base64

        from backend import secrets_filter

        b64 = base64.b64encode(FAKE_KEY_A.encode("utf-8")).decode("ascii")
        b64le = base64.b64encode(FAKE_KEY_A.encode("utf-16-le")).decode("ascii")
        self.assertNotEqual(b64, b64le)
        out = secrets_filter.redact_text(f"encoded: {b64} :: {b64le}")
        self.assertNotIn(b64, out)
        self.assertNotIn(b64le, out)
        self.assertIn("[REDACTED]", out)

    def test_base64_of_short_key_redacted(self):
        import base64

        from backend import secrets_filter

        key = "sk-test-fake"
        self.assertEqual(len(key), 12)
        b64 = base64.b64encode(key.encode("utf-8")).decode("ascii")
        self.assertGreaterEqual(len(b64), 16)
        secrets_filter.clear_secrets()
        try:
            secrets_filter.register_secret(key)
            out = secrets_filter.redact_text(f"encoded: {b64}")
            self.assertNotIn(b64, out)
            self.assertIn("[REDACTED]", out)
        finally:
            secrets_filter.register_secret(FAKE_KEY_A)
            secrets_filter.register_secret(OPAQUE_SECRET)

    def test_json_escaped_form_redacted(self):
        from backend import secrets_filter

        key = "sk-t\u00ebst-fake-key-value"
        escaped = json.dumps(key, ensure_ascii=True)[1:-1]
        self.assertNotEqual(escaped, key)
        secrets_filter.clear_secrets()
        try:
            secrets_filter.register_secret(key)
            out = secrets_filter.redact_text(f'{{"apiKey": "{escaped}"}}')
            self.assertNotIn(escaped, out)
            self.assertNotIn(key, out)
            self.assertIn("[REDACTED]", out)
        finally:
            secrets_filter.register_secret(FAKE_KEY_A)
            secrets_filter.register_secret(OPAQUE_SECRET)

    def test_short_derived_forms_not_registered(self):
        import base64

        from backend import secrets_filter

        key = "ab"  # base64 "YWI=" is tiny; must not pollute the filter
        secrets_filter.clear_secrets()
        try:
            secrets_filter.register_secret(key)
            self.assertEqual(
                secrets_filter.redact_text("YWI= YQBi plain"), "YWI= YQBi plain"
            )
        finally:
            secrets_filter.register_secret(FAKE_KEY_A)
            secrets_filter.register_secret(OPAQUE_SECRET)


class WsAuthTests(unittest.TestCase):
    def test_valid_token_ok(self):
        from backend.main import check_ws_auth

        ok, _ = check_ws_auth(None, "tok", "tok")
        self.assertTrue(ok)

    def test_missing_or_wrong_token_rejected(self):
        from backend.main import check_ws_auth

        self.assertFalse(check_ws_auth(None, None, "tok")[0])
        self.assertFalse(check_ws_auth(None, "", "tok")[0])
        self.assertFalse(check_ws_auth(None, "wrong", "tok")[0])
        self.assertFalse(check_ws_auth(None, "tok", "")[0])

    def test_foreign_browser_origin_rejected_even_with_token(self):
        from backend.main import check_ws_auth

        ok, reason = check_ws_auth("http://evil.example", "tok", "tok")
        self.assertFalse(ok)
        self.assertIn("origin", reason)

    def test_own_and_nonbrowser_origins_ok(self):
        from backend.main import check_ws_auth

        self.assertTrue(check_ws_auth("http://127.0.0.1:8765", "tok", "tok")[0])
        self.assertTrue(check_ws_auth("file://", "tok", "tok")[0])
        self.assertTrue(check_ws_auth("null", "tok", "tok")[0])
        self.assertTrue(check_ws_auth("", "tok", "tok")[0])


class TrajectoryFilteringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from backend import secrets_filter

        secrets_filter.clear_secrets()
        secrets_filter.register_secret(FAKE_KEY_A)
        secrets_filter.register_secret(OPAQUE_SECRET)

    @classmethod
    def tearDownClass(cls) -> None:
        from backend import secrets_filter

        secrets_filter.clear_secrets()

    def test_screenshot_persistence_gated(self):
        from backend.trajectory import TrajectoryRecorder

        tmp = Path(tempfile.mkdtemp(prefix="pcu-test-traj-"))
        rec = TrajectoryRecorder("t1", "instruction", root=tmp, save_screenshots=False)
        rec.save_screenshot(b"png", 1)
        self.assertFalse(list(tmp.glob("*")))
        rec.save_screenshots = True
        rec.save_screenshot(b"png", 1)
        self.assertTrue(list(tmp.glob("*/step_01.png")))

    def test_instruction_redacted_in_task_json(self):
        from backend.trajectory import TrajectoryRecorder

        tmp = Path(tempfile.mkdtemp(prefix="pcu-test-traj2-"))
        rec = TrajectoryRecorder("t1", f"type {FAKE_KEY_A}", root=tmp)
        rec.save_event({"type": "log", "line": f"seen {OPAQUE_SECRET}"})
        run_dir = rec._ensure_dir()
        task = json.loads((run_dir / "task.json").read_text(encoding="utf-8"))
        self.assertNotIn(FAKE_KEY_A, task["instruction"])
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(OPAQUE_SECRET, events)
        self.assertNotIn(FAKE_KEY_A, events)


class ProviderDumpTests(unittest.TestCase):
    def test_raw_dump_off_by_default(self):
        os.environ.pop("PCU_RAW_PROVIDER_DUMP", None)
        tmp = Path(tempfile.mkdtemp(prefix="pcu-test-dump1-"))
        old = os.environ.get("PCU_TRAJECTORY_DIR")
        os.environ["PCU_TRAJECTORY_DIR"] = str(tmp)
        try:
            from backend.providers import openai_compat

            openai_compat._DEBUG_REPORTED.clear()
            openai_compat._dump_raw("parse_fail", {"content": FAKE_KEY_A})
            self.assertFalse(list(tmp.glob("**/*")))
        finally:
            if old is None:
                os.environ.pop("PCU_TRAJECTORY_DIR", None)
            else:
                os.environ["PCU_TRAJECTORY_DIR"] = old

    def test_raw_dump_opt_in_redacts(self):
        tmp = Path(tempfile.mkdtemp(prefix="pcu-test-dump2-"))
        old = os.environ.get("PCU_TRAJECTORY_DIR")
        old_dump = os.environ.get("PCU_RAW_PROVIDER_DUMP")
        os.environ["PCU_TRAJECTORY_DIR"] = str(tmp)
        os.environ["PCU_RAW_PROVIDER_DUMP"] = "1"
        try:
            from backend.providers import openai_compat

            openai_compat._DEBUG_REPORTED.clear()
            openai_compat._dump_raw("parse_fail", {"content": f"leak {FAKE_KEY_A}"})
            files = list(tmp.glob("_provider_debug/raw_replies.jsonl"))
            self.assertEqual(len(files), 1)
            body = files[0].read_text(encoding="utf-8")
            self.assertNotIn(FAKE_KEY_A, body)
        finally:
            if old is None:
                os.environ.pop("PCU_TRAJECTORY_DIR", None)
            else:
                os.environ["PCU_TRAJECTORY_DIR"] = old
            if old_dump is None:
                os.environ.pop("PCU_RAW_PROVIDER_DUMP", None)
            else:
                os.environ["PCU_RAW_PROVIDER_DUMP"] = old_dump


class RuntimeTokenAclTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows ACLs")
    def test_restrict_dacl_to_owner(self):
        from backend.main import _restrict_dacl_to_owner

        tmp = Path(tempfile.mkdtemp(prefix="pcu-test-acl-"))
        path = tmp / "tokenfile"
        path.write_text("data", encoding="utf-8")
        self.assertTrue(_restrict_dacl_to_owner(path))
        # File stays readable/writable by the owner after hardening.
        path.write_text("data2", encoding="utf-8")
        self.assertEqual(path.read_text(encoding="utf-8"), "data2")


class WsIntegrationTests(unittest.TestCase):
    """Live handshake against a real ephemeral-port server."""

    async def _assert_closed_1008(self, ws: Any) -> None:
        import asyncio

        import websockets

        try:
            await asyncio.wait_for(ws.recv(), timeout=5)
            self.fail("connection should have been rejected")
        except websockets.exceptions.ConnectionClosed as exc:
            rcvd = exc.rcvd
            self.assertIsNotNone(rcvd)
            self.assertEqual(rcvd.code, 1008)

    def test_missing_token_rejected(self):
        import asyncio

        import websockets

        from backend.main import Backend

        async def scenario() -> None:
            with ConfigPatcher(self) as patcher:
                tmp = Path(tempfile.mkdtemp(prefix="pcu-test-ws-"))
                patcher.apply(tmp / "config.json", tmp / "legacy.json")
                backend = Backend()
                backend.issue_runtime_token()
                async with websockets.serve(backend.handler, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                        await self._assert_closed_1008(ws)

        asyncio.run(scenario())

    @unittest.skipUnless(sys.platform == "win32", "token file lives in config dir")
    def test_valid_token_accepted(self):
        import asyncio

        import websockets

        from backend.main import Backend

        async def scenario() -> None:
            with ConfigPatcher(self) as patcher:
                tmp = Path(tempfile.mkdtemp(prefix="pcu-test-ws2-"))
                patcher.apply(tmp / "config.json", tmp / "legacy.json")
                backend = Backend()
                token = backend.issue_runtime_token()
                async with websockets.serve(backend.handler, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    async with websockets.connect(
                        f"ws://127.0.0.1:{port}",
                        additional_headers={"X-PCU-Token": token},
                    ) as ws:
                        first = await asyncio.wait_for(ws.recv(), timeout=5)
                        self.assertEqual(json.loads(first).get("type"), "status")
                backend.clear_runtime_token()
                self.assertFalse((tmp / "runtime_token").exists())

        asyncio.run(scenario())

    @unittest.skipUnless(sys.platform == "win32", "token file lives in config dir")
    def test_foreign_browser_origin_rejected_with_token(self):
        import asyncio

        import websockets

        from backend.main import Backend

        async def scenario() -> None:
            with ConfigPatcher(self) as patcher:
                tmp = Path(tempfile.mkdtemp(prefix="pcu-test-ws3-"))
                patcher.apply(tmp / "config.json", tmp / "legacy.json")
                backend = Backend()
                token = backend.issue_runtime_token()
                async with websockets.serve(backend.handler, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    async with websockets.connect(
                        f"ws://127.0.0.1:{port}",
                        additional_headers={"X-PCU-Token": token,
                                            "Origin": "http://evil.example"},
                    ) as ws:
                        await self._assert_closed_1008(ws)

        asyncio.run(scenario())


class WsPreHandshakeTests(unittest.TestCase):
    """GAP 3: process_request rejects before the HTTP 101 completes.

    The in-handler defense-in-depth path is covered by WsIntegrationTests
    (which serves without process_request and still closes with 1008).
    """

    def _serve(self, backend: Any) -> Any:
        import websockets

        return websockets.serve(
            backend.handler, "127.0.0.1", 0, process_request=backend.process_request
        )

    @unittest.skipUnless(sys.platform == "win32", "token file lives in config dir")
    def test_valid_token_handshake_completes(self):
        import asyncio

        import websockets

        from backend.main import Backend

        async def scenario() -> None:
            with ConfigPatcher(self) as patcher:
                tmp = Path(tempfile.mkdtemp(prefix="pcu-test-wsph1-"))
                patcher.apply(tmp / "config.json", tmp / "legacy.json")
                backend = Backend()
                token = backend.issue_runtime_token()
                async with self._serve(backend) as server:
                    port = server.sockets[0].getsockname()[1]
                    async with websockets.connect(
                        f"ws://127.0.0.1:{port}",
                        additional_headers={"X-PCU-Token": token},
                    ) as ws:
                        first = await asyncio.wait_for(ws.recv(), timeout=5)
                        self.assertEqual(json.loads(first).get("type"), "status")

        asyncio.run(scenario())

    @unittest.skipUnless(sys.platform == "win32", "token file lives in config dir")
    def test_missing_token_rejected_pre_handshake(self):
        import asyncio

        import websockets

        from backend.main import Backend

        async def scenario() -> None:
            with ConfigPatcher(self) as patcher:
                tmp = Path(tempfile.mkdtemp(prefix="pcu-test-wsph2-"))
                patcher.apply(tmp / "config.json", tmp / "legacy.json")
                backend = Backend()
                backend.issue_runtime_token()
                async with self._serve(backend) as server:
                    port = server.sockets[0].getsockname()[1]
                    with self.assertRaises(websockets.exceptions.InvalidStatus) as ctx:
                        async with websockets.connect(f"ws://127.0.0.1:{port}"):
                            pass
                    self.assertEqual(ctx.exception.response.status_code, 401)

        asyncio.run(scenario())

    @unittest.skipUnless(sys.platform == "win32", "token file lives in config dir")
    def test_wrong_token_rejected_pre_handshake(self):
        import asyncio

        import websockets

        from backend.main import Backend

        async def scenario() -> None:
            with ConfigPatcher(self) as patcher:
                tmp = Path(tempfile.mkdtemp(prefix="pcu-test-wsph3-"))
                patcher.apply(tmp / "config.json", tmp / "legacy.json")
                backend = Backend()
                backend.issue_runtime_token()
                async with self._serve(backend) as server:
                    port = server.sockets[0].getsockname()[1]
                    with self.assertRaises(websockets.exceptions.InvalidStatus) as ctx:
                        async with websockets.connect(
                            f"ws://127.0.0.1:{port}",
                            additional_headers={"X-PCU-Token": "wrong-token-value"},
                        ):
                            pass
                    self.assertEqual(ctx.exception.response.status_code, 401)

        asyncio.run(scenario())

    @unittest.skipUnless(sys.platform == "win32", "token file lives in config dir")
    def test_foreign_browser_origin_rejected_pre_handshake(self):
        import asyncio

        import websockets

        from backend.main import Backend

        async def scenario() -> None:
            with ConfigPatcher(self) as patcher:
                tmp = Path(tempfile.mkdtemp(prefix="pcu-test-wsph4-"))
                patcher.apply(tmp / "config.json", tmp / "legacy.json")
                backend = Backend()
                token = backend.issue_runtime_token()
                async with self._serve(backend) as server:
                    port = server.sockets[0].getsockname()[1]
                    with self.assertRaises(websockets.exceptions.InvalidStatus) as ctx:
                        async with websockets.connect(
                            f"ws://127.0.0.1:{port}",
                            additional_headers={"X-PCU-Token": token,
                                                "Origin": "http://evil.example"},
                        ):
                            pass
                    self.assertEqual(ctx.exception.response.status_code, 403)

        asyncio.run(scenario())


class ConfigFailClosedTests(unittest.TestCase):
    """Finding 3: protection failure must not destroy the credential."""

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_load_protection_failure_disk_unchanged_plaintext_kept(self):
        import contextlib
        import io

        with ConfigPatcher(self) as patcher:
            tmp = Path(tempfile.mkdtemp(prefix="pcu-test-fc-load-"))
            patcher.apply(tmp / "config.json", tmp / "legacy.json")
            original = json.dumps({
                "provider": "openai",
                "openai": {"api_key": FAKE_KEY_A, "model": "m"},
            })
            patcher.config_path.write_text(original, encoding="utf-8")

            captured = io.StringIO()
            with _BreakProtect():
                with contextlib.redirect_stdout(captured):
                    cfg = patcher.config_mod.load()

            # Disk file is byte-identical: no rewrite, no credential loss.
            self.assertEqual(
                patcher.config_path.read_text(encoding="utf-8"), original)
            # Plaintext still materialized in memory so the app keeps working.
            self.assertEqual(cfg["openai"]["api_key"], FAKE_KEY_A)
            # No exception escaped; a redacted warning was logged.
            warning = captured.getvalue()
            self.assertIn("secure storage unavailable", warning)
            self.assertNotIn(FAKE_KEY_A, warning)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_save_refuses_credentialless_write(self):
        with ConfigPatcher(self) as patcher:
            tmp = Path(tempfile.mkdtemp(prefix="pcu-test-fc-save-"))
            patcher.apply(tmp / "config.json", tmp / "legacy.json")
            patcher.config_mod.save({
                "provider": "openai",
                "openai": {"api_key": FAKE_KEY_A, "model": "m"},
            })
            before = patcher.config_path.read_bytes()

            with _BreakProtect():
                with self.assertRaises(patcher.config_mod.ConfigWriteError):
                    patcher.config_mod.save({
                        "provider": "openai",
                        "openai": {"api_key": FAKE_KEY_A, "model": "m"},
                    })

            # Existing file untouched; previous credential intact.
            self.assertEqual(patcher.config_path.read_bytes(), before)
            cfg = patcher.config_mod.load()  # protection still broken here
            # key_status never materializes; check the encrypted blob on disk.
            disk = json.loads(before.decode("utf-8"))
            self.assertTrue(disk.get("apiKeyEncrypted"))
            self.assertNotIn(FAKE_KEY_A, json.dumps(disk))

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_save_allows_keyless_config(self):
        with ConfigPatcher(self) as patcher:
            tmp = Path(tempfile.mkdtemp(prefix="pcu-test-fc-save2-"))
            patcher.apply(tmp / "config.json", tmp / "legacy.json")
            # A config that never held a key must still save normally.
            patcher.config_mod.save({
                "provider": "openai",
                "openai": {"api_key": "", "model": "m"},
            })
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertFalse(disk.get("apiKeyEncrypted"))
            self.assertEqual(disk["openai"]["api_key"], "")

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_rotation_protection_failure_keeps_previous_key(self):
        with ConfigPatcher(self) as patcher:
            tmp = Path(tempfile.mkdtemp(prefix="pcu-test-fc-rot-"))
            patcher.apply(tmp / "config.json", tmp / "legacy.json")
            patcher.config_mod.save({
                "provider": "openai",
                "openai": {"api_key": "", "model": "m"},
                "apiKeyEncrypted": patcher.config_mod.secret_store.protect(FAKE_KEY_A),
                "keySource": "bundled",
                "keyVersion": 1,
            })
            before = patcher.config_path.read_bytes()

            with _BreakProtect():
                with self.assertRaises(OSError):
                    patcher.config_mod.rotate_key(FAKE_KEY_B, 2)

            self.assertEqual(patcher.config_path.read_bytes(), before)
            cfg = patcher.config_mod.load()  # protection broken; still loads
            self.assertEqual(
                patcher.config_mod.decrypt_key(cfg), FAKE_KEY_A)


class ConfigProvisionTests(unittest.TestCase):
    """Finding 2: installer provision.json envelope consumption contract."""

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def _apply(self, patcher: ConfigPatcher, prefix: str) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix=prefix))
        patcher.apply(tmp / "config.json", tmp / "legacy.json")
        return tmp

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_happy_path_consumed_and_deleted(self):
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov1-")
            _write_envelope(tmp, FAKE_KEY_C, 7)

            cfg = patcher.config_mod.load()

            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_C)
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertEqual(disk["keySource"], "bundled")
            self.assertEqual(disk["keyVersion"], 7)
            # Envelope AND every legacy/staging name are gone.
            self.assertFalse((tmp / "provision.json").exists())
            self.assertFalse((tmp / "provision.blob").exists())
            self.assertFalse((tmp / "provision.meta.json").exists())
            self.assertFalse((tmp / "provision.json.tmp").exists())
            self.assertFalse((tmp / "provision.blob.tmp").exists())
            self.assertFalse((tmp / "provision.meta.json.tmp").exists())
            # Idempotent: a second load with the envelope gone is a no-op.
            cfg2 = patcher.config_mod.load()
            self.assertEqual(patcher.config_mod.decrypt_key(cfg2), FAKE_KEY_C)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_replaces_bundled_on_version_change(self):
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov2-")
            patcher.config_mod.save({
                "provider": "openai",
                "openai": {"api_key": "", "model": "m"},
                "apiKeyEncrypted": secret_store.protect(FAKE_KEY_A),
                "keySource": "bundled",
                "keyVersion": 1,
            })
            _write_envelope(tmp, FAKE_KEY_C, 2)

            cfg = patcher.config_mod.load()

            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_C)
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertEqual(disk["keyVersion"], 2)
            self.assertFalse((tmp / "provision.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_user_key_preserved(self):
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov3-")
            patcher.config_mod.save({
                "provider": "openai",
                "openai": {"api_key": "", "model": "m"},
                "apiKeyEncrypted": secret_store.protect(FAKE_KEY_A),
                "keySource": "user",
                "keyVersion": 1,
            })
            _write_envelope(tmp, FAKE_KEY_C, 2)

            cfg = patcher.config_mod.load()

            # User key preserved; provision is one-shot (files removed).
            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_A)
            self.assertFalse((tmp / "provision.json").exists())
            self.assertFalse((tmp / "provision.meta.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_unprotect_failure_retains_envelope_and_key(self):
        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov4-")
            patcher.config_mod.save({
                "provider": "openai",
                "openai": {"api_key": "", "model": "m"},
                "apiKeyEncrypted": patcher.config_mod.secret_store.protect(
                    FAKE_KEY_A),
                "keySource": "bundled",
                "keyVersion": 1,
            })
            # Self-consistent garbage: sha256 matches the blob bytes, but
            # DPAPI cannot decrypt them (validation passes, unprotect fails).
            _write_envelope(
                tmp, FAKE_KEY_C, 2, blob=b"\x01\x02\x03not-a-blob")

            cfg = patcher.config_mod.load()

            # Existing credential intact; envelope retained for the next
            # launch (transient unprotect failure, unlike a validation
            # failure which is deleted).
            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_A)
            self.assertTrue((tmp / "provision.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_protection_failure_retains_envelope_and_key(self):
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov5-")
            patcher.config_mod.save({
                "provider": "openai",
                "openai": {"api_key": "", "model": "m"},
                "apiKeyEncrypted": secret_store.protect(FAKE_KEY_A),
                "keySource": "bundled",
                "keyVersion": 1,
            })
            before = patcher.config_path.read_bytes()
            _write_envelope(tmp, FAKE_KEY_C, 2)

            with _BreakProtect():
                cfg = patcher.config_mod.load()

            # Decryption succeeded but persistence failed: existing key and
            # config untouched, provision retained for the next launch.
            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_A)
            self.assertEqual(patcher.config_path.read_bytes(), before)
            self.assertTrue((tmp / "provision.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_legacy_missing_meta_fails_closed(self):
        """Legacy pair with the meta deleted (old-format kill between the two
        renames): fail closed, both removed, app starts without the key."""
        import base64

        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov6-")
            (tmp / "provision.blob").write_bytes(
                base64.b64decode(secret_store.protect(FAKE_KEY_C)))

            cfg = patcher.config_mod.load()

            self.assertEqual(patcher.config_mod.decrypt_key(cfg), "")
            self.assertFalse((tmp / "provision.blob").exists())
            self.assertFalse((tmp / "provision.meta.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_utf16_bytes_roundtrip(self):
        import ctypes

        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov7-")
            # Installer wrote the key string as UTF-16LE bytes inside DPAPI.
            data = FAKE_KEY_C.encode("utf-16-le")
            blob = secret_store._CRYPTPROTECTDATA_BLOB()
            in_blob = secret_store._blob(data)
            if not ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(in_blob), None, None, None, None,
                secret_store.CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob),
            ):
                self.fail("CryptProtectData failed")
            _write_envelope(
                tmp, FAKE_KEY_C, 9, blob=secret_store._bytes_from_blob(blob))

            cfg = patcher.config_mod.load()

            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_C)
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertEqual(disk["keyVersion"], 9)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_migrated_legacy_matching_key_becomes_bundled(self):
        """Wrinkle fix (a): dad's machine scenario.

        His plaintext config holds the PREVIOUSLY-BUNDLED key (he never set
        a custom one). Migration labels it "user"; consumption must
        reclassify it as bundled (with the envelope keyVersion) so future
        installer versions rotate instead of being blocked forever.
        """
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov8-")
            # Legacy plaintext config: key equals the previously-bundled key
            # the new installer provisions.
            patcher.config_path.write_text(json.dumps({
                "provider": "openai",
                "openai": {"api_key": FAKE_KEY_C, "model": "m"},
            }), encoding="utf-8")
            _write_envelope(tmp, FAKE_KEY_C, 7)

            cfg = patcher.config_mod.load()

            # Same key value, but now bundled at the provisioned version:
            # rotation proceeds on future versions.
            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_C)
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertEqual(disk["keySource"], "bundled")
            self.assertEqual(disk["keyVersion"], 7)
            self.assertNotIn(FAKE_KEY_C, json.dumps(disk))
            self.assertFalse((tmp / "provision.json").exists())
            self.assertFalse((tmp / "provision.meta.json").exists())

            # Future installer version actually rotates the key now.
            _write_envelope(tmp, FAKE_KEY_B, 8)
            cfg2 = patcher.config_mod.load()
            self.assertEqual(patcher.config_mod.decrypt_key(cfg2), FAKE_KEY_B)
            disk2 = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertEqual(disk2["keySource"], "bundled")
            self.assertEqual(disk2["keyVersion"], 8)
            self.assertFalse((tmp / "provision.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_migrated_legacy_user_key_preserved(self):
        """Wrinkle fix (b): a genuinely user-supplied key stays "user"."""
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov9-")
            patcher.config_path.write_text(json.dumps({
                "provider": "openai",
                "openai": {"api_key": FAKE_KEY_A, "model": "m"},
            }), encoding="utf-8")
            _write_envelope(tmp, FAKE_KEY_C, 7)

            cfg = patcher.config_mod.load()

            # User key preserved, still labeled user; provision is one-shot.
            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_A)
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertEqual(disk["keySource"], "user")
            self.assertFalse((tmp / "provision.json").exists())
            self.assertFalse((tmp / "provision.meta.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_migrated_reclassification_after_failed_launch(self):
        """Deferred case: migration launch could not consume (protect broken);
        the NEXT launch still reclassifies the matching legacy key."""
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov10-")
            patcher.config_path.write_text(json.dumps({
                "provider": "openai",
                "openai": {"api_key": FAKE_KEY_C, "model": "m"},
            }), encoding="utf-8")
            _write_envelope(tmp, FAKE_KEY_C, 7)

            # Launch 1: protection fails during migration; disk untouched,
            # provision retained for the next launch.
            with _BreakProtect():
                patcher.config_mod.load()
            self.assertTrue((tmp / "provision.json").exists())

            # Launch 2: migration succeeds and the matching key is
            # reclassified bundled at the provisioned version.
            cfg = patcher.config_mod.load()
            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_C)
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertEqual(disk["keySource"], "bundled")
            self.assertEqual(disk["keyVersion"], 7)
            self.assertFalse((tmp / "provision.json").exists())
            self.assertFalse((tmp / "provision.meta.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_fresh_install_path_unchanged(self):
        """Wrinkle fix (c) regression: fresh install (no stored key) still
        provisions as bundled exactly as before."""
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov11-")
            self.assertFalse(patcher.config_path.exists())
            _write_envelope(tmp, FAKE_KEY_C, 3)

            cfg = patcher.config_mod.load()

            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_C)
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertEqual(disk["keySource"], "bundled")
            self.assertEqual(disk["keyVersion"], 3)
            self.assertFalse((tmp / "provision.json").exists())
            self.assertFalse((tmp / "provision.meta.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_envelope_bad_sha_fails_closed(self):
        """Interruption scenario (b): envelope whose blob does not match its
        blob_sha256 is tamper evidence - deleted, no key adopted."""
        import base64
        import hashlib

        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov12-")
            raw = base64.b64decode(secret_store.protect(FAKE_KEY_C))
            envelope = {
                "schema": 1,
                "keyVersion": 7,
                "blob": base64.b64encode(raw).decode("ascii"),
                "blob_sha256": hashlib.sha256(raw).hexdigest(),
            }
            envelope["blob_sha256"] = "0" * 64
            (tmp / "provision.json").write_text(
                json.dumps(envelope), encoding="utf-8")

            cfg = patcher.config_mod.load()

            self.assertEqual(patcher.config_mod.decrypt_key(cfg), "")
            self.assertFalse((tmp / "provision.json").exists())
            self.assertFalse((tmp / "provision.blob").exists())
            self.assertFalse((tmp / "provision.meta.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_envelope_invalid_json_fails_closed(self):
        """A corrupt envelope (undecodable JSON / wrong schema) is deleted
        and consumption fails closed."""
        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov13-")
            (tmp / "provision.json").write_text("{not json", encoding="utf-8")

            cfg = patcher.config_mod.load()

            self.assertEqual(patcher.config_mod.decrypt_key(cfg), "")
            self.assertFalse((tmp / "provision.json").exists())

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov14-")
            (tmp / "provision.json").write_text(
                json.dumps({"schema": 99, "keyVersion": 1, "blob": "",
                            "blob_sha256": ""}), encoding="utf-8")

            cfg = patcher.config_mod.load()

            self.assertEqual(patcher.config_mod.decrypt_key(cfg), "")
            self.assertFalse((tmp / "provision.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_legacy_missing_blob_fails_closed(self):
        """Interruption scenario (d): legacy pair with the blob deleted but
        meta present - fail closed, both removed, no key."""
        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov15-")
            (tmp / "provision.meta.json").write_text(
                json.dumps({"keyVersion": 7, "keySource": "bundled"}),
                encoding="utf-8")

            cfg = patcher.config_mod.load()

            self.assertEqual(patcher.config_mod.decrypt_key(cfg), "")
            self.assertFalse((tmp / "provision.blob").exists())
            self.assertFalse((tmp / "provision.meta.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_legacy_undecodable_meta_fails_closed(self):
        """Legacy pair with an undecodable meta next to a decryptable blob:
        fail closed, both removed, no key."""
        import base64

        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov16-")
            (tmp / "provision.blob").write_bytes(
                base64.b64decode(secret_store.protect(FAKE_KEY_C)))
            (tmp / "provision.meta.json").write_text(
                "{not json", encoding="utf-8")

            cfg = patcher.config_mod.load()

            self.assertEqual(patcher.config_mod.decrypt_key(cfg), "")
            self.assertFalse((tmp / "provision.blob").exists())
            self.assertFalse((tmp / "provision.meta.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_tmp_leftovers_swept_on_consume(self):
        """Interruption scenario (e): .tmp leftovers of a hard-killed
        installer write are ignored and removed on the next consume; the
        valid envelope still provisions."""
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov17-")
            (tmp / "provision.json.tmp").write_text("{killed mid-write", 
                                                    encoding="utf-8")
            (tmp / "provision.blob.tmp").write_bytes(b"\x00\x01")
            (tmp / "provision.meta.json.tmp").write_text("{", encoding="utf-8")
            _write_envelope(tmp, FAKE_KEY_C, 5)

            cfg = patcher.config_mod.load()

            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_C)
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertEqual(disk["keyVersion"], 5)
            self.assertFalse((tmp / "provision.json.tmp").exists())
            self.assertFalse((tmp / "provision.blob.tmp").exists())
            self.assertFalse((tmp / "provision.meta.json.tmp").exists())
            self.assertFalse((tmp / "provision.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_tmp_only_starts_without_key(self):
        """Interruption scenario (f): an envelope write killed before the
        rename leaves only the .tmp - the backend sees no provision at all
        and the app starts without the built-in key."""
        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-prov18-")
            (tmp / "provision.json.tmp").write_text('{"schema":1,"blob":"x',
                                                    encoding="utf-8")

            cfg = patcher.config_mod.load()

            self.assertEqual(patcher.config_mod.decrypt_key(cfg), "")
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8")) \
                if patcher.config_path.exists() else {}
            self.assertNotEqual(disk.get("keySource"), "bundled")
            self.assertFalse((tmp / "provision.json").exists())
            # The leftover is swept on the (no-op) consume.
            self.assertFalse((tmp / "provision.json.tmp").exists())


class BundledBackupTests(unittest.TestCase):
    """bundledKeyEncrypted maintenance: backup of the last-known bundled key.

    The backup is a DPAPI blob in the SAME protected store (never plaintext)
    so the WS verb restore_bundled can restore the bundled key after the
    installer's provision.blob is consumed and deleted.
    """

    FAKE_KEY_USER = "sk-test-fake-user-key-1111222233334444"

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def _apply(self, patcher: ConfigPatcher, prefix: str) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix=prefix))
        patcher.apply(tmp / "config.json", tmp / "legacy.json")
        return tmp

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def _disk(self, patcher: ConfigPatcher) -> dict:
        return json.loads(patcher.config_path.read_text(encoding="utf-8"))

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_provision_consumption_writes_bundled_backup(self):
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-bb1-")
            _write_envelope(tmp, FAKE_KEY_C, 7)

            patcher.config_mod.load()

            disk = self._disk(patcher)
            # Same key, DPAPI-protected, never plaintext; version recorded
            # alongside so a restore can re-apply it.
            self.assertEqual(
                secret_store.unprotect(disk["bundledKeyEncrypted"]), FAKE_KEY_C)
            self.assertEqual(disk["bundledKeyVersion"], 7)
            self.assertNotIn(FAKE_KEY_C, json.dumps(disk))

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_bundled_rotation_refreshes_backup(self):
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-bb2-")
            patcher.config_mod.rotate_key(FAKE_KEY_A, 1, "bundled")

            patcher.config_mod.rotate_key(FAKE_KEY_B, 2, "bundled")

            disk = self._disk(patcher)
            self.assertEqual(
                secret_store.unprotect(disk["bundledKeyEncrypted"]), FAKE_KEY_B)
            self.assertEqual(disk["bundledKeyVersion"], 2)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_user_rotation_keeps_bundled_backup(self):
        """A user rotation must NOT invalidate the "use built-in key" backup."""
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            self._apply(patcher, "pcu-test-bb3-")
            patcher.config_mod.rotate_key(FAKE_KEY_A, 1, "bundled")

            patcher.config_mod.rotate_key(self.FAKE_KEY_USER, None, "user")

            disk = self._disk(patcher)
            self.assertEqual(disk["keySource"], "user")
            self.assertEqual(
                secret_store.unprotect(disk["apiKeyEncrypted"]),
                self.FAKE_KEY_USER)
            # Backup untouched: restore_bundled still finds the bundled key.
            self.assertEqual(
                secret_store.unprotect(disk["bundledKeyEncrypted"]), FAKE_KEY_A)
            self.assertEqual(disk["bundledKeyVersion"], 1)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_same_version_noop_heals_missing_backup(self):
        """Pre-upgrade installs (bundled key, no backup field) self-heal."""
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-bb4-")
            patcher.config_mod.save({
                "provider": "openai",
                "openai": {"api_key": "", "model": "m"},
                "apiKeyEncrypted": secret_store.protect(FAKE_KEY_A),
                "keySource": "bundled",
                "keyVersion": 5,
            })
            disk = self._disk(patcher)
            self.assertFalse(disk.get("bundledKeyEncrypted"))

            outcome = patcher.config_mod.rotate_key(FAKE_KEY_A, "5", "bundled")

            self.assertEqual(outcome, "same")
            disk = self._disk(patcher)
            self.assertEqual(
                secret_store.unprotect(disk["bundledKeyEncrypted"]), FAKE_KEY_A)
            self.assertEqual(disk["bundledKeyVersion"], 5)
            self.assertEqual(patcher.config_mod.decrypt_key(
                patcher.config_mod.load()), FAKE_KEY_A)


class RestoreBundledTests(unittest.TestCase):
    """WS verb restore_bundled backend logic ("use built-in key")."""

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def _apply(self, patcher: ConfigPatcher, prefix: str) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix=prefix))
        patcher.apply(tmp / "config.json", tmp / "legacy.json")
        return tmp

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def _seed_bundled_with_backup(self, patcher: ConfigPatcher) -> None:
        patcher.config_mod.rotate_key(FAKE_KEY_A, 3, "bundled")

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_restore_replaces_user_key_and_restores_version(self):
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            self._apply(patcher, "pcu-test-rb1-")
            self._seed_bundled_with_backup(patcher)
            # Explicit user action: a user key replaces the bundled one...
            patcher.config_mod.rotate_key(FAKE_KEY_B, None, "user")

            result = patcher.config_mod.restore_bundled()

            self.assertTrue(result["ok"])
            self.assertIsNone(result["error"])
            cfg = patcher.config_mod.load()
            # ...and restoring brings the bundled key (and its version) back.
            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_A)
            disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
            self.assertEqual(disk["keySource"], "bundled")
            self.assertEqual(disk["keyVersion"], 3)
            self.assertNotIn(FAKE_KEY_A, json.dumps(disk))
            self.assertNotIn(FAKE_KEY_B, json.dumps(disk))

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_restore_without_backup_fails_and_keeps_current_key(self):
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            self._apply(patcher, "pcu-test-rb2-")
            patcher.config_mod.save({
                "provider": "openai",
                "openai": {"api_key": "", "model": "m"},
                "apiKeyEncrypted": secret_store.protect(FAKE_KEY_B),
                "keySource": "user",
                "keyVersion": 0,
            })
            before = patcher.config_path.read_bytes()

            result = patcher.config_mod.restore_bundled()

            self.assertFalse(result["ok"])
            self.assertIn("no built-in key stored", result["error"])
            self.assertNotIn(FAKE_KEY_B, result["error"])
            # Current credential untouched.
            self.assertEqual(patcher.config_path.read_bytes(), before)
            self.assertEqual(patcher.config_mod.decrypt_key(
                patcher.config_mod.load()), FAKE_KEY_B)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_restore_corrupt_backup_fails_and_keeps_current_key(self):
        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            tmp = self._apply(patcher, "pcu-test-rb3-")
            patcher.config_mod.save({
                "provider": "openai",
                "openai": {"api_key": "", "model": "m"},
                "apiKeyEncrypted": secret_store.protect(FAKE_KEY_B),
                "keySource": "user",
                "keyVersion": 0,
                "bundledKeyEncrypted": "not-a-valid-dpapi-blob!!",
                "bundledKeyVersion": 5,
            })
            before = patcher.config_path.read_bytes()

            result = patcher.config_mod.restore_bundled()

            self.assertFalse(result["ok"])
            self.assertIn("unreadable", result["error"])
            self.assertEqual(patcher.config_path.read_bytes(), before)
            cfg = patcher.config_mod.load()
            self.assertEqual(patcher.config_mod.decrypt_key(cfg), FAKE_KEY_B)
            self.assertEqual(
                json.loads(patcher.config_path.read_text(encoding="utf-8"))["keySource"],
                "user")

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_restore_persist_failure_fails_and_keeps_current_key(self):
        """Fail-closed: a failed save must not touch the stored credential."""
        import contextlib
        import io
        from unittest import mock

        from backend import secret_store

        with ConfigPatcher(self) as patcher:
            self._apply(patcher, "pcu-test-rb4-")
            self._seed_bundled_with_backup(patcher)
            patcher.config_mod.rotate_key(FAKE_KEY_B, None, "user")
            before = patcher.config_path.read_bytes()

            captured = io.StringIO()
            with mock.patch.object(patcher.config_mod, "save",
                                   side_effect=OSError("synthetic persist failure")):
                with contextlib.redirect_stdout(captured):
                    result = patcher.config_mod.restore_bundled()

            self.assertFalse(result["ok"])
            self.assertIn("could not persist", result["error"])
            self.assertNotIn(FAKE_KEY_B, result["error"])
            self.assertNotIn(FAKE_KEY_A, result["error"])
            self.assertNotIn(FAKE_KEY_B, captured.getvalue())
            # Disk untouched: the user credential survives the failed restore.
            self.assertEqual(patcher.config_path.read_bytes(), before)


class WsKeyOpTests(unittest.TestCase):
    """Live WS key-op verbs (rotate_key / restore_bundled) against a real
    ephemeral-port backend; reply contract is {"type":"key_op_result"}."""

    def _recv_until(self, ws: Any, predicate: Any) -> Any:
        import asyncio

        async def pump() -> Any:
            while True:
                raw = await ws.recv()
                message = json.loads(raw)
                if predicate(message):
                    return message

        return asyncio.wait_for(pump(), timeout=10)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_rotate_key_user_source_via_ws(self):
        import asyncio

        import websockets

        from backend.main import Backend

        async def scenario() -> None:
            with ConfigPatcher(self) as patcher:
                tmp = Path(tempfile.mkdtemp(prefix="pcu-test-wskey1-"))
                patcher.apply(tmp / "config.json", tmp / "legacy.json")
                backend = Backend()
                token = backend.issue_runtime_token()
                async with websockets.serve(backend.handler, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    async with websockets.connect(
                        f"ws://127.0.0.1:{port}",
                        additional_headers={"X-PCU-Token": token},
                    ) as ws:
                        await ws.send(json.dumps({
                            "type": "rotate_key",
                            "apiKey": FAKE_KEY_B,
                            "keySource": "user",
                        }))
                        reply = await self._recv_until(
                            ws, lambda m: m.get("type") == "key_op_result"
                            and m.get("op") == "rotate_key")
                self.assertTrue(reply["ok"])
                self.assertEqual(reply.get("outcome"), "replaced")
                disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
                self.assertEqual(disk["keySource"], "user")
                self.assertNotIn(FAKE_KEY_B, json.dumps(disk))
                self.assertEqual(patcher.config_mod.decrypt_key(
                    patcher.config_mod.load()), FAKE_KEY_B)

        asyncio.run(scenario())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_restore_bundled_via_ws_without_backup_fails(self):
        import asyncio

        import websockets

        from backend.main import Backend

        async def scenario() -> None:
            with ConfigPatcher(self) as patcher:
                tmp = Path(tempfile.mkdtemp(prefix="pcu-test-wskey2-"))
                patcher.apply(tmp / "config.json", tmp / "legacy.json")
                patcher.config_mod.save({
                    "provider": "openai",
                    "openai": {"api_key": "", "model": "m"},
                    "apiKeyEncrypted": patcher.config_mod.secret_store.protect(
                        FAKE_KEY_B),
                    "keySource": "user",
                    "keyVersion": 0,
                })
                backend = Backend()
                token = backend.issue_runtime_token()
                async with websockets.serve(backend.handler, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    async with websockets.connect(
                        f"ws://127.0.0.1:{port}",
                        additional_headers={"X-PCU-Token": token},
                    ) as ws:
                        await ws.send(json.dumps({"type": "restore_bundled"}))
                        reply = await self._recv_until(
                            ws, lambda m: m.get("type") == "key_op_result"
                            and m.get("op") == "restore_bundled")
                self.assertFalse(reply["ok"])
                self.assertIn("no built-in key stored", reply["error"])
                self.assertNotIn(FAKE_KEY_B, json.dumps(reply))
                # Current key kept.
                self.assertEqual(patcher.config_mod.decrypt_key(
                    patcher.config_mod.load()), FAKE_KEY_B)

        asyncio.run(scenario())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI requires Windows")
    def test_restore_bundled_via_ws_happy_path(self):
        import asyncio

        import websockets

        from backend.main import Backend

        async def scenario() -> None:
            with ConfigPatcher(self) as patcher:
                tmp = Path(tempfile.mkdtemp(prefix="pcu-test-wskey3-"))
                patcher.apply(tmp / "config.json", tmp / "legacy.json")
                patcher.config_mod.rotate_key(FAKE_KEY_A, 3, "bundled")
                patcher.config_mod.rotate_key(FAKE_KEY_B, None, "user")
                backend = Backend()
                token = backend.issue_runtime_token()
                async with websockets.serve(backend.handler, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    async with websockets.connect(
                        f"ws://127.0.0.1:{port}",
                        additional_headers={"X-PCU-Token": token},
                    ) as ws:
                        await ws.send(json.dumps({"type": "restore_bundled"}))
                        reply = await self._recv_until(
                            ws, lambda m: m.get("type") == "key_op_result"
                            and m.get("op") == "restore_bundled")
                self.assertTrue(reply["ok"])
                self.assertNotIn(FAKE_KEY_A, json.dumps(reply))
                self.assertNotIn(FAKE_KEY_B, json.dumps(reply))
                disk = json.loads(patcher.config_path.read_text(encoding="utf-8"))
                self.assertEqual(disk["keySource"], "bundled")
                self.assertEqual(disk["keyVersion"], 3)
                self.assertEqual(patcher.config_mod.decrypt_key(
                    patcher.config_mod.load()), FAKE_KEY_A)

        asyncio.run(scenario())


class TypeActionPrivacyTests(unittest.TestCase):
    """Finding 4: typed credentials never reach display/persist/broadcast."""

    def setUp(self) -> None:
        from backend import secrets_filter

        secrets_filter.clear_secrets()

    def tearDown(self) -> None:
        from backend import secrets_filter

        secrets_filter.clear_secrets()

    def test_describe_withholds_typed_text(self):
        from backend.agent_loop import _describe

        detail = _describe({"kind": "type", "text": TYPED_SECRET})
        self.assertNotIn(TYPED_SECRET, detail)
        self.assertIn(f"{len(TYPED_SECRET)} chars", detail)
        # Other kinds unchanged.
        self.assertEqual(
            _describe({"kind": "click", "x": 5, "y": 6}), "click at (5, 6)")

    def test_filter_alone_cannot_redact_arbitrary_typed_text(self):
        # Documents why construction-time stripping is required: an
        # unregistered password matches no exact secret or token pattern.
        from backend import secrets_filter

        self.assertEqual(
            secrets_filter.redact_text(TYPED_SECRET), TYPED_SECRET)

    def test_typed_text_absent_from_events_and_broadcast(self):
        import asyncio
        import os

        from backend.agent_loop import TaskRunner, _describe

        tmp = Path(tempfile.mkdtemp(prefix="pcu-test-type-privacy-"))
        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        old_dir = os.environ.get("PCU_TRAJECTORY_DIR")
        os.environ["PCU_TRAJECTORY_DIR"] = str(tmp)
        try:
            async def scenario() -> None:
                runner = TaskRunner(
                    "t1", "fill the PIN field",
                    {"saveScreenshots": False, "max_steps": 5}, send)
                action = {"kind": "type", "text": TYPED_SECRET}
                detail = _describe(action)
                action_msg: dict = {"type": "action", "kind": "type",
                                    "detail": detail}
                await runner._emit(action_msg)

            asyncio.run(scenario())
        finally:
            if old_dir is None:
                os.environ.pop("PCU_TRAJECTORY_DIR", None)
            else:
                os.environ["PCU_TRAJECTORY_DIR"] = old_dir

        broadcast = json.dumps(sent)
        self.assertNotIn(TYPED_SECRET, broadcast)
        self.assertIn(f"type [typed text withheld: "
                      f"{len(TYPED_SECRET)} chars]", broadcast)
        events_files = list(tmp.glob("*/events.jsonl"))
        self.assertEqual(len(events_files), 1)
        events = events_files[0].read_text(encoding="utf-8")
        self.assertNotIn(TYPED_SECRET, events)
        self.assertIn(f"{len(TYPED_SECRET)} chars", events)


class ProviderDumpScrubTests(unittest.TestCase):
    """Finding 4: the model's raw action JSON is scrubbed in opt-in dumps."""

    def test_raw_dump_scrubs_typed_text(self):
        import contextlib
        import io
        import os

        from backend.providers import openai_compat

        tmp = Path(tempfile.mkdtemp(prefix="pcu-test-dump3-"))
        old_dir = os.environ.get("PCU_TRAJECTORY_DIR")
        old_dump = os.environ.get("PCU_RAW_PROVIDER_DUMP")
        os.environ["PCU_TRAJECTORY_DIR"] = str(tmp)
        os.environ["PCU_RAW_PROVIDER_DUMP"] = "1"
        try:
            openai_compat._DEBUG_REPORTED.clear()
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                openai_compat._dump_raw("parse_fail", {
                    "content": ('{"actions": [{"kind": "type", '
                                '"text": "' + TYPED_SECRET + '"}]}')
                })
            files = list(tmp.glob("_provider_debug/raw_replies.jsonl"))
            self.assertEqual(len(files), 1)
            body = files[0].read_text(encoding="utf-8")
            self.assertNotIn(TYPED_SECRET, body)
            self.assertIn("TYPED TEXT WITHHELD", body)
            # Action semantics retained (the reply is JSON-escaped in the dump).
            self.assertIn('\\"kind\\": \\"type\\"', body)
        finally:
            if old_dir is None:
                os.environ.pop("PCU_TRAJECTORY_DIR", None)
            else:
                os.environ["PCU_TRAJECTORY_DIR"] = old_dir
            if old_dump is None:
                os.environ.pop("PCU_RAW_PROVIDER_DUMP", None)
            else:
                os.environ["PCU_RAW_PROVIDER_DUMP"] = old_dump


if __name__ == "__main__":
    unittest.main()
