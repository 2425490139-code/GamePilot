"""All input is a fake backend. No test submits an OS input event."""
import copy
import ctypes
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT.parent / "屏幕截图").is_dir():
    raise unittest.SkipTest("Private full-screen reference images are not distributed")
sys.path.insert(0, str(ROOT))
from actions import ActionGate, SingleStep, Cancelled, LABELS
from config import load_config
from matcher import Matcher
from ocr import parse_day, recognize_day
from windows_input import WindowsMouse, EmergencyWatch, INPUT


class FakeMouse:
    def __init__(self):
        self.calls = []
        self.window = 11
        self.fail = False

    def prepare(self, point, region):
        if self.fail:
            raise RuntimeError("前台窗口已变化")
        return self.window

    def click(self, point, region, expected_window):
        self.prepare(point, region)
        if self.window != expected_window:
            raise RuntimeError("前台窗口已变化")
        self.calls.append((point, region, expected_window))


class FastClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        self.now += .1
        return self.now


class Phase2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matcher = Matcher(load_config())
        cls.images, cls.results = {}, {}
        for state, spec in cls.matcher.manifest.items():
            with Image.open(ROOT.parent / "屏幕截图" / spec["source"]) as im:
                cls.images[state] = im.convert("RGB")
            result = cls.matcher.detect(cls.images[state], with_ocr=False)
            if state in ("P1", "P2"):
                result["stage"] = 1  # Mocked OCR; independent OCR integration is separate.
            cls.results[state] = result
        cls.images["WAIT_SCREEN"] = Image.new("RGB", (1705, 1066), "black")
        cls.results["WAIT_SCREEN"] = cls.matcher.detect(cls.images["WAIT_SCREEN"], with_ocr=False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gamepilot_test_")
        self.addCleanup(self.temp.cleanup)
        self.config = {**load_config(), "region": [0, 0, 1705, 1066],
                       "step_countdown_seconds": 0, "step_sample_seconds": .001,
                       "step_settle_seconds": .001, "step_verify_seconds": .8}
        self.gate = ActionGate()
        self.mouse = FakeMouse()

    def engine(self, states, mutate=None, snapshot_hook=None, day_reader=None):
        # Scripted recognition outcomes based on actual sample results. No OS capture/OCR.
        owner = self

        class ScriptedMatcher:
            def __init__(self):
                self.index = 0

            def detect(self, image, origin, with_ocr=True):
                index = self.index
                state = states[min(index, len(states) - 1)]
                self.index += 1
                result = copy.deepcopy(owner.results[state])
                if mutate:
                    mutate(index, result)
                return result

        matcher = ScriptedMatcher()

        def snapshot(region):
            index = matcher.index
            if snapshot_hook:
                snapshot_hook(index)
            state = states[min(index, len(states) - 1)]
            return owner.images[state].copy(), (0, 0)

        return SingleStep(matcher, self.config, self.gate, snapshot=snapshot, backend=self.mouse,
                          day_reader=day_reader or (lambda image, cfg: {"day": 1, "text": "mock", "error": None}),
                          clock=FastClock(), report_root=self.temp.name)

    def test_each_allowed_state_dry_run_never_clicks(self):
        for state in LABELS:
            with self.subTest(state=state):
                report = self.engine([state]).run(state)
                self.assertEqual(report["status"], "DRY_RUN")
                self.assertFalse(report["input_attempted"])
                self.assertTrue((Path(report["report_dir"]) / "before.png").exists())
        self.assertEqual(self.mouse.calls, [])

    def test_each_single_click_and_stable_transition(self):
        for before, after in (("P1", "P2"), ("P2", "P3"), ("P3", "P2"), ("P4", "P5"), ("P5", "P1")):
            with self.subTest(before=before):
                self.mouse.calls.clear()
                report = self.engine([before] * 4 + [after] * 2).run(before, real=True)
                self.assertEqual(report["status"], "VERIFIED", report)
                self.assertEqual(len(self.mouse.calls), 1)
                self.assertEqual(report["after"]["state"], after)
                saved = json.loads((Path(report["report_dir"]) / "result.json").read_text(encoding="utf-8"))
                self.assertTrue(saved["clicked"])

    def test_timeout_or_unknown_never_retries(self):
        for suffix in (["P5"], ["WAIT_SCREEN"], ["P6"]):
            with self.subTest(suffix=suffix):
                self.mouse.calls.clear()
                report = self.engine(["P5"] * 4 + suffix).run("P5", real=True)
                self.assertEqual(report["status"], "PAUSED")
                self.assertTrue(report["clicked"])
                self.assertEqual(len(self.mouse.calls), 1)

    def test_unstable_transition_is_not_success(self):
        report = self.engine(["P5"] * 4 + ["P1", "WAIT_SCREEN", "P1", "P2"]).run("P5", real=True)
        self.assertEqual(report["status"], "PAUSED")
        self.assertEqual(len(self.mouse.calls), 1)

    def test_unknown_wrong_state_and_p6_before_click(self):
        for observed, requested in (("WAIT_SCREEN", "P5"), ("P3", "P5"), ("P6", "P6")):
            with self.subTest(observed=observed):
                report = self.engine([observed]).run(requested, real=True)
                self.assertEqual(report["status"], "PAUSED")
        self.assertEqual(self.mouse.calls, [])

    def test_target_moves_or_wrong_label_or_out_of_bounds(self):
        def drift(index, result):
            if index:
                x, y = result["button"]["screen_center"]
                result["button"]["screen_center"] = (x + 15, y)

        def wrong_label(index, result):
            result["button"]["label"] = "购买"

        def out_of_bounds(index, result):
            result["button"]["screen_center"] = (9999, 9999)

        for mutation in (drift, wrong_label, out_of_bounds):
            report = self.engine(["P5"], mutate=mutation).run("P5", real=True)
            self.assertEqual(report["status"], "PAUSED")
        self.assertEqual(self.mouse.calls, [])

    def test_stage2_unknown_stage_and_stage_change_block_input(self):
        report = self.engine(["P1"], mutate=lambda i, r: r.update(stage=None)).run("P1", real=True)
        self.assertEqual(report["status"], "PAUSED")
        report = self.engine(["P1"], mutate=lambda i, r: r.update(stage=1 if i == 0 else 3)).run("P1", real=True)
        self.assertEqual(report["status"], "PAUSED")
        self.assertEqual(self.mouse.calls, [])

    def test_stage2_plan_page_is_clicked_instead_of_aborting(self):
        report = self.engine(["P1"] * 5 + ["P5"] * 2,
                             mutate=lambda i, r: r.update(stage=2)).run("P1", real=True)
        self.assertEqual(report["status"], "VERIFIED", report)
        self.assertEqual(len(self.mouse.calls), 1)

    def test_final_recapture_rejects_changed_page(self):
        report = self.engine(["P5"] * 3 + ["P3"]).run("P5", real=True)
        self.assertEqual(report["status"], "PAUSED")
        self.assertEqual(self.mouse.calls, [])

    def test_missing_region_and_focus_failure_block_input(self):
        self.config["region"] = None
        report = self.engine(["P5"]).run("P5", real=True)
        self.assertEqual(report["status"], "PAUSED")
        self.config["region"] = [0, 0, 1705, 1066]
        self.mouse.fail = True
        report = self.engine(["P5"]).run("P5", real=True)
        self.assertEqual(report["status"], "PAUSED")
        self.assertEqual(self.mouse.calls, [])

    def test_cancel_before_run_and_before_submission(self):
        self.gate.cancel()
        report = self.engine(["P5"]).run("P5", real=True)
        self.assertEqual(report["status"], "CANCELLED")
        self.gate.reset()
        report = self.engine(["P5"], snapshot_hook=lambda i: self.gate.cancel() if i == 3 else None).run("P5", real=True)
        self.assertEqual(report["status"], "CANCELLED")
        self.assertEqual(self.mouse.calls, [])

    def test_cancel_after_submission_stops_verification_without_new_click(self):
        original = self.mouse.click

        def click(*args):
            original(*args)
            self.gate.event.set()

        self.mouse.click = click
        report = self.engine(["P5"]).run("P5", real=True)
        self.assertEqual(report["status"], "CANCELLED")
        self.assertTrue(report["clicked"])
        self.assertEqual(len(self.mouse.calls), 1)

    def test_p2_same_screen_requires_day_advance_not_animation(self):
        values = iter((1, 2, 2))
        report = self.engine(["P2"], day_reader=lambda im, cfg: {"day": next(values), "text": "mock", "error": None}).run("P2", real=True)
        self.assertEqual(report["status"], "VERIFIED", report)
        self.assertEqual(report["observations"][-1]["evidence"], ("next_day", 2))
        self.mouse.calls.clear()
        report = self.engine(["P2"]).run("P2", real=True)
        self.assertEqual(report["status"], "PAUSED")
        self.assertEqual(len(self.mouse.calls), 1)

    def test_f8_monitor_cancels_and_joins(self):
        notified = threading.Event()
        with EmergencyWatch(self.gate.cancel, notified.set, reader=lambda: True) as watch:
            self.assertTrue(notified.wait(1))
            self.assertTrue(self.gate.event.is_set())
        self.assertFalse(watch.thread.is_alive())

    def test_f8_monitor_failure_fails_closed(self):
        def failure():
            raise RuntimeError("mock unavailable")

        with self.assertRaises(RuntimeError):
            with EmergencyWatch(self.gate.cancel, reader=failure):
                pass
        self.assertTrue(self.gate.event.is_set())

    def test_cancel_serializes_with_submission(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

        class Backend:
            def click(inner, *args):
                entered.set()
                release.wait(2)

        worker = threading.Thread(target=lambda: self.gate.submit(Backend(), (1, 1), [0, 0, 10, 10], 1))
        worker.start()
        self.assertTrue(entered.wait(1))
        stopper = threading.Thread(target=lambda: (self.gate.cancel(), stopped.set()))
        stopper.start()
        self.assertTrue(self.gate.event.wait(1))
        self.assertFalse(stopped.is_set())
        release.set()
        worker.join(2)
        stopper.join(2)
        self.assertTrue(stopped.is_set())
        with self.assertRaises(Cancelled):
            self.gate.submit(self.mouse, (1, 1), [0, 0, 10, 10], 1)

    def test_windows_batch_layout_and_partial_release_with_fake_api(self):
        class FakeAPI:
            def __init__(self):
                self.calls = []
                self.returned = 3

            def GetSystemMetrics(self, n):
                return {76: 0, 77: 0, 78: 1920, 79: 1080}[n]

            def SendInput(self, count, inputs, size):
                if count == 3:
                    self.calls.append([inputs[i].mi.dwFlags for i in range(3)])
                else:
                    self.calls.append(["release"])
                self.assert_size = size
                return self.returned

        mouse = WindowsMouse.__new__(WindowsMouse)
        mouse.api = FakeAPI()
        mouse.prepare = lambda p, r: 11
        mouse.click((100, 100), [0, 0, 1705, 1066], 11)
        self.assertEqual(mouse.api.calls[0], [0xC001, 2, 4])
        self.assertEqual(ctypes.sizeof(INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        mouse.api.returned = 2
        with self.assertRaises(RuntimeError):
            mouse.click((100, 100), [0, 0, 1705, 1066], 11)
        self.assertEqual(mouse.api.calls[-1], ["release"])

    def test_day_ocr_parser_rejects_conflicting_or_missing_days(self):
        self.assertEqual(parse_day("第 1 天"), 1)
        self.assertEqual(parse_day("第２天"), 2)
        for text in ("阶段：2/7", "第0天", "第1天 第2天", "今日收入 616"):
            self.assertIsNone(parse_day(text))
        with patch("ocr.recognize_text", return_value={"text": "第1天", "error": None}):
            self.assertEqual(recognize_day(self.images["P2"], self.config)["day"], 1)


if __name__ == "__main__":
    unittest.main()
