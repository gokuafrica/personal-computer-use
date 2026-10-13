"""Subscription adapter validation and cancellation regression tests."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.providers.codex_provider import CodexProvider, subscription_env, validate_result
from backend import config


class ValidationTests(unittest.TestCase):
    def result(self, action, done=False):
        return {"actions": [action], "done": done, "summary": "test"}

    def test_rejects_invalid_actions(self):
        cases = [
            {"kind": "click", "x": -1, "y": 5}, {"kind": "click", "x": 800, "y": 5},
            {"kind": "click", "x": float("nan"), "y": 5}, {"kind": "click", "x": True, "y": 5},
            {"kind": "click"}, {"kind": "shell", "text": "bad"},
            {"kind": "click_element", "id": 999}, {"kind": "wait", "amount": 500},
            {"kind": "scroll", "amount": 1, "direction": "sideways"},
        ]
        for action in cases:
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_result(self.result(action), 800, 400, None)

    def test_completion_cannot_execute_unverified_action(self):
        with self.assertRaises(ValueError):
            validate_result(self.result({"kind": "type", "text": "hello"}, True), 800, 400, None)

    def test_element_id_and_pixel_validation(self):
        for action in ({"kind": "click_element", "id": 7}, {"kind": "click", "x": 799, "y": 399}):
            self.assertEqual(validate_result(self.result(action), 800, 400, {"elements": [{"id": 7}]}), self.result(action))

    def test_subscription_env_excludes_api_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "CODEX_API_KEY": "fake", "OPENAI_BASE_URL": "https://example.invalid"}):
            env = subscription_env()
        self.assertFalse(any(k in env for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL")))

    def test_subscription_config_not_overwritten_by_legacy_key(self):
        self.assertFalse(config._active_is_default_empty({"provider": "codex"}))

    def test_no_key_materialized_into_codex(self):
        with patch.object(config, "decrypt_key", return_value="synthetic-key"):
            cfg = config._materialize_runtime({"provider": "codex", "codex": {"model": "gpt-6-luna"}})
        self.assertNotIn("api_key", cfg["codex"])


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_out_does_not_send_screenshot(self):
        with patch('backend.providers.codex_provider.login_status', return_value={"signedIn": False, "message": "Sign in"}), \
             patch('asyncio.create_subprocess_exec') as spawn:
            with self.assertRaisesRegex(RuntimeError, "Sign in"):
                await CodexProvider({}).run_step("", 800, 400, "test", None)
            spawn.assert_not_called()

    async def test_cancel_kills_process_and_removes_temporary_image(self):
        started = asyncio.Event()
        folders = []
        class Process:
            returncode = None
            killed = False
            async def communicate(self, data=None):
                if self.killed:
                    return b'', b''
                started.set()
                await asyncio.Future()
            def kill(self):
                self.killed = True
                self.returncode = -1
        proc = Process()
        async def spawn(*args, **kwargs):
            folders.append(Path(kwargs['cwd']))
            self.assertIn('--ephemeral', args)
            self.assertIn('forced_login_method="chatgpt"', args)
            return proc
        provider = CodexProvider({})
        provider._authenticated = True
        with patch('backend.providers.codex_provider.codex_command', return_value=['codex.exe']), \
             patch('asyncio.create_subprocess_exec', side_effect=spawn):
            task = asyncio.create_task(provider.run_step(base64.b64encode(b'test').decode(), 800, 400, 'test', None))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(proc.killed)
        self.assertTrue(all(not folder.exists() for folder in folders))


if __name__ == '__main__':
    unittest.main()
