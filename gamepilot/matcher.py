"""Local template matching. OpenCV optional; NumPy FFT NCC is the offline fallback."""
import json
import numpy as np
from PIL import Image, ImageDraw
from config import ROOT, REFERENCE_SIZE
from capture import to_screen
from ocr import recognize_stage
from diagnostics import diagnose_overlay
from rules import validate_manifest

try:
    import cv2
except ImportError:
    cv2 = None


def ncc(search, template):
    if cv2 is not None:
        return cv2.matchTemplate(search.astype(np.float32), template.astype(np.float32), cv2.TM_CCOEFF_NORMED)
    h, w = template.shape
    ih, iw = search.shape
    centered = template - template.mean()
    energy = np.sum(centered * centered)
    if energy < 1e-6:
        return np.zeros((ih - h + 1, iw - w + 1))
    shape = (ih + h - 1, iw + w - 1)
    cross = np.fft.irfft2(np.fft.rfft2(search, s=shape) * np.fft.rfft2(centered[::-1, ::-1], s=shape), s=shape)
    numerator = cross[h - 1:ih, w - 1:iw]

    def boxsum(a):
        integral = np.pad(a, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        return integral[h:, w:] - integral[:-h, w:] - integral[h:, :-w] + integral[:-h, :-w]

    variance = np.maximum(boxsum(search * search) - boxsum(search) ** 2 / (h * w), 0)
    denominator = np.sqrt(variance * energy)
    result = np.zeros_like(numerator)
    np.divide(numerator, denominator, out=result, where=denominator > 1e-6)
    return np.clip(result, -1, 1)


class Matcher:
    def __init__(self, config):
        self.config = config
        directory = ROOT / config["template_directory"]
        self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        validate_manifest(self.manifest, REFERENCE_SIZE)
        self.templates = {}
        self.color_templates = {}
        blocker_manifest = directory / "blockers.json"
        self.blockers = []
        if blocker_manifest.exists():
            for spec in json.loads(blocker_manifest.read_text(encoding="utf-8")):
                with Image.open(directory / spec["file"]) as im:
                    self.blockers.append((spec, np.asarray(im.convert("L"), dtype=np.float64)))
        d = config["match_downsample"]
        for state in self.manifest:
            for kind in ("anchor", "button"):
                with Image.open(directory / f"{state.lower()}_{kind}.png") as im:
                    gray = im.convert("L")
                    self.templates[state, kind] = np.asarray(gray.resize((max(2, gray.width // d), max(2, gray.height // d))), dtype=np.float64)
                    self.color_templates[state, kind] = np.asarray(im.convert("RGB").resize((max(2, gray.width // d), max(2, gray.height // d))), dtype=np.float64)

    def locate(self, gray, normalized, state, kind):
        d = self.config["match_downsample"]
        expected = self.manifest[state][kind]
        margin = self.config["search_margin"]
        x0 = max(0, int(expected[0] - REFERENCE_SIZE[0] * margin))
        y0 = max(0, int(expected[1] - REFERENCE_SIZE[1] * margin))
        x1 = min(REFERENCE_SIZE[0], int(expected[2] + REFERENCE_SIZE[0] * margin))
        y1 = min(REFERENCE_SIZE[1], int(expected[3] + REFERENCE_SIZE[1] * margin))
        search = gray.crop((x0, y0, x1, y1))
        search = np.asarray(search.resize((search.width // d, search.height // d)), dtype=np.float64)
        template = self.templates[state, kind]
        scores = ncc(search, template).copy()
        threshold = self.config["template_thresholds"].get(state, {}).get(kind, self.config[f"{kind}_threshold"])
        target = self.color_templates[state, kind]
        candidates, eligible = [], []
        # Distinct spatial peaks only: neighboring pixels of one button are not
        # separate buttons. Appearance is checked BEFORE ranking candidates.
        radius_y, radius_x = max(1, template.shape[0] // 2), max(1, template.shape[1] // 2)
        for index in range(self.config["match_max_candidates"]):
            y, x = np.unravel_index(np.argmax(scores), scores.shape)
            score = float(scores[y, x])
            if index and score < threshold:
                break
            left, top = x0 + int(x) * d, y0 + int(y) * d
            box = [left, top, left + expected[2] - expected[0], top + expected[3] - expected[1]]
            patch = np.asarray(gray.crop(box).resize((template.shape[1], template.shape[0])), dtype=np.float64)
            rgb = np.asarray(normalized.crop(box).resize((target.shape[1], target.shape[0])), dtype=np.float64)
            appearance_error = float(np.mean(np.abs(patch - template)) / 255)
            color_error = float(np.mean(np.abs(rgb - target)) / 255)
            candidate = {"score": round(score, 4), "appearance_error": round(appearance_error, 4),
                         "color_error": round(color_error, 4), "box": box}
            candidates.append(candidate)
            if (score >= threshold and appearance_error <= self.config["appearance_max_error"]
                    and color_error <= self.config["appearance_max_error"]):
                eligible.append(candidate)
            scores[max(0, y - radius_y):y + radius_y + 1, max(0, x - radius_x):x + radius_x + 1] = -np.inf
            if score < threshold:
                break
        limited = len(candidates) == self.config["match_max_candidates"] and float(np.max(scores)) >= threshold
        ambiguous = (len(eligible) > 1
                     and eligible[0]["score"] - eligible[1]["score"] <= self.config["match_ambiguity_margin"])
        best = (eligible or candidates)[0].copy()
        best.update(eligible=bool(eligible) and not ambiguous and not limited, ambiguous=ambiguous,
                    candidate_limit_reached=limited, alternatives=candidates)
        return best

    def detect(self, image, origin=(0, 0), with_ocr=True):
        aspect_error = abs((image.width / image.height) / (REFERENCE_SIZE[0] / REFERENCE_SIZE[1]) - 1)
        base = {"state": "WAIT_SCREEN", "stage": None, "stage2_evidence": False,
                "blocked_reason": None,
                "button": None, "candidates": {}, "ocr": {"stage": None, "text": "", "error": None},
                "size": list(image.size), "origin": list(origin), "mode": "仅识别，不点击"}
        if aspect_error > .04:
            base["reason"] = "截图宽高比不符，请设置仅包含游戏画面的区域（不含黑边/标题栏）"
            return base
        normalized = image.convert("RGB").resize(REFERENCE_SIZE)
        gray = normalized.convert("L")
        for spec, template in self.blockers:
            roi = spec["search_box"]
            search = np.asarray(gray.crop(roi), dtype=np.float64)
            scores = ncc(search, template)
            y, x = np.unravel_index(np.argmax(scores), scores.shape)
            if float(scores[y, x]) >= spec["threshold"]:
                base.update(blocked_reason=spec["reason"], reason=spec["reason"],
                            blocker={"name": spec["name"], "score": round(float(scores[y, x]), 4)})
                return base
        valid = []
        for state in self.manifest:
            anchor, button = self.locate(gray, normalized, state, "anchor"), self.locate(gray, normalized, state, "button")
            base["candidates"][state] = {"anchor": anchor, "button": button}
            if anchor["eligible"] and button["eligible"]:
                valid.append(state)
        base["ambiguous_targets"] = [f"{state}.{kind}" for state, matches in base["candidates"].items()
                                     for kind, match in matches.items()
                                     if match["ambiguous"] or match["candidate_limit_reached"]]
        if len(valid) != 1:
            if valid:
                base["reason"] = "多个界面同时匹配，等待复核"
            else:
                base["reason"] = "目标有多个相似位置或候选超限，等待复核" if base["ambiguous_targets"] else "无可靠匹配"
            if not valid and with_ocr:
                base["diagnostic"] = diagnose_overlay(normalized, self.config)
                if base["diagnostic"]["blocked_reason"]:
                    base.update(blocked_reason=base["diagnostic"]["blocked_reason"], reason=base["diagnostic"]["blocked_reason"])
            return base
        state = base["state"] = valid[0]
        button = base["candidates"][state]["button"].copy()
        button["label"] = self.manifest[state]["label"]
        button["screen_box"] = to_screen(button["box"], image.size, origin)
        box = button["screen_box"]
        button["screen_center"] = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        base["button"] = button
        if with_ocr:
            base["ocr"] = recognize_stage(normalized, state, self.config)
        base["stage"] = base["ocr"]["stage"]
        base["stage2_evidence"] = base["stage"] == 2
        base["reason"] = "已匹配页面锚点和按钮；此结果不执行操作"
        return base


def annotate(image, result, read_only=True, max_width=None):
    out = image.convert("RGB").copy()
    scale = 1.0
    if max_width and out.width > max_width:
        scale = max_width / out.width
        out = out.resize((max_width, max(1, int(out.height * scale))))
    draw = ImageDraw.Draw(out)
    state = result["state"]
    if result["button"]:
        for kind, color in (("anchor", "cyan"), ("button", "lime")):
            box = result["candidates"][state][kind]["box"]
            mapped = to_screen(box, image.size, (0, 0))
            if scale != 1.0:
                mapped = tuple(round(value * scale) for value in mapped)
            draw.rectangle(mapped, outline=color, width=3)
    draw.rectangle((0, 0, min(out.width, 520), 32), fill="black")
    mode = "READ ONLY" if read_only else "INPUT ENABLED"
    draw.text((8, 8), f"{state} | stage={result['stage']} | {mode}", fill="white")
    return out
