"""Local recognition and single-step settings; real input is never persisted."""
import json
import sys
from pathlib import Path


def _frozen():
    return bool(getattr(sys, "frozen", False))


def _resource_dir():
    """Where templates and the OCR PowerShell scripts live (read-only)."""
    if _frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def _data_dir():
    """Where reports and settings are written (must be writable)."""
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _resource_dir()
DATA = _data_dir()
# Shipped with the code; the user's own settings live next to the program.
SHIPPED_CONFIG = ROOT / "configs" / "settings.json"
REFERENCE_SIZE = (1705, 1066)
DEFAULT = {
    "region": None,
    "interval_ms": 50,
    "stable_frames": 2,
    "anchor_threshold": 0.88,
    "button_threshold": 0.90,
    "search_margin": 0.055,
    "match_downsample": 2,
    "ocr_enabled": True,
    "ocr_timeout_seconds": 12,
    "step_countdown_seconds": 0,
    "focus_wait_seconds": 0,
    "step_sample_seconds": 0.05,
    "step_settle_seconds": 0.05,
    "step_verify_seconds": 6,
    "step_max_target_drift": 6,
    "p6_enabled": True,
    "p6_esc_max_attempts": 2,
    "p6_menu_timeout_seconds": 5,
    "p6_restart_timeout_seconds": 12,
    "unknown_timeout_seconds": 10,
    "unknown_max_frames": 12,
    "emergency_key": "F8",
    "template_directory": "templates",
    "template_thresholds": {},
    "appearance_max_error": 0.16,
    "match_max_candidates": 8,
    "match_ambiguity_margin": 0.02,
}


def load_config(path=None):
    path = Path(path) if path else DATA / "configs" / "settings.json"
    config = {**DEFAULT, **(json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})}
    return validate_config(config)


def validate_config(config):
    import math
    for key in ("interval_ms", "stable_frames", "match_downsample", "step_countdown_seconds", "p6_esc_max_attempts", "unknown_max_frames", "match_max_candidates"):
        if type(config[key]) is not int:
            raise ValueError(f"{key} 必须为整数")
    for key in ("anchor_threshold", "button_threshold", "search_margin", "ocr_timeout_seconds", "step_sample_seconds", "step_settle_seconds", "step_verify_seconds", "step_max_target_drift", "p6_menu_timeout_seconds", "p6_restart_timeout_seconds", "unknown_timeout_seconds", "appearance_max_error", "match_ambiguity_margin", "focus_wait_seconds"):
        if type(config[key]) not in (int, float) or not math.isfinite(config[key]):
            raise ValueError(f"{key} 必须为有限数值")
    region = config["region"]
    if region is not None and (not isinstance(region, (list, tuple)) or len(region) != 4 or not all(type(v) is int for v in region)
                               or region[2] <= 0 or region[3] <= 0):
        raise ValueError("截图区域必须为 [左, 上, 宽, 高]，宽高须大于零")
    for key in ("anchor_threshold", "button_threshold"):
        if not 0 < config[key] <= 1:
            raise ValueError(f"{key} 必须在 0～1 之间")
    if not 50 <= config["interval_ms"] <= 60000 or not 2 <= config["stable_frames"] <= 5:
        raise ValueError("检测间隔至少 50ms，连续确认次数至少 2 次")
    if config["match_downsample"] not in (1, 2, 3, 4):
        raise ValueError("match_downsample 必须为 1～4")
    if not 0 <= config["search_margin"] <= .2 or config["ocr_timeout_seconds"] <= 0:
        raise ValueError("搜索范围或 OCR 超时配置无效")
    if not 0 <= config["step_countdown_seconds"] <= 30:
        raise ValueError("操作倒计时必须为 0～30 秒，0 表示不倒计时")
    if not 0 <= config["focus_wait_seconds"] <= 120:
        raise ValueError("等待游戏前台时间必须为 0～120 秒，0 表示立即检查并尝试切回游戏")
    if not .05 <= config["step_sample_seconds"] <= 2 or not .05 <= config["step_settle_seconds"] <= 5:
        raise ValueError("单步检测/等待间隔超出范围")
    if not 1 <= config["step_verify_seconds"] <= 30 or not 0 <= config["step_max_target_drift"] <= 20:
        raise ValueError("单步验证超时或目标移动阈值无效")
    if type(config["p6_enabled"]) is not bool or not 1 <= config["p6_esc_max_attempts"] <= 3:
        raise ValueError("P6 开关或 ESC 尝试次数无效")
    for key in ("p6_menu_timeout_seconds", "p6_restart_timeout_seconds", "unknown_timeout_seconds"):
        if not 1 <= config[key] <= 60:
            raise ValueError(f"{key} 必须为 1～60 秒")
    if not 2 <= config["unknown_max_frames"] <= 100:
        raise ValueError("连续未知画面次数必须为 2～100")
    if config["emergency_key"] != "F8":
        raise ValueError("当前独立紧急停止键固定为 F8")
    if type(config["ocr_enabled"]) is not bool:
        raise ValueError("ocr_enabled 必须为布尔值")
    if not 0 < config["appearance_max_error"] <= .25:
        raise ValueError("appearance_max_error 必须在 0～0.25 之间")
    if not 2 <= config["match_max_candidates"] <= 32 or not 0 <= config["match_ambiguity_margin"] <= .1:
        raise ValueError("候选数量必须为 2～32，歧义置信度差必须为 0～0.1")
    if not isinstance(config["template_directory"], str):
        raise ValueError("模板目录必须为本地项目路径")
    directory = (ROOT / config["template_directory"]).resolve()
    if not directory.is_relative_to(ROOT):
        raise ValueError("模板必须保存在项目内，不访问网络或其他目录")
    if not isinstance(config["template_thresholds"], dict):
        raise ValueError("template_thresholds 必须为对象")
    for state, values in config["template_thresholds"].items():
        if state not in ("P1", "P2", "P3", "P4", "P5", "P6") or not isinstance(values, dict):
            raise ValueError("模板阈值状态无效")
        for key, value in values.items():
            if key not in ("anchor", "button") or type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError("模板阈值必须为 0～1 的有限数值")
    return config


def save_region(region):
    save_config({"region": region})


def save_config(changes):
    config = validate_config({**load_config(), **changes})
    (DATA / "configs").mkdir(exist_ok=True)
    path = DATA / "configs" / "settings.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
