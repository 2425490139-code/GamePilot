"""Recognize blocking messages; never reconnects or sends input."""
import re
from ocr import recognize_text


def network_reason(text):
    compact = re.sub(r"\s+", "", text)
    phrases = ("网络异常", "正在重新连接", "网络连接失败", "服务器连接失败", "连接已断开")
    if any(phrase in compact for phrase in phrases):
        return "游戏提示网络异常/重新连接，已暂停；不重复点击、不调整网络"
    return None


def diagnose_overlay(image, config):
    crop = image.crop((420, 440, 1300, 690))
    result = recognize_text(crop, config)
    return {**result, "blocked_reason": network_reason(result["text"]) if not result["error"] else None}
