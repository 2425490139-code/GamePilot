"""Regression from two real frames. Tests never inject desktop input."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "tests/fixtures/live_p2.png").is_file():
    raise unittest.SkipTest("Private live screenshots are not distributed")
sys.path.insert(0, str(ROOT))
from config import load_config
from capture import find_game_window
from diagnostics import network_reason
from matcher import Matcher
from actions import ActionGate, SingleStep
from state_machine import GameLoop, RestartFlow
from logger import Session


class LiveFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        cls.matcher = Matcher(cls.config)
        with Image.open(ROOT / "tests" / "fixtures" / "live_network_error.png") as image:
            cls.error_image = image.convert("RGB")
        with Image.open(ROOT / "tests" / "fixtures" / "live_p2.png") as image:
            cls.p2_image = image.convert("RGB")

    def test_real_p2_frame_and_actual_network_error_are_distinct(self):
        normal = self.matcher.detect(self.p2_image, with_ocr=False)
        self.assertEqual(normal["state"], "P2")
        self.assertFalse(normal["blocked_reason"])
        for image in (self.error_image, self.error_image.resize((1364, 853))):
            result = self.matcher.detect(image, with_ocr=False)
            self.assertEqual(result["state"], "WAIT_SCREEN")
            self.assertIn("网络", result["blocked_reason"])
            self.assertIsNone(result["button"])

    def test_network_text_has_explicit_context(self):
        for text in ("网 络 异 常，正 在 重 新 连 接 中…", "网络连接失败", "服务器连接失败"):
            self.assertIsNotNone(network_reason(text))
        for text in ("重新开始", "阶段 2/7", "进入下一天"):
            self.assertIsNone(network_reason(text))

    def test_same_grayscale_structure_with_wrong_colors_is_rejected(self):
        recolored = self.p2_image.convert("L").convert("RGB")
        result = self.matcher.detect(recolored, with_ocr=False)
        self.assertEqual(result["state"], "WAIT_SCREEN")
        self.assertGreater(result["candidates"]["P2"]["anchor"]["score"], .88)
        self.assertGreater(result["candidates"]["P2"]["anchor"]["color_error"], self.config["appearance_max_error"])

    def test_blocker_stops_loop_single_step_and_restart_without_input(self):
        with tempfile.TemporaryDirectory(prefix="gamepilot_blocker_") as folder:
            cfg = {**self.config, "region": [0, 0, 1705, 1066], "step_countdown_seconds": 0}
            gate, backend = ActionGate(), MagicMock()
            snapshot = lambda region: (self.error_image.copy(), (0, 0))
            single = SingleStep(self.matcher, cfg, gate, snapshot=snapshot, backend=backend, report_root=Path(folder) / "steps")
            self.assertEqual(single.run("P2", real=True)["status"], "PAUSED")
            loop = GameLoop(self.matcher, cfg, gate, backend, snapshot=snapshot, report_root=Path(folder) / "runs")
            result = loop.run(real=True, max_observations=2)
            self.assertEqual(result["status"], "PAUSED")
            self.assertEqual(result["observations"], 1)
            state = Session()
            restart = RestartFlow(self.matcher, cfg, gate, backend, state, snapshot=snapshot, report_root=Path(folder) / "p6")
            self.assertEqual(restart.run()["status"], "PAUSED")
            self.assertTrue(state.snapshot()["p6_armed"])
            backend.click.assert_not_called()
            backend.escape.assert_not_called()

    def fake_api(self, windows):
        api = MagicMock()
        api.IsWindowVisible.side_effect = lambda handle: windows[handle].get("visible", True)
        api.IsIconic.side_effect = lambda handle: windows[handle].get("minimized", False)
        api.GetWindowTextLengthW.side_effect = lambda handle: len(windows[handle]["title"])

        def title(handle, buffer, length):
            buffer.value = windows[handle]["title"]
            return len(buffer.value)

        def rectangle(handle, pointer):
            pointer._obj.left, pointer._obj.top = 0, 0
            pointer._obj.right, pointer._obj.bottom = windows[handle]["region"][2:]
            return True

        def point(handle, pointer):
            pointer._obj.x, pointer._obj.y = windows[handle]["region"][:2]
            return True

        def enumerate_windows(callback, parameter):
            for handle in windows:
                callback(handle, parameter)
            return True

        api.GetWindowTextW.side_effect = title
        def class_name(handle, buffer, length):
            buffer.value = windows[handle].get("class", "UnrealWindow")
            return len(buffer.value)
        api.GetClassNameW.side_effect = class_name
        api.GetClientRect.side_effect = rectangle
        api.ClientToScreen.side_effect = point
        api.EnumWindows.side_effect = enumerate_windows
        return api

    def test_window_locator_preserves_physical_pixels_and_ignores_minimized(self):
        windows = {7: {"title": "鸣潮  ", "region": [0, 0, 2560, 1600]},
                   8: {"title": "鸣潮", "region": [0, 0, 1920, 1080], "minimized": True},
                   9: {"title": "Other", "class": "OtherClass", "region": [20, 30, 500, 400]}}
        with patch("capture.ctypes.WinDLL", return_value=self.fake_api(windows)), patch("capture.enable_dpi_awareness"), patch("capture.window_process_name", return_value="Client-Win64-Shipping.exe"):
            result = find_game_window()
        self.assertEqual(result["region"], [0, 0, 2560, 1600])
        self.assertEqual(result["window"], 7)
        self.assertEqual(result["title"], "鸣潮")

    def test_window_locator_never_guesses_between_two_or_missing_windows(self):
        for windows in ({}, {7: {"title": "鸣潮", "region": [0, 0, 2560, 1600]},
                            8: {"title": "鸣潮", "region": [0, 0, 1920, 1080]}}):
            with self.subTest(count=len(windows)):
                with patch("capture.ctypes.WinDLL", return_value=self.fake_api(windows)), patch("capture.enable_dpi_awareness"), patch("capture.window_process_name", return_value="Client-Win64-Shipping.exe"):
                    with self.assertRaises(RuntimeError):
                        find_game_window()

    def test_window_locator_rejects_same_title_launcher(self):
        windows = {7: {"title": "鸣潮", "region": [0, 0, 1280, 720]}}
        for name in ("launcher_main.exe", None):
            with self.subTest(process=name):
                with patch("capture.ctypes.WinDLL", return_value=self.fake_api(windows)), patch("capture.enable_dpi_awareness"), patch("capture.window_process_name", return_value=name):
                    with self.assertRaises(RuntimeError):
                        find_game_window()

    def test_window_locator_uses_process_and_class_not_localized_title(self):
        windows = {7: {"title": "Wuthering Waves", "region": [10, 20, 1920, 1080]},
                   8: {"title": "鸣潮", "class": "#32770", "region": [0, 0, 500, 400]}}
        with patch("capture.ctypes.WinDLL", return_value=self.fake_api(windows)), patch("capture.enable_dpi_awareness"), patch("capture.window_process_name", return_value="Client-Win64-Shipping.exe"):
            self.assertEqual(find_game_window()["window"], 7)


if __name__ == "__main__":
    unittest.main()
