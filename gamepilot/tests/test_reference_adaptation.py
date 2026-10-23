"""Reference-inspired candidate selection, using synthetic images and fake input only."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import load_config, validate_config, REFERENCE_SIZE
from matcher import Matcher
from rules import validate_manifest


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.config = {**load_config(), "match_downsample": 1}
        self.matcher = Matcher.__new__(Matcher)
        self.matcher.config = self.config
        self.matcher.manifest = {"P5": {"button": [550, 480, 590, 504]}}
        rng = np.random.default_rng(27)
        # Orange texture: a grayscale copy has nearly perfect NCC but wrong color.
        pattern = rng.integers(-20, 21, (24, 40, 1))
        self.rgb = np.uint8(np.array([230, 65, 25]) + pattern)
        self.template = Image.fromarray(self.rgb)
        self.matcher.templates = {("P5", "button"): np.asarray(self.template.convert("L"), dtype=np.float64)}
        self.matcher.color_templates = {("P5", "button"): self.rgb.astype(np.float64)}
        self.image = Image.new("RGB", REFERENCE_SIZE)

    def locate(self):
        return self.matcher.locate(self.image.convert("L"), self.image, "P5", "button")

    def test_appearance_is_filtered_before_confidence_ranking(self):
        self.image.paste(self.template.convert("L").convert("RGB"), (490, 480))
        noisy = np.uint8(np.clip(self.rgb.astype(int) + np.random.default_rng(3).integers(-2, 3, self.rgb.shape), 0, 255))
        self.image.paste(Image.fromarray(noisy), (590, 480))
        result = self.locate()
        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["box"][:2], [590, 480])
        self.assertGreater(result["alternatives"][0]["color_error"], self.config["appearance_max_error"])
        self.assertGreater(result["alternatives"][0]["score"], result["score"])

    def test_two_similar_valid_positions_are_ambiguous(self):
        for x in (490, 590):
            self.image.paste(self.template, (x, 480))
        result = self.locate()
        self.assertTrue(result["ambiguous"], result)
        self.assertFalse(result["eligible"])

    def test_single_peak_does_not_count_neighboring_pixels_as_buttons(self):
        self.image.paste(self.template, (550, 480))
        result = self.locate()
        self.assertTrue(result["eligible"], result)
        self.assertFalse(result["ambiguous"])
        self.assertEqual(result["box"][:2], [550, 480])

    def test_candidate_overflow_cannot_choose_an_unchecked_target(self):
        self.config["match_max_candidates"] = 2
        for x in (470, 540, 610):
            self.image.paste(self.template, (x, 480))
        result = self.locate()
        self.assertTrue(result["candidate_limit_reached"], result)
        self.assertFalse(result["eligible"])

    def test_weak_or_wrong_color_candidates_have_no_action(self):
        self.assertFalse(self.locate()["eligible"])
        self.image.paste(self.template.convert("L").convert("RGB"), (550, 480))
        result = self.locate()
        self.assertGreaterEqual(result["score"], self.config["button_threshold"])
        self.assertFalse(result["eligible"])

    def test_numpy_fallback_applies_the_same_candidate_filters(self):
        for x in (490, 590):
            self.image.paste(self.template, (x, 480))
        with patch("matcher.cv2", None):
            result = self.locate()
        self.assertTrue(result["ambiguous"], result)
        self.assertFalse(result["eligible"])


@unittest.skipUnless((ROOT.parent / "屏幕截图").is_dir(), "Private reference images are not distributed")
class PlanConstraintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matcher = Matcher(load_config())

    def test_manifest_cannot_replace_skip_close_or_restart_with_other_actions(self):
        for state, label in (("P3", "确认"), ("P5", "购买"), ("P5", "隐藏当前界面"), ("P6", "再来一次")):
            with self.subTest(state=state, label=label):
                manifest = copy.deepcopy(self.matcher.manifest)
                manifest[state]["label"] = label
                with self.assertRaises(ValueError):
                    validate_manifest(manifest, REFERENCE_SIZE)
        validate_manifest(self.matcher.manifest, REFERENCE_SIZE)

    def test_unlisted_features_and_invalid_template_bounds_are_rejected(self):
        for changes in (lambda m: m.update(garden_get_gold={}), lambda m: m.pop("P6"),
                        lambda m: m["P5"].update(button=[-1, 0, 40, 40])):
            manifest = copy.deepcopy(self.matcher.manifest)
            changes(manifest)
            with self.assertRaises(ValueError):
                validate_manifest(manifest, REFERENCE_SIZE)

    def test_two_real_page_features_still_wait_instead_of_ranking_pages(self):
        image = Image.new("RGB", REFERENCE_SIZE)
        for state in ("P2", "P5"):
            spec = self.matcher.manifest[state]
            for kind in ("anchor", "button"):
                with Image.open(ROOT / "templates" / f"{state.lower()}_{kind}.png") as template:
                    image.paste(template, tuple(spec[kind][:2]))
        result = self.matcher.detect(image, with_ocr=False)
        self.assertEqual(result["state"], "WAIT_SCREEN", result)
        self.assertIsNone(result["button"])
        self.assertIn("多个界面", result["reason"])

    def test_duplicate_real_restart_button_never_exposes_a_click_target(self):
        spec = self.matcher.manifest["P6"]
        image = Image.new("RGB", REFERENCE_SIZE)
        with Image.open(ROOT / "templates" / "p6_anchor.png") as template:
            image.paste(template, tuple(spec["anchor"][:2]))
        with Image.open(ROOT / "templates" / "p6_button.png") as template:
            for dx in (-64, 64):
                image.paste(template, (spec["button"][0] + dx, spec["button"][1]))
        result = self.matcher.detect(image, with_ocr=False)
        self.assertEqual(result["state"], "WAIT_SCREEN", result)
        self.assertIsNone(result["button"])
        self.assertIn("P6.button", result["ambiguous_targets"])

    def test_candidate_configuration_rejects_unbounded_or_invalid_values(self):
        for key, value in (("match_max_candidates", 1), ("match_max_candidates", 33),
                           ("match_max_candidates", True), ("match_ambiguity_margin", float("nan")),
                           ("match_ambiguity_margin", -.01), ("match_ambiguity_margin", .2)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_config({**load_config(), key: value})


if __name__ == "__main__":
    unittest.main()
