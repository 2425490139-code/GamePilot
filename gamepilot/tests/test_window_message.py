"""Window message regression; all Windows APIs are fakes, no desktop input."""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from actions import ActionGate, Cancelled
from windows_message import WindowMessageMouse


class WindowMessageTests(unittest.TestCase):
    def setUp(self):
        self.api = MagicMock()
        self.region = [100, 200, 1705, 1066]
        self.target = {"window": 7, "pid": 321, "region": self.region,
                       "class_name": "UnrealWindow", "process_name": "Client-Win64-Shipping.exe"}
        self.pid, self.window_class = 321, "UnrealWindow"
        self.api.GetForegroundWindow.return_value = 7
        self.api.SetForegroundWindow.return_value = False
        self.api.IsWindow.return_value = self.api.IsWindowVisible.return_value = True
        self.api.IsIconic.return_value = False
        self.api.WindowFromPoint.return_value = self.api.GetAncestor.return_value = 7
        self.api.PostMessageW.return_value = self.api.SetCursorPos.return_value = True
        self.api.MapVirtualKeyW.return_value = 1
        self.api.GetWindowTextW.side_effect = lambda h, b, n: setattr(b, "value", "鸣潮" if h == 7 else "其他窗口") or 1
        self.api.GetClassNameW.side_effect = lambda h, b, n: setattr(b, "value", self.window_class if h == 7 else "Other") or 1
        self.api.GetWindowThreadProcessId.side_effect = lambda h, p: setattr(p._obj, "value", self.pid if h == 7 else 999) or 1

        def rect(h, p):
            p._obj.left = p._obj.top = 0
            p._obj.right, p._obj.bottom = self.region[2:]
            return True

        def origin(h, p):
            p._obj.x, p._obj.y = self.region[:2]
            return True

        def local(h, p):
            p._obj.x -= self.region[0]
            p._obj.y -= self.region[1]
            return True

        self.api.GetClientRect.side_effect = rect
        self.api.ClientToScreen.side_effect = origin
        self.api.ScreenToClient.side_effect = local
        self.gate, self.sleep = ActionGate(), MagicMock()
        self.mouse = WindowMessageMouse(api=self.api, sleeper=self.sleep)
        self.addCleanup(lambda: self.api.SendInput.assert_not_called())

    def bind(self):
        return self.mouse.wait_for_game(self.region, self.gate, locator=lambda: self.target)

    def messages(self):
        return [call.args for call in self.api.PostMessageW.call_args_list]

    def test_binding_succeeds_even_when_foreground_activation_is_denied(self):
        self.api.GetForegroundWindow.return_value = 99
        self.assertEqual(self.bind(), 7)
        self.assertEqual(self.mouse.game_pid, 321)
        self.api.BringWindowToTop.assert_called_once_with(7)
        self.api.PostMessageW.assert_not_called()
        self.api.SetCursorPos.assert_not_called()
        self.sleep.assert_not_called()

    def test_foreground_game_receives_client_coordinates_not_desktop_coordinates(self):
        self.bind()
        self.gate.submit(self.mouse, (500, 600), self.region, 7)
        packed = (400 << 16) | 400
        self.assertEqual(self.messages(), [(7, 6, 1, 0), (7, 0x200, 0, packed),
                                          (7, 0x201, 1, packed), (7, 0x202, 0, packed)])
        self.api.SetCursorPos.assert_called_once_with(500, 600)

    def test_alt_tab_blocks_mouse_and_keyboard_before_submission(self):
        self.bind()
        self.api.GetForegroundWindow.return_value = 99
        for submit in (self.gate.submit, self.gate.submit_escape):
            with self.assertRaisesRegex(RuntimeError, "失去前台"):
                submit(self.mouse, (500, 600), self.region, 7)
        self.api.PostMessageW.assert_not_called()
        self.api.SetCursorPos.assert_not_called()

    def test_esc_is_sent_only_to_game_and_has_keyup_bits(self):
        self.bind()
        self.gate.submit_escape(self.mouse, (500, 600), self.region, 7)
        down = 1 | (1 << 16)
        self.assertEqual(self.messages(), [(7, 6, 1, 0), (7, 0x100, 27, down),
                                          (7, 0x101, 27, down | (3 << 30))])
        self.api.SetCursorPos.assert_not_called()

    def test_obscured_button_prevents_messages_and_cursor_movement(self):
        self.bind()
        self.api.GetAncestor.return_value = 99
        with self.assertRaisesRegex(RuntimeError, "遮挡"):
            self.gate.submit(self.mouse, (500, 600), self.region, 7)
        self.api.PostMessageW.assert_not_called()
        self.api.SetCursorPos.assert_not_called()

    def test_pid_or_window_class_replacement_prevents_input(self):
        for pid, name in ((322, "UnrealWindow"), (321, "#32770")):
            with self.subTest(pid=pid, name=name):
                self.pid, self.window_class = 321, "UnrealWindow"
                self.bind()
                self.pid, self.window_class = pid, name
                with self.assertRaisesRegex(RuntimeError, "身份已变化"):
                    self.mouse.click((500, 600), self.region, 7)
        self.api.PostMessageW.assert_not_called()

    def test_destroyed_or_minimized_window_prevents_input(self):
        self.bind()
        self.api.IsIconic.return_value = True
        with self.assertRaisesRegex(RuntimeError, "最小化"):
            self.mouse.click((500, 600), self.region, 7)
        self.api.PostMessageW.assert_not_called()

    def test_window_moved_since_binding_requires_new_detection(self):
        self.bind()
        self.region[0] += 10
        with self.assertRaisesRegex(RuntimeError, "位置或尺寸已变化"):
            self.mouse.click((500, 600), self.region, 7)
        self.api.PostMessageW.assert_not_called()

    def test_wrong_expected_window_or_outside_point_prevents_input(self):
        self.bind()
        for point, window in (((500, 600), 99), ((99, 600), 7)):
            with self.subTest(point=point), self.assertRaises(RuntimeError):
                self.mouse.click(point, self.region, window)
        self.api.PostMessageW.assert_not_called()

    def test_permission_error_is_reported_not_treated_as_success(self):
        self.bind()
        self.api.PostMessageW.return_value = False
        with patch("windows_message.ctypes.get_last_error", return_value=5):
            with self.assertRaisesRegex(RuntimeError, "相同权限"):
                self.mouse.click((500, 600), self.region, 7)
        self.assertEqual(self.messages(), [(7, 6, 1, 0)])
        self.api.SetCursorPos.assert_not_called()

    def test_coordinate_conversion_error_prevents_messages(self):
        self.bind()
        self.api.ScreenToClient.side_effect = None
        self.api.ScreenToClient.return_value = False
        with self.assertRaisesRegex(RuntimeError, "转换"):
            self.mouse.click((500, 600), self.region, 7)
        self.api.PostMessageW.assert_not_called()

    def test_up_is_attempted_even_if_delay_raises(self):
        self.bind()
        self.sleep.side_effect = RuntimeError("interrupted")
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.mouse.click((500, 600), self.region, 7)
        self.assertEqual(self.messages()[-1][1], 0x202)

    def test_release_failure_retries_release_only_and_still_reports_failure(self):
        self.bind()
        self.api.PostMessageW.side_effect = [True, True, True, False, True]
        with self.assertRaisesRegex(RuntimeError, "提交失败"):
            self.mouse.click((500, 600), self.region, 7)
        self.assertEqual([m[1] for m in self.messages()], [6, 0x200, 0x201, 0x202, 0x202])

    def test_stop_blocks_binding_and_click_submission(self):
        self.gate.cancel()
        with self.assertRaises(Cancelled):
            self.bind()
        self.api.BringWindowToTop.assert_not_called()
        self.gate.reset()
        self.bind()
        self.gate.cancel()
        with self.assertRaises(Cancelled):
            self.gate.submit(self.mouse, (500, 600), self.region, 7)
        self.api.PostMessageW.assert_not_called()


if __name__ == "__main__":
    unittest.main()
