"""Slow SDK initialization must not block backend heartbeats or cancellation."""
import asyncio
import threading
import unittest
from unittest.mock import patch
from backend.providers.openai_compat import OpenAICompatProvider
from backend.providers.openai_provider import OpenAIProvider
from backend.providers.anthropic_provider import AnthropicProvider


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def check_provider(self, provider):
        entered=threading.Event();release=threading.Event()
        def slow_client():
            entered.set()
            release.wait(2)
            raise RuntimeError('Simulated slow initialization failure')
        with patch.object(provider,'_get_client',side_effect=slow_client):
            task=asyncio.create_task(provider.run_step('',10,10,'Fixture',None))
            try:
                for _ in range(50):
                    if entered.is_set():break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                # The event loop must reach here while initialization is held.
                self.assertFalse(task.done(),'SDK initialization blocked the event loop')
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task,.2)
            finally:
                release.set()
                await asyncio.gather(task,return_exceptions=True)

    async def test_compat_sdk_startup_is_cancellable(self):
        await self.check_provider(OpenAICompatProvider({'openai_compat':{'model':'test'}}))

    async def test_openai_sdk_startup_is_cancellable(self):
        await self.check_provider(OpenAIProvider({}))

    async def test_anthropic_sdk_startup_is_cancellable(self):
        await self.check_provider(AnthropicProvider({}))
