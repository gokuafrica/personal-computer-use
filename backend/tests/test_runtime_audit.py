"""Regression tests for input delivery, confirmations and completion. No real input."""
import asyncio
import ctypes
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from backend import a11y, control, agent_loop
from backend.providers import StepResult


class InputTests(unittest.TestCase):
    def test_windows_input_layout(self):
        self.assertEqual(ctypes.sizeof(control._Input), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

    def test_click_delivers_down_and_up(self):
        flags = []
        def send(count, inputs, size):
            flags.extend(inputs[i].mi.dwFlags for i in range(count))
            return count
        with patch.object(control._user32, 'SendInput', side_effect=send), patch.object(control.pyautogui, 'failSafeCheck'):
            control.left_click()
            control.right_click()
        self.assertEqual(flags, [2, 4, 8, 16])

    def test_blocked_click_raises(self):
        with patch.object(control._user32, 'SendInput', return_value=0), patch.object(control.pyautogui, 'failSafeCheck'):
            with self.assertRaisesRegex(RuntimeError, 'Windows blocked'):
                control.left_click()

    def test_partial_press_releases(self):
        with patch.object(control._user32, 'SendInput', side_effect=[1, 1]) as send, patch.object(control.pyautogui, 'failSafeCheck'):
            with self.assertRaises(RuntimeError):
                control.left_click()
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.args[0], 1)

    def test_unicode_and_stop(self):
        units = []
        def inject(down, up):
            self.assertEqual(up.ki.dwFlags, 6)
            units.append(down.ki.wScan)
        with patch.object(control, '_inject_pair', side_effect=inject), patch.object(control.time, 'sleep'):
            control.type_text('café क🙂')
            result = b''.join(unit.to_bytes(2, 'little') for unit in units).decode('utf-16-le')
            self.assertEqual(result, 'café क🙂')
            units.clear()
            control.type_text('abcdef', lambda: len(units) >= 2)
            self.assertEqual(units, [97, 98])

    def test_failsafe_preserved(self):
        with patch.object(control.pyautogui, 'failSafeCheck', side_effect=control.FailSafeException), patch.object(control._user32, 'SendInput') as send:
            with self.assertRaises(control.FailSafeException):
                control.left_click()
            send.assert_not_called()


class AccessibilityTests(unittest.TestCase):
    def test_value_pattern(self):
        field = Mock()
        field.GetValuePattern.return_value.Value = 'real contents'
        self.assertEqual(a11y._safe_value(field), 'real contents')

    def test_text_pattern_fallback(self):
        field = Mock()
        field.GetValuePattern.side_effect = RuntimeError()
        field.GetTextPattern.return_value.DocumentRange.GetText.return_value = 'document contents'
        self.assertEqual(a11y._safe_value(field), 'document contents')

    def test_field_label_is_not_contents(self):
        field = Mock(Name='Text editor')
        field.GetValuePattern.side_effect = RuntimeError()
        field.GetTextPattern.side_effect = RuntimeError()
        with patch.object(a11y, '_import_uia', return_value=SimpleNamespace(GetFocusedControl=lambda: field)):
            self.assertEqual(a11y.read_focused_value(), '')


class ConfirmationProtocolTests(unittest.TestCase):
    def test_non_boolean_approval_is_ignored(self):
        from backend.main import Backend
        backend = Backend()
        backend.runner = Mock()
        for value in ('false', 'true', 1, None):
            backend.handle_confirm({'id': 'pending', 'approved': value})
        backend.runner.resolve_confirm.assert_not_called()

    def test_boolean_approval_is_forwarded(self):
        from backend.main import Backend
        backend = Backend()
        backend.runner = Mock()
        backend.handle_confirm({'id': 'pending', 'approved': False})
        backend.runner.resolve_confirm.assert_called_once_with('pending', False)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def runner(self, **cfg):
        with patch.object(agent_loop, 'new_recorder', return_value=Mock()):
            return agent_loop.TaskRunner('audit-test', 'synthetic task', {'max_steps': 3, 'action_delay_s': 0, **cfg}, AsyncMock())

    async def scripted(self, runner, responses):
        provider = SimpleNamespace(run_step=AsyncMock(side_effect=responses))
        with ExitStack() as stack:
            stack.enter_context(patch.object(agent_loop, 'get_provider', return_value=provider))
            stack.enter_context(patch.object(agent_loop.screen, 'capture', return_value=Image.new('RGB', (50, 50))))
            stack.enter_context(patch.object(agent_loop.screen, 'origin', return_value=(0, 0)))
            stack.enter_context(patch.object(runner, '_capture_a11y', AsyncMock(return_value=None)))
            stack.enter_context(patch.object(agent_loop, 'prune_trajectory_root'))
            await runner._run()
        return provider

    async def test_confirmation_off_by_default(self):
        runner = self.runner()
        runner._gate = AsyncMock(return_value=True)
        runner._execute = AsyncMock()
        runner._current_a11y_elements = [{'id': 1, 'name': 'Delete', 'center': [20, 20]}]
        await self.scripted(runner, [StepResult(actions=[{'kind': 'click_element', 'id': 1}], done=True)])
        runner._gate.assert_not_awaited()
        runner._execute.assert_awaited_once()

    async def test_optional_confirmation_executes_once(self):
        runner = self.runner(confirm_actions=True)
        runner._gate = AsyncMock(return_value=True)
        runner._execute = AsyncMock()
        runner._current_a11y_elements = [{'id': 1, 'name': 'Delete', 'center': [20, 20]}]
        await self.scripted(runner, [StepResult(actions=[{'kind': 'click_element', 'id': 1}], done=True)])
        runner._gate.assert_awaited_once_with('Click Delete (element 1)')
        runner._execute.assert_awaited_once()

    async def test_type_then_done_requires_another_observation(self):
        runner = self.runner()
        runner._execute = AsyncMock()
        runner._verify_after_type = AsyncMock(return_value='unknown')
        provider = await self.scripted(runner, [
            StepResult(actions=[{'kind': 'type', 'text': 'abc'}, {'kind': 'done'}], done=True),
            StepResult(actions=[], done=True),
        ])
        self.assertEqual(provider.run_step.await_count, 2)
        self.assertEqual(runner._steps, 2)

    async def test_stop_after_pointer_move_prevents_click(self):
        runner = self.runner()
        with patch.object(control, 'move', side_effect=lambda *args: runner.stop_event.set()), patch.object(control, 'left_click') as click:
            with self.assertRaises(agent_loop._TaskStopped):
                await runner._execute({'kind': 'click', 'x': 20, 'y': 20}, 1, 1, 0, 0)
            click.assert_not_called()

    async def test_declined_action_stops_instead_of_reprompting(self):
        runner = self.runner(confirm_actions=True)
        async def decline(message):
            if message.get('type') == 'need_confirmation':
                runner.resolve_confirm(message['id'], False)
        runner.send = decline
        with self.assertRaisesRegex(RuntimeError, 'declined'):
            await runner._gate('Synthetic Submit button')


if __name__ == '__main__':
    unittest.main()
