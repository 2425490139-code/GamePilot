"""Scripted screen/OCR observations, fake input only; not real game acceptance."""
import copy
import ctypes
import json
from pathlib import Path
import sys
import tempfile
import unittest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT.parent / "屏幕截图").is_dir():
    raise unittest.SkipTest("Private full-screen reference images are not distributed")
sys.path.insert(0, str(ROOT))
from config import load_config, REFERENCE_SIZE
from matcher import Matcher
from actions import ActionGate
from logger import Session
from state_machine import RestartFlow, GameLoop
from windows_input import WindowsMouse, INPUT


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += .05
        return self.value


class Mouse:
    def __init__(self, gate):
        self.events, self.gate = [], gate
        self.window = 7
        self.on_escape = None
        self.on_click = None

    def prepare(self, point, region):
        if self.window != 7:
            raise RuntimeError("mock focus changed")
        return self.window

    def escape(self, point, region, window):
        if self.prepare(point, region) != window:
            raise RuntimeError("mock stale window")
        self.events.append("ESC")
        if self.on_escape:
            self.on_escape()

    def click(self, point, region, window):
        if self.prepare(point, region) != window:
            raise RuntimeError("mock stale window")
        self.events.append("restart_click")
        if self.on_click:
            self.on_click()


class Scenario:
    images, results = {}, {}

    @classmethod
    def fixtures(cls):
        if cls.results:
            return
        matcher = Matcher(load_config())
        for state, spec in matcher.manifest.items():
            with Image.open(ROOT.parent / "屏幕截图" / spec["source"]) as image:
                cls.images[state] = image.convert("RGB").resize(REFERENCE_SIZE)
            cls.results[state] = matcher.detect(cls.images[state], with_ocr=False)
        cls.images["WAIT_SCREEN"] = Image.new("RGB", REFERENCE_SIZE, "black")
        cls.results["WAIT_SCREEN"] = matcher.detect(cls.images["WAIT_SCREEN"], with_ocr=False)

    def __init__(self, tokens):
        self.fixtures()
        self.tokens, self.index = tokens, 0
        self.dynamic = None

    def current(self):
        return self.dynamic() if self.dynamic else self.tokens[min(self.index, len(self.tokens) - 1)]

    def snapshot(self, region):
        return self.images[self.current()[0]].copy(), (0, 0)

    def detect(self, image, origin, with_ocr=True):
        state, stage = self.current()
        self.index += 1
        result = copy.deepcopy(self.results[state])
        if with_ocr:
            result.update(stage=stage, stage2_evidence=stage == 2,
                          ocr={"stage": stage, "text": f"MOCK 阶段:{stage}/7" if stage else "", "error": None})
        return result


class Phase3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gamepilot_p6_")
        self.addCleanup(self.temp.cleanup)
        self.config = {**load_config(), "region": [0, 0, *REFERENCE_SIZE], "step_countdown_seconds": 0,
                       "interval_ms": 1,
                       "step_sample_seconds": .001, "step_settle_seconds": .001,
                       "p6_menu_timeout_seconds": .4, "p6_restart_timeout_seconds": .6,
                       "unknown_timeout_seconds": .5, "unknown_max_frames": 3}
        self.gate = ActionGate()
        self.mouse = Mouse(self.gate)
        self.session = Session(Path(self.temp.name) / "session.json")

    def flow(self, scenario):
        return RestartFlow(scenario, self.config, self.gate, self.mouse, self.session,
                           snapshot=scenario.snapshot, clock=Clock(), report_root=Path(self.temp.name) / "p6")

    def loop(self, scenario, **kwargs):
        return GameLoop(scenario, self.config, self.gate, self.mouse, self.session,
                        snapshot=scenario.snapshot, clock=Clock(), report_root=Path(self.temp.name) / "runs", **kwargs)

    def success_scenario(self):
        return Scenario([("P1", 2)] * 3 + [("P6", None)] * 3 + [("P1", 1)] * 2)

    def test_restart_strict_sequence_and_verified_stage1(self):
        result = self.flow(self.success_scenario()).run()
        self.assertEqual(result["status"], "VERIFIED", result)
        self.assertEqual(self.mouse.events, ["ESC", "restart_click"])
        self.assertFalse(self.session.snapshot()["p6_armed"])
        self.assertEqual(self.session.snapshot()["restart_count"], 1)
        events = [x["event"] for x in result["events"]]
        self.assertLess(events.index("escape"), events.index("restart_click"))
        self.assertTrue((Path(result["report_dir"]) / "pause_menu.png").exists())

    def test_duplicate_stage2_is_blocked_across_new_instances(self):
        self.assertEqual(self.flow(self.success_scenario()).run()["status"], "VERIFIED")
        loaded = Session(Path(self.temp.name) / "session.json")
        self.assertFalse(loaded.snapshot()["p6_armed"])
        self.session = loaded
        result = self.flow(self.success_scenario()).run()
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(self.mouse.events, ["ESC", "restart_click"])

    def test_no_stage2_proof_no_input(self):
        for tokens in ([('P1', 1)], [('P4', None)], [('P1', 2), ('P1', 1)], [('WAIT_SCREEN', None)]):
            with self.subTest(tokens=tokens):
                result = self.flow(Scenario(tokens)).run()
                self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(self.mouse.events, [])
        self.assertTrue(self.session.snapshot()["p6_armed"])

    def test_esc_does_not_open_menu_bounded_retry(self):
        result = self.flow(Scenario([("P2", 2)])).run()
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(self.mouse.events, ["ESC", "ESC"])
        self.assertFalse(self.session.snapshot()["p6_armed"])

    def test_unknown_after_esc_is_not_toggled_again(self):
        result = self.flow(Scenario([("P1", 2)] * 3 + [("WAIT_SCREEN", None)])).run()
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(self.mouse.events, ["ESC"])

    def test_second_esc_allowed_only_with_new_stage2_proof(self):
        scenario = Scenario([("P2", 2)])
        scenario.dynamic = lambda: ("P1", 1) if "restart_click" in self.mouse.events else (
            ("P6", None) if self.mouse.events.count("ESC") >= 2 else ("P2", 2))
        result = self.flow(scenario).run()
        self.assertEqual(result["status"], "VERIFIED", result)
        self.assertEqual(self.mouse.events, ["ESC", "ESC", "restart_click"])

    def test_unknown_confirmation_and_unchanged_stage2_pause(self):
        for after in (("WAIT_SCREEN", None), ("P1", 2), ("P1", 3)):
            with self.subTest(after=after):
                self.session = Session()
                self.mouse.events.clear()
                result = self.flow(Scenario([("P1", 2)] * 3 + [("P6", None)] * 3 + [after])).run()
                self.assertEqual(result["status"], "PAUSED")
                self.assertEqual(self.mouse.events, ["ESC", "restart_click"])
                self.assertFalse(self.session.snapshot()["p6_armed"])

    def test_cancel_after_esc_does_not_click_restart(self):
        self.mouse.on_escape = self.gate.event.set
        result = self.flow(self.success_scenario()).run()
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(self.mouse.events, ["ESC"])

    def test_cancel_before_run_has_no_input(self):
        self.gate.cancel()
        result = self.flow(self.success_scenario()).run()
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(self.mouse.events, [])

    def test_pause_menu_disappears_before_click_no_click(self):
        scenario = Scenario([("P1", 2)] * 3 + [("P6", None)] * 2 + [("P2", 2)])
        result = self.flow(scenario).run()
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(self.mouse.events, ["ESC"])

    def test_p6_disabled_no_input(self):
        self.config["p6_enabled"] = False
        result = self.flow(self.success_scenario()).run()
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(self.mouse.events, [])

    def test_readonly_loop_never_calls_actions_or_disarms_latch(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("Readonly must not create an action controller")

        result = self.loop(Scenario([("P1", 2)]), step_factory=forbidden, restart_factory=forbidden).run(max_observations=4)
        self.assertEqual(result["status"], "OBSERVED")
        self.assertEqual(self.mouse.events, [])
        self.assertTrue(self.session.snapshot()["p6_armed"])

    def test_unknown_loop_times_out_without_click(self):
        result = self.loop(Scenario([("WAIT_SCREEN", None)])).run(real=True)
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(result["observations"], 3)
        self.assertEqual(self.mouse.events, [])

    def test_independent_pause_menu_never_triggers_restart(self):
        result = self.loop(Scenario([("P6", None)])).run(real=True)
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(self.mouse.events, [])

    def test_normal_loop_follows_observed_order_and_stops_on_failure(self):
        scenario = Scenario([("P5", None)] * 2 + [("P2", 1)] * 2 + [("P3", None)] * 2)
        calls = []

        class Step:
            def __init__(inner, *args, **kwargs):
                pass

            def run(inner, expected, real):
                calls.append(expected)
                return {"status": "VERIFIED" if len(calls) < 3 else "PAUSED", "reason": "scripted", "clicked": True}

        result = self.loop(scenario, step_factory=Step).run(real=True, max_observations=8)
        self.assertEqual(calls, ["P5", "P2", "P3"])
        self.assertEqual(result["status"], "PAUSED")

    def test_stage2_board_exit_then_restart_from_menu(self):
        calls = []

        class Step:
            def __init__(inner, *args, **kwargs):
                pass

            def run(inner, expected, real):
                calls.append(("step", expected))
                return {"status": "VERIFIED", "reason": "mock", "clicked": True, "fresh": {"stage": 2}}

        class Restart:
            def __init__(inner, *args, **kwargs):
                pass

            def run(inner, start_at_menu=False):
                calls.append(("restart", start_at_menu))
                return {"status": "VERIFIED", "reason": "mock"}

        self.loop(Scenario([("P2", 2)] * 2 + [("P6", None)] * 4),
                  step_factory=Step, restart_factory=Restart).run(real=True, max_observations=5)
        self.assertEqual(calls[0], ("step", "P2"))
        self.assertIn(("restart", True), calls, calls)

    def test_stage2_plan_page_is_clicked_not_restarted(self):
        calls = []

        class Step:
            def __init__(inner, *args, **kwargs):
                pass

            def run(inner, expected, real):
                calls.append(expected)
                return {"status": "VERIFIED", "reason": "mock", "clicked": True}

        def forbidden(*args, **kwargs):
            raise AssertionError("Stage 2 plan pages are clicked, not restarted")

        self.loop(Scenario([("P1", 2)]), step_factory=Step, restart_factory=forbidden).run(real=True, max_observations=2)
        self.assertEqual(calls, ["P1"])

    def test_pause_menu_without_exit_evidence_clicks_nothing(self):
        calls = []

        class Restart:
            def __init__(inner, *args, **kwargs):
                pass

            def run(inner, start_at_menu=False):
                calls.append(start_at_menu)
                return {"status": "VERIFIED", "reason": "mock"}

        result = self.loop(Scenario([("P6", None)]), restart_factory=Restart).run(real=True, max_observations=2)
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "PAUSED")

    def test_automatic_flow_can_start_at_p2_p3_or_p5_without_p1(self):
        for state, stage in (("P2", 1), ("P3", None), ("P5", None)):
            calls = []

            class Step:
                def __init__(inner, *args, **kwargs):
                    pass

                def run(inner, expected, real):
                    calls.append(expected)
                    return {"status": "PAUSED", "reason": "mock first action only"}

            with self.subTest(state=state):
                result = self.loop(Scenario([(state, stage)]), step_factory=Step).run(real=True, max_observations=2)
                self.assertEqual(calls, [state])
                self.assertEqual(result["status"], "PAUSED")

    def test_stage1_normal_loop_rearms_only_after_continuous_proof(self):
        self.session.trigger()

        class Step:
            def __init__(inner, *args, **kwargs):
                pass

            def run(inner, *args, **kwargs):
                return {"status": "VERIFIED", "reason": "mock", "fresh": {"stage": 1}}

        self.loop(Scenario([("P1", 1)]), step_factory=Step).run(real=True, max_observations=1)
        self.assertFalse(self.session.snapshot()["p6_armed"])
        self.loop(Scenario([("P1", 1)]), step_factory=Step).run(real=True, max_observations=2)
        self.assertTrue(self.session.snapshot()["p6_armed"])

    def test_corrupt_session_fails_closed(self):
        path = Path(self.temp.name) / "bad.json"
        path.write_text("broken", encoding="utf-8")
        self.assertFalse(Session(path).snapshot()["p6_armed"])

    def test_keyboard_batch_uses_esc_down_up_and_checks_focus(self):
        class API:
            def __init__(inner):
                inner.calls = []

            def SendInput(inner, count, inputs, size):
                inner.calls.append([(inputs[i].type, inputs[i].ki.wVk, inputs[i].ki.dwFlags) for i in range(count)])
                return count

        mouse = WindowsMouse.__new__(WindowsMouse)
        mouse.api = API()
        mouse.prepare = lambda *a: 7
        mouse.escape((10, 10), [0, 0, 100, 100], 7)
        self.assertEqual(mouse.api.calls, [[(1, 0x1B, 0), (1, 0x1B, 2)]])
        mouse.prepare = lambda *a: 8
        with self.assertRaises(RuntimeError):
            mouse.escape((10, 10), [0, 0, 100, 100], 7)
        self.assertEqual(len(mouse.api.calls), 1)


if __name__ == "__main__":
    unittest.main()
