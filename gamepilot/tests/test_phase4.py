import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config
from logger import Session


class Phase4Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gamepilot_config_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_configuration_round_trip_and_region_preserves_other_options(self):
        with patch.object(config, "DATA", self.root), patch.object(config, "ROOT", self.root):
            config.save_config({"p6_enabled": False, "anchor_threshold": .91,
                                "template_thresholds": {"P5": {"button": .94}}})
            config.save_region([10, 20, 1705, 1066])
            loaded = config.load_config()
            self.assertEqual(loaded["region"], [10, 20, 1705, 1066])
            self.assertFalse(loaded["p6_enabled"])
            self.assertEqual(loaded["template_thresholds"]["P5"]["button"], .94)
            self.assertNotIn("real_enabled", loaded)

    def test_bad_configuration_cannot_overwrite_valid_file(self):
        with patch.object(config, "DATA", self.root), patch.object(config, "ROOT", self.root):
            config.save_config({})
            path = self.root / "configs" / "settings.json"
            original = path.read_bytes()
            cases = ({"region": [True, 0, 1, 1]}, {"region": [0, 0, -1, 1]},
                     {"region": 12}, {"anchor_threshold": float("nan")},
                     {"stable_frames": 1}, {"stable_frames": 2.5},
                     {"p6_esc_max_attempts": 0}, {"step_countdown_seconds": -1},
                     {"template_directory": "../outside"}, {"template_thresholds": {"P5": {"button": 2}}},
                     {"emergency_key": "F9"}, {"ocr_enabled": "yes"})
            for changes in cases:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    config.save_config(changes)
                self.assertEqual(path.read_bytes(), original)

    def test_session_updates_preserve_latch_and_timestamps_across_resume(self):
        path = self.root / "session.json"
        state = Session(path)
        self.assertTrue(state.trigger())
        stamp = state.snapshot()["p6_triggered_at"]
        state.update(last_action="暂停")
        resumed = Session(path)
        self.assertFalse(resumed.trigger())
        self.assertEqual(resumed.snapshot()["p6_triggered_at"], stamp)
        resumed.rearm()
        self.assertTrue(Session(path).snapshot()["p6_armed"])

    def test_wrong_session_types_fail_closed(self):
        path = self.root / "session.json"
        for value in (None, [], {"p6_armed": "false", "restart_count": 1}):
            with self.subTest(value=value):
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assertFalse(Session(path).snapshot()["p6_armed"])


if __name__ == "__main__":
    unittest.main()
