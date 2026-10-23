import sys
import unittest
from pathlib import Path
import numpy as np
from PIL import Image, ImageEnhance

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT.parent / "屏幕截图").is_dir():
    raise unittest.SkipTest("Private full-screen reference images are not distributed")
sys.path.insert(0, str(ROOT))
from config import load_config
from capture import to_screen
from matcher import Matcher, ncc
from ocr import parse_stage


class Phase1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matcher = Matcher(load_config())

    def test_existing_six_screens(self):
        for state, spec in self.matcher.manifest.items():
            with self.subTest(state=state), Image.open(ROOT.parent / "屏幕截图" / spec["source"]) as im:
                result = self.matcher.detect(im, with_ocr=False)
                self.assertEqual(result["state"], state)
                self.assertIsNone(result["stage"])
                self.assertFalse(result["stage2_evidence"])

    def test_blank_and_noise_are_unknown(self):
        rng = np.random.default_rng(8)
        frames = [Image.new("RGB", (1705, 1066), color) for color in ("black", "white", "gray")]
        frames.append(Image.fromarray(rng.integers(0, 256, (1066, 1705, 3), dtype=np.uint8)))
        for im in frames:
            result = self.matcher.detect(im, with_ocr=False)
            self.assertEqual(result["state"], "WAIT_SCREEN")
            self.assertIsNone(result["button"])

    def test_scale_and_brightness(self):
        for state, spec in self.matcher.manifest.items():
            with self.subTest(state=state), Image.open(ROOT.parent / "屏幕截图" / spec["source"]) as im:
                scaled = ImageEnhance.Brightness(im.resize((1364, 853))).enhance(.85)
                result = self.matcher.detect(scaled, origin=(100, 60), with_ocr=False)
                self.assertEqual(result["state"], state)
                x, y = result["button"]["screen_center"]
                self.assertTrue(100 <= x < 1464 and 60 <= y < 913)

    def test_single_button_is_not_a_page(self):
        for state, spec in self.matcher.manifest.items():
            with self.subTest(state=state):
                image = Image.new("RGB", (1705, 1066), "black")
                with Image.open(ROOT / "templates" / f"{state.lower()}_button.png") as button:
                    image.paste(button, tuple(spec["button"][:2]))
                self.assertEqual(self.matcher.detect(image, with_ocr=False)["state"], "WAIT_SCREEN")

    def test_dimmed_unknown_overlay_cannot_match_background_buttons(self):
        from PIL import ImageDraw
        spec = self.matcher.manifest["P2"]
        with Image.open(ROOT.parent / "屏幕截图" / spec["source"]) as image:
            overlay = ImageEnhance.Brightness(image).enhance(.55)
        ImageDraw.Draw(overlay).rectangle((500, 300, 1200, 700), fill="white")
        result = self.matcher.detect(overlay, with_ocr=False)
        self.assertEqual(result["state"], "WAIT_SCREEN")
        self.assertIsNone(result["button"])

    def test_template_can_move_within_search_region(self):
        for state, spec in self.matcher.manifest.items():
            with self.subTest(state=state), Image.open(ROOT.parent / "屏幕截图" / spec["source"]) as im:
                shifted = Image.new("RGB", im.size)
                shifted.paste(im, (12, 8))
                result = self.matcher.detect(shifted, with_ocr=False)
                self.assertEqual(result["state"], state)
                expected = spec["button"]
                actual = result["button"]["box"]
                self.assertLessEqual(abs(actual[0] - expected[0] - 12), 4)
                self.assertLessEqual(abs(actual[1] - expected[1] - 8), 4)

    def test_aspect_mismatch_is_unknown(self):
        result = self.matcher.detect(Image.new("RGB", (1920, 1080)), with_ocr=False)
        self.assertEqual(result["state"], "WAIT_SCREEN")

    def test_stage_context_and_conflicts(self):
        for text in ("乐 园 阶 段 2 / 7 目 标", "阶段：2/7", "阶段：２／７"):
            self.assertEqual(parse_stage(text), 2)
        for text in ("第2天", "2/7", "第2阶段收益结算", "阶段：8/7", "阶段：2/7 阶段：1/7"):
            self.assertIsNone(parse_stage(text))
        self.assertEqual(parse_stage("乐园阶段 1/7 目标"), 1)

    def test_coordinate_origin_scaling(self):
        self.assertEqual(to_screen((10, 20, 30, 40), (3410, 2132), (-100, 50)), (-80, 90, -40, 130))

    def test_ncc_peak_and_constant(self):
        rng = np.random.default_rng(99)
        search = rng.normal(size=(30, 40))
        template = search[7:14, 13:22].copy()
        scores = ncc(search, template)
        self.assertEqual(np.unravel_index(np.argmax(scores), scores.shape), (7, 13))
        self.assertAlmostEqual(float(scores[7, 13]), 1., places=5)
        self.assertTrue(np.isfinite(ncc(np.zeros((20, 20)), template)).all())


if __name__ == "__main__":
    unittest.main()
