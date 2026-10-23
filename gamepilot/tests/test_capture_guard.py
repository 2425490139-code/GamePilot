"""Control-window occlusion regressions, with fake UI and screenshot APIs."""
import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from actions import ActionGate, Cancelled, SingleStep
from ui import App


class CaptureGuardTests(unittest.TestCase):
    def setUp(self):
        self.app = App.__new__(App)
        self.app.gate, self.app.events = ActionGate(), queue.Queue()
        self.app.stop_event = self.app.gate.event
        self.app.root = MagicMock()
        self.app.root.state.return_value = "normal"
        self.app.root.iconify.side_effect = lambda: setattr(self.app.root.state, "return_value", "iconic")
        self.app.closing, self.app.auto_minimized = False, False

    def request(self):
        return {"done": threading.Event(), "cancelled": threading.Event(), "errors": []}

    def test_restored_controls_pause_without_auto_hiding(self):
        request = self.request()
        self.app.prepare_capture_request(request)
        self.app.root.iconify.assert_not_called()
        self.assertTrue(request["done"].is_set())
        self.assertTrue(request["errors"])
        self.assertTrue(self.app.stop_event.is_set())

    def test_already_hidden_controls_need_no_extra_hide(self):
        self.app.root.state.return_value = "iconic"
        request = self.request()
        self.app.prepare_capture_request(request)
        self.app.root.iconify.assert_not_called()
        self.assertFalse(request["errors"])

    def test_late_request_cannot_hide_controls_after_stop_or_timeout(self):
        for cancelled in (True, False):
            request = self.request()
            if cancelled:
                request["cancelled"].set()
            else:
                self.app.gate.cancel()
            self.app.prepare_capture_request(request)
            self.assertTrue(request["done"].is_set())
            self.assertTrue(request["errors"])
        self.app.root.iconify.assert_not_called()

    def test_hidden_capture_does_not_need_ui_ack(self):
        self.app.controls_hidden = threading.Event()
        self.app.controls_hidden.set()
        with patch("ui.capture", return_value=("frame", (0, 0))) as grab:
            self.assertEqual(self.app.capture_for_run([0, 0, 2560, 1600]), ("frame", (0, 0)))
        grab.assert_called_once()
        self.assertTrue(self.app.events.empty())

    def test_visible_controls_block_capture(self):
        self.app.controls_hidden = threading.Event()
        with patch("ui.capture") as grab, self.assertRaises(Cancelled):
            self.app.capture_for_run([0, 0, 2560, 1600])
        grab.assert_not_called()

    def test_restore_during_capture_discards_frame(self):
        self.app.controls_hidden = threading.Event()
        self.app.controls_hidden.set()
        def restored(region):
            self.app.controls_hidden.clear()
            return "frame", (0, 0)
        with patch("ui.capture", side_effect=restored), self.assertRaises(Cancelled):
            self.app.capture_for_run([0, 0, 2560, 1600])

    def test_stop_blocks_capture(self):
        self.app.controls_hidden = threading.Event()
        self.app.controls_hidden.set()
        self.app.gate.cancel()
        with patch("ui.capture") as grab, self.assertRaises(Cancelled):
            self.app.capture_for_run([0, 0, 2560, 1600])
        grab.assert_not_called()

    def test_ocr_reading_tool_title_reports_occlusion_without_moving_mouse(self):
        engine = SingleStep.__new__(SingleStep)
        result = {"state": "P2", "stage": None, "button": {"label": "进入下一天"},
                  "ocr": {"text": "GamePilot ． 窗 囗 绑 定 版 ． 实 际 作 、", "error": None}}
        with self.assertRaisesRegex(RuntimeError, "GamePilot 窗口遮挡"):
            engine.validate(result, "P2", True)


if __name__ == "__main__":
    unittest.main()
