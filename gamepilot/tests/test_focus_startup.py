"""Startup focus waiting with fake windows and clocks. No desktop input."""
from pathlib import Path
import queue
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from actions import ActionGate, Cancelled
from config import load_config, validate_config
from windows_input import WindowsMouse
from ui import App


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class FocusStartupTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.gate = Clock(), ActionGate()
        self.mouse = WindowsMouse.__new__(WindowsMouse)
        self.mouse.api = MagicMock()
        self.mouse.prepare = MagicMock(return_value=7)
        self.target = {"window": 7, "region": [0, 0, 1705, 1066]}
        self.locator = MagicMock(return_value=self.target)
        self.messages = []
        self.gate.wait = self.advance
        self.addCleanup(lambda: self.mouse.api.SendInput.assert_not_called())

    def advance(self, seconds):
        self.gate.check()
        self.clock.now += seconds

    def wait(self, **kwargs):
        return self.mouse.wait_for_game([0, 0, 1705, 1066], self.gate, timeout=1,
                                        notify=self.messages.append, clock=self.clock,
                                        locator=kwargs.pop("locator", self.locator), **kwargs)

    def test_waits_for_two_fresh_confirmations_after_user_switches_to_game(self):
        self.mouse.prepare.side_effect = [RuntimeError("工具在前台"), RuntimeError("工具在前台"), 7, 7]
        self.assertEqual(self.wait(), 7)
        self.assertEqual(self.mouse.game_window, 7)
        self.assertEqual(self.mouse.prepare.call_count, 4)
        self.assertTrue(any("最小化" in text for text in self.messages))
        self.assertIn("连续确认", self.messages[-1])

    def test_focus_never_arrives_times_out_without_input(self):
        self.mouse.prepare.side_effect = RuntimeError("目标不属于前台窗口")
        with self.assertRaisesRegex(RuntimeError, "等待游戏前台超时"):
            self.wait()
        self.assertEqual(self.mouse.prepare.call_count, 4)

    def test_missing_game_is_waited_for_but_not_guessed(self):
        self.locator.side_effect = [RuntimeError("未找到游戏"), self.target, self.target]
        self.assertEqual(self.wait(), 7)
        self.assertEqual(self.mouse.prepare.call_count, 2)

    def test_other_foreground_window_cannot_satisfy_game_wait(self):
        self.mouse.prepare.return_value = 99
        with self.assertRaisesRegex(RuntimeError, "超时"):
            self.wait()

    def test_changed_region_requires_relocation_instead_of_waiting_or_clicking(self):
        self.target["region"] = [50, 50, 1705, 1066]
        with self.assertRaisesRegex(RuntimeError, "自动定位游戏"):
            self.wait()
        self.mouse.prepare.assert_not_called()

    def test_intermittent_focus_does_not_count_as_two_consecutive_confirmations(self):
        self.mouse.prepare.side_effect = [7, RuntimeError("焦点丢失"), 7, RuntimeError("焦点丢失")]
        with self.assertRaisesRegex(RuntimeError, "超时"):
            self.wait()

    def test_cancel_before_or_during_wait_stops_without_new_attempt(self):
        self.gate.cancel()
        with self.assertRaises(Cancelled):
            self.wait()
        self.locator.assert_not_called()
        self.gate.reset()
        self.mouse.prepare.side_effect = RuntimeError("工具在前台")

        def stop(seconds):
            self.gate.cancel()
            self.gate.check()

        self.gate.wait = stop
        with self.assertRaises(Cancelled):
            self.wait()
        self.assertEqual(self.mouse.prepare.call_count, 1)

    def test_focus_timeout_config_is_finite_and_bounded(self):
        for value in (-1, 121, True, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config({**load_config(), "focus_wait_seconds": value})

    def test_prepared_backend_rejects_another_foreground_window_before_input(self):
        mouse = WindowsMouse.__new__(WindowsMouse)
        mouse.api = MagicMock()
        mouse.game_window = 7
        mouse.api.GetAncestor.return_value = 99
        mouse.api.GetForegroundWindow.return_value = 99
        with self.assertRaisesRegex(RuntimeError, "不是本轮确认的游戏"):
            mouse.prepare((500, 500), [0, 0, 1705, 1066])
        mouse.api.GetWindowRect.assert_not_called()
        mouse.api.SendInput.assert_not_called()

    def immediate(self):
        return self.mouse.wait_for_game([0, 0, 1705, 1066], self.gate, timeout=0,
                                        notify=self.messages.append, locator=self.locator)

    def test_immediate_start_has_no_timed_wait_when_game_already_foreground(self):
        self.mouse.api.GetForegroundWindow.return_value = 7
        with patch.object(self.gate, "wait") as delay:
            self.assertEqual(self.immediate(), 7)
        delay.assert_not_called()
        self.mouse.api.SetForegroundWindow.assert_not_called()
        self.mouse.prepare.assert_not_called()  # Only actual button checks use WindowFromPoint.
        self.assertEqual(self.mouse.game_window, 7)

    def test_immediate_start_attempts_to_activate_only_the_identified_game_once(self):
        self.mouse.api.GetForegroundWindow.side_effect = [99, 7]
        self.mouse.api.SetForegroundWindow.return_value = True
        self.assertEqual(self.immediate(), 7)
        self.mouse.api.SetForegroundWindow.assert_called_once_with(7)
        self.assertIn("不要求从 P1", self.messages[-1])

    def test_activation_denied_explains_reason_without_waiting_or_clicking(self):
        self.mouse.api.GetForegroundWindow.return_value = 99
        self.mouse.api.SetForegroundWindow.return_value = False
        with patch.object(self.gate, "wait") as delay, self.assertRaisesRegex(RuntimeError, "系统未允许"):
            self.immediate()
        delay.assert_not_called()
        self.assertFalse(hasattr(self.mouse, "game_window"))

    def test_activation_success_must_be_verified_without_blind_retry(self):
        self.mouse.api.GetForegroundWindow.return_value = 99
        self.mouse.api.SetForegroundWindow.return_value = True
        with self.assertRaisesRegex(RuntimeError, "仍不在前台"):
            self.immediate()
        self.mouse.api.SetForegroundWindow.assert_called_once_with(7)

    def test_immediate_start_never_activates_when_region_is_stale(self):
        self.target["region"] = [50, 50, 1705, 1066]
        with self.assertRaisesRegex(RuntimeError, "自动定位游戏"):
            self.immediate()
        self.mouse.api.SetForegroundWindow.assert_not_called()

    def test_missing_game_fails_immediately_with_locator_reason(self):
        self.locator.side_effect = RuntimeError("找到 0 个匹配窗口")
        with self.assertRaisesRegex(RuntimeError, "0 个匹配窗口"):
            self.immediate()
        self.mouse.api.SetForegroundWindow.assert_not_called()

    def test_cancelled_immediate_start_never_activates_a_window(self):
        self.gate.cancel()
        with self.assertRaises(Cancelled):
            self.immediate()
        self.locator.assert_not_called()
        self.mouse.api.SetForegroundWindow.assert_not_called()

    def test_cancel_during_activation_prevents_recognition_start(self):
        self.mouse.api.GetForegroundWindow.return_value = 99

        def activation(window):
            self.gate.cancel()
            return True

        self.mouse.api.SetForegroundWindow.side_effect = activation
        with self.assertRaises(Cancelled):
            self.immediate()
        self.assertFalse(hasattr(self.mouse, "game_window"))

    def test_zero_countdown_and_zero_focus_wait_are_valid_defaults(self):
        config = validate_config({**load_config(), "step_countdown_seconds": 0, "focus_wait_seconds": 0})
        self.assertEqual(config["step_countdown_seconds"], 0)
        self.assertEqual(config["focus_wait_seconds"], 0)


class WorkerStartupTests(unittest.TestCase):
    def make_app(self):
        app = App.__new__(App)
        app.matcher, app.session = MagicMock(), MagicMock()
        app.config = {**load_config(), "region": [0, 0, 1705, 1066]}
        app.gate, app.events, app.frames = ActionGate(), queue.Queue(), queue.Queue(maxsize=2)
        return app

    def test_automatic_run_does_not_start_actions_when_focus_wait_fails(self):
        for error, status in ((RuntimeError("mock focus timeout"), "PAUSED"), (Cancelled("mock stop"), "CANCELLED")):
            with self.subTest(status=status):
                app, backend = self.make_app(), MagicMock()
                backend.wait_for_game.side_effect = error
                with patch("windows_message.WindowMessageMouse", return_value=backend), patch("windows_input.EmergencyWatch"), patch("ui.GameLoop") as loop:
                    app.flow_work(True)
                loop.return_value.run.assert_not_called()
                event = app.events.get_nowait()[1]
                self.assertEqual(event["kind"], "flow_done")
                self.assertEqual(event["result"]["status"], status)
                backend.click.assert_not_called()
                backend.escape.assert_not_called()

    def test_single_step_also_waits_without_starting_an_action_on_timeout(self):
        app, backend = self.make_app(), MagicMock()
        backend.wait_for_game.side_effect = RuntimeError("mock focus timeout")
        with patch("windows_message.WindowMessageMouse", return_value=backend), patch("windows_input.EmergencyWatch"), patch("ui.SingleStep") as step:
            app.step_work("P2", True, None)
        step.return_value.run.assert_not_called()
        event = app.events.get_nowait()[1]
        self.assertEqual(event["kind"], "step_done")
        self.assertEqual(event["result"]["status"], "PAUSED")

    def test_readonly_still_never_constructs_an_input_backend(self):
        app = self.make_app()
        with patch("windows_message.WindowMessageMouse") as backend, patch("ui.GameLoop") as loop:
            loop.return_value.run.return_value = {"status": "OBSERVED", "reason": "mock"}
            app.flow_work(False)
        backend.assert_not_called()
        loop.return_value.run.assert_called_once_with()

    def test_actual_worker_starts_controller_only_after_foreground_is_confirmed(self):
        app, backend, order = self.make_app(), MagicMock(), []
        backend.wait_for_game.side_effect = lambda *a, **k: order.append("focus_ready")
        with patch("windows_message.WindowMessageMouse", return_value=backend), patch("windows_input.EmergencyWatch"), patch("ui.GameLoop") as loop:
            loop.return_value.run.side_effect = lambda **k: (order.append("run"), {"status": "CANCELLED", "reason": "mock"})[1]
            app.flow_work(True)
        self.assertEqual(order, ["focus_ready", "run"])
        backend.wait_for_game.assert_called_once()
        backend.click.assert_not_called()
        backend.escape.assert_not_called()


if __name__ == "__main__":
    unittest.main()
