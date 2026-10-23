"""Reject stale stage evidence while allowing tiny screenshot quantization noise."""
import numpy as np
from config import REFERENCE_SIZE

STAGE_BOXES = {"P1": (895, 254, 975, 305), "P2": (165, 85, 225, 126)}


def compare_stage_pixels(before, after, state):
    if state not in STAGE_BOXES or before.size != after.size:
        return {"unchanged": False, "reason": "阶段画面类型或截图尺寸变化"}
    box = STAGE_BOXES[state]
    first = np.asarray(before.convert("RGB").resize(REFERENCE_SIZE).crop(box), dtype=np.int16)
    second = np.asarray(after.convert("RGB").resize(REFERENCE_SIZE).crop(box), dtype=np.int16)
    delta = np.abs(first - second)
    maximum = int(delta.max())
    # Per-channel bound, NOT average error: a changed digit cannot be diluted
    # by an otherwise static background. Never relax thresholds for animation.
    return {"unchanged": maximum <= 2, "max_channel_delta": maximum,
            "changed_pixels": int(np.any(delta > 0, axis=2).sum()), "tolerance": 2,
            "reason": "阶段数字区域仅有微小像素噪声或未变" if maximum <= 2 else "阶段数字区域发生明显变化"}
