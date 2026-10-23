"""BetterGI-inspired UI flow and real screenshot-noise regression, without OS input."""
import copy
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import load_config, REFERENCE_SIZE
from stage_guard import compare_stage_pixels, STAGE_BOXES
from actions import ActionGate, Cancelled, SingleStep, preview_target
from matcher import Matcher
from overlay import TargetOverlay, target_geometry
from ui import App


class StageGuardTests(unittest.TestCase):
    def test_real_stage_one_pair_with_one_level_noise_is_accepted(self):
        images = []
        for name in ("before", "last"):
            image = Image.new("RGB", REFERENCE_SIZE)
            with Image.open(ROOT / "tests" / "fixtures" / f"stage_guard_p2_{name}_crop.png") as crop:
                image.paste(crop, STAGE_BOXES["P2"][:2])
            images.append(image)
        result = compare_stage_pixels(*images, "P2")
        self.assertTrue(result["unchanged"], result)
        self.assertEqual(result["max_channel_delta"], 1)
        self.assertGreater(result["changed_pixels"], 0)

    def test_tiny_noise_is_bounded_per_channel_not_averaged(self):
        before = Image.new("RGB", REFERENCE_SIZE, (80, 80, 80))
        after = Image.new("RGB", REFERENCE_SIZE, (82, 81, 79))
        self.assertTrue(compare_stage_pixels(before, after, "P2")["unchanged"])
        after.putpixel((170, 90), (83, 80, 80))
        result = compare_stage_pixels(before, after, "P2")
        self.assertFalse(result["unchanged"])
        self.assertEqual(result["max_channel_delta"], 3)

    def test_changed_digit_is_rejected_for_both_stage_locations(self):
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 26)
        for state in ("P1", "P2"):
            frames = []
            left, top, _, _ = STAGE_BOXES[state]
            for digit in (1, 2, 3):
                image = Image.new("RGB", REFERENCE_SIZE, "black")
                ImageDraw.Draw(image).text((left, top), f"{digit}/7", font=font, fill="white")
                frames.append(image)
            self.assertFalse(compare_stage_pixels(frames[0], frames[1], state)["unchanged"])
            self.assertFalse(compare_stage_pixels(frames[1], frames[2], state)["unchanged"])

    def test_unknown_page_or_changed_size_does_not_reuse_stage_proof(self):
        image = Image.new("RGB", REFERENCE_SIZE)
        self.assertFalse(compare_stage_pixels(image, image, "P5")["unchanged"])
        self.assertFalse(compare_stage_pixels(image, image.resize((1364, 853)), "P2")["unchanged"])


class VisualFeedbackTests(unittest.TestCase):
    def app(self):
        app = App.__new__(App)
        app.root, app.target_overlay = MagicMock(), MagicMock()
        app.events, app.gate = queue.Queue(), ActionGate()
        app.controls_hidden = threading.Event()
        app.stop_event = app.gate.event
        app.active_target_request, app.closing, app.auto_minimized = None, False, False
        return app

    def request(self):
        return {"box": [100, 200, 300, 260], "done": threading.Event(),
                "cancelled": threading.Event(), "errors": []}

    def test_target_rectangle_preserves_screen_origin_and_margin(self):
        self.assertEqual(target_geometry([100, 200, 300, 260]), (94, 194, 212, 72))
        self.assertEqual(target_geometry([-100, -20, -20, 20]), (-106, -26, 92, 52))
        with self.assertRaises(ValueError):
            target_geometry([100, 200, 50, 260])

    def test_tk_outline_is_transparent_disabled_and_cleared_after_use(self):
        # Widgets are mocks: this test never opens a screen overlay.
        with patch("overlay.tk.Toplevel") as top, patch("overlay.tk.Canvas") as canvas:
            overlay = TargetOverlay(MagicMock())
            overlay.show([100, 200, 300, 260])
            top.return_value.wm_attributes.assert_any_call("-disabled", True)
            top.return_value.wm_attributes.assert_any_call("-transparentcolor", "#ff00ff")
            canvas.return_value.create_rectangle.assert_called_once_with(3, 3, 209, 69, outline="#ff2020", width=3)
            overlay.hide()
            overlay.close()
            top.return_value.withdraw.assert_called()
            top.return_value.destroy.assert_called_once()

    def test_red_box_is_hidden_before_worker_receives_completion(self):
        app, request = self.app(), self.request()
        app.show_target_request(request)
        app.target_overlay.show.assert_called_once_with(request["box"])
        self.assertFalse(request["done"].is_set())
        delay, finish = app.root.after.call_args.args
        self.assertEqual(delay, 90)
        finish()
        app.target_overlay.hide.assert_called_once()
        self.assertTrue(request["done"].is_set())
        self.assertEqual(request["errors"], [])

    def test_stop_or_expired_request_never_shows_a_red_box(self):
        for cancelled in (False, True):
            app, request = self.app(), self.request()
            if cancelled:
                request["cancelled"].set()
            else:
                app.gate.cancel()
            app.show_target_request(request)
            app.target_overlay.show.assert_not_called()
            self.assertTrue(request["done"].is_set())

    def test_show_or_hide_failure_is_returned_to_worker_without_click(self):
        for method in ("show", "hide"):
            app, request = self.app(), self.request()
            getattr(app.target_overlay, method).side_effect = RuntimeError("mock display failure")
            app.show_target_request(request)
            if method == "hide":
                app.root.after.call_args.args[1]()
            self.assertTrue(request["done"].is_set())
            self.assertTrue(request["errors"])

    def test_cancel_hides_active_box_and_late_timer_does_not_hide_next_box(self):
        app, first, second = self.app(), self.request(), self.request()
        app.show_target_request(first)
        late_finish = app.root.after.call_args.args[1]
        app.cancel_target()
        self.assertTrue(first["cancelled"].is_set())
        app.show_target_request(second)
        app.target_overlay.hide.reset_mock()
        late_finish()
        app.target_overlay.hide.assert_not_called()
        self.assertIs(app.active_target_request, second)

    def test_worker_wait_for_red_box_is_cancellable_without_input_lock(self):
        gate, backend = ActionGate(), MagicMock()

        def cancel(result, current_gate):
            self.assertTrue(current_gate.lock.acquire(blocking=False))
            current_gate.lock.release()
            current_gate.cancel()

        backend.preview_target.side_effect = cancel
        with self.assertRaises(Cancelled):
            preview_target(backend, {"button": {}}, gate)
        backend.click.assert_not_called()

    def test_worker_rejects_red_box_timeout(self):
        app = self.app()
        with patch("ui.time.monotonic", side_effect=[0, 3]):
            with self.assertRaisesRegex(RuntimeError, "红框.*超时"):
                app.flash_target({"state": "P2", "button": {"screen_box": [100, 200, 300, 260]}}, app.gate)
        request = app.events.get_nowait()[1]["request"]
        self.assertTrue(request["cancelled"].is_set())

    def test_minimize_and_restore_controls_without_touching_game(self):
        app = self.app()
        app.root.state.return_value = "iconic"
        app.minimize_for_run()
        app.root.iconify.assert_called_once()
        self.assertTrue(app.auto_minimized)
        app.restore_controls()
        app.root.deiconify.assert_called_once()
        self.assertFalse(app.auto_minimized)


@unittest.skipUnless((ROOT.parent / "屏幕截图").is_dir(), "Private reference images are not distributed")
class FinalCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        matcher = Matcher(load_config())
        cls.images, cls.results = {}, {}
        for state in ("P5", "P2", "P3"):
            with Image.open(ROOT.parent / "屏幕截图" / matcher.manifest[state]["source"]) as image:
                cls.images[state] = image.convert("RGB").resize(REFERENCE_SIZE)
            cls.results[state] = matcher.detect(cls.images[state], with_ocr=False)

    def test_target_changes_during_red_box_feedback_prevent_click(self):
        with tempfile.TemporaryDirectory() as folder:
            gate, backend, matcher = ActionGate(), MagicMock(), MagicMock()
            current = ["P5"]
            backend.prepare.return_value = 7
            backend.preview_target.side_effect = lambda *args: current.__setitem__(0, "P3")
            matcher.detect.side_effect = lambda *a, **k: copy.deepcopy(self.results[current[0]])
            cfg = {**load_config(), "region": [0, 0, *REFERENCE_SIZE], "step_countdown_seconds": 0,
                   "step_sample_seconds": .001}
            step = SingleStep(matcher, cfg, gate, backend=backend,
                              snapshot=lambda region: (self.images[current[0]], (0, 0)), report_root=folder)
            outcome = step.run("P5", real=True)
            self.assertEqual(outcome["status"], "PAUSED", outcome)
            backend.preview_target.assert_called_once()
            backend.click.assert_not_called()
            self.assertGreaterEqual(matcher.detect.call_count, 4)


if __name__ == "__main__":
    unittest.main()
