"""One requested click, fresh evidence, then verification. Never a loop of actions."""
from datetime import datetime
import json
import logging
import threading
import time
from uuid import uuid4
from pathlib import Path
from config import ROOT, DATA
from capture import capture, to_screen
from ocr import recognize_day
from rules import NORMAL_LABELS as LABELS, STAGE2_EXIT_BOX, STAGE2_EXIT_LABEL, STAGE2_PAGES
from stage_guard import compare_stage_pixels


EXPECTED_AFTER = {
    "P1": {"P2", "P5"},
    "P2": {"P1", "P3", "P4", "P5", "P6"},
    "P3": {"P2", "P4"},
    "P4": {"P1", "P2", "P5"},
    "P5": {"P1", "P2", "P3", "P4"},
}


class Cancelled(Exception):
    pass


class Stage2Detected(RuntimeError):
    pass


def preview_target(backend, result, gate):
    """Visual feedback runs outside the input lock, so stopping cannot deadlock Tk."""
    preview = getattr(backend, "preview_target", None)
    if preview is not None:
        gate.check()
        preview(result, gate)
        gate.check()


class ActionGate:
    """Cancellation and OS submission share a lock; no new submission after cancel returns."""
    def __init__(self, event=None):
        self.event = event if event is not None else threading.Event()
        self.lock = threading.Lock()

    def cancel(self):
        self.event.set()
        with self.lock:
            pass

    def reset(self):
        with self.lock:
            self.event.clear()

    def check(self):
        if self.event.is_set():
            raise Cancelled("用户停止，未安排新的操作")

    def wait(self, seconds):
        if self.event.wait(seconds):
            raise Cancelled("用户停止，未安排新的操作")

    def submit(self, backend, point, region, window):
        with self.lock:
            self.check()
            backend.click(point, region, window)

    def submit_escape(self, backend, point, region, window):
        with self.lock:
            self.check()
            backend.escape(point, region, window)


class SingleStep:
    def __init__(self, matcher, config, gate, snapshot=capture, backend=None,
                 day_reader=recognize_day, notify=lambda text: None, clock=time.monotonic,
                 report_root=None):
        self.matcher, self.config, self.gate = matcher, config, gate
        self.snapshot, self.backend, self.day_reader = snapshot, backend, day_reader
        self.notify, self.clock = notify, clock
        self.report_root = Path(report_root) if report_root is not None else DATA / "reports" / "steps"
        self.last_image = None
        self.last_result = None
        self.run_lock = threading.Lock()

    def sample(self, with_ocr=True):
        self.gate.check()
        image, origin = self.snapshot(self.config["region"])
        self.last_image = image
        result = self.matcher.detect(image, origin, with_ocr=with_ocr)
        self.last_result = result
        self.gate.check()
        if result.get("blocked_reason"):
            raise RuntimeError(result["blocked_reason"])
        return image, result

    def stage2_target(self, result):
        """Stage 2 on the board exits through the top-right X instead of ESC."""
        box = to_screen(STAGE2_EXIT_BOX, result["size"], result["origin"])
        return {"label": STAGE2_EXIT_LABEL, "screen_box": list(box),
                "screen_center": ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)}

    def validate(self, result, expected, real, require_stage=True):
        if expected not in LABELS or result["state"] != expected:
            raise RuntimeError(f"期望 {expected}，实际为 {result['state']}，没有点击")
        exiting = expected == "P2" and result.get("stage") == 2
        if exiting:
            result["button"] = self.stage2_target(result)
        button = result.get("button")
        wanted = STAGE2_EXIT_LABEL if exiting else LABELS[expected]
        if not button or button.get("label") != wanted:
            raise RuntimeError("目标按钮不在本阶段允许列表内")
        if result.get("stage") == 2 and result["state"] not in STAGE2_PAGES:
            raise RuntimeError(f"第 2 阶段出现在未知界面 {result['state']}，没有点击")
        if real and require_stage and expected in ("P1", "P2") and result.get("stage") is None:
            ocr = result.get("ocr", {})
            detail = ocr.get("error") or ocr.get("text") or "未读到文字"
            if "gamepilot" in detail.casefold():
                raise RuntimeError("阶段文字被 GamePilot 窗口遮挡，停止实际操作")
            raise RuntimeError(f"阶段 OCR 没有可靠证据，停止实际操作；OCR：{detail[:160]}")
        x, y = button["screen_center"]
        l, t, r, b = button["screen_box"]
        ox, oy = result["origin"]
        w, h = result["size"]
        if not (ox <= l <= x < r <= ox + w and oy <= t <= y < b <= oy + h):
            raise RuntimeError("目标或按钮边界超出截图区域")
        return (x, y)

    def run(self, expected, real=False):
        if not self.run_lock.acquire(blocking=False):
            return {"status": "PAUSED", "reason": "已有单步操作进行中", "clicked": False}
        report = {"status": "PAUSED", "expected": expected, "real": real, "clicked": False,
                  "input_attempted": False, "reason": "", "observations": [],
                  "backend": type(self.backend).__name__ if self.backend is not None else None}
        folder = self.report_root / f"{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid4().hex[:8]}"
        try:
            self.last_image = None
            self.last_result = None
            folder.mkdir(parents=True)
            if expected not in LABELS:
                raise RuntimeError("本阶段仅允许 P1～P5 的单步按钮测试，P6 留待第三阶段")
            self.gate.check()
            if real:
                if self.config["region"] is None:
                    raise RuntimeError("实际操作前必须保存明确的游戏截图区域")
                if self.backend is None:
                    raise RuntimeError("没有实际输入后端")
                for remaining in range(self.config["step_countdown_seconds"], 0, -1):
                    self.notify(f"{remaining} 秒后重新识别 {expected}，请将游戏置于前台；F8 紧急停止")
                    self.gate.wait(1)
            previous, window, before_day = None, None, None
            frames = max(1, int(self.config.get("step_confirm_frames", self.config["stable_frames"])))
            for index in range(frames):
                # Template matching alone proves the page is standing still. OCR is
                # deferred to the single pre-click confirmation below, because one
                # empty reading used to abort the whole step before any input.
                image, result = self.sample(with_ocr=not real)
                point = self.validate(result, expected, real, require_stage=False)
                if previous:
                    self.check_drift(previous, result)
                    if result.get("stage") != previous.get("stage"):
                        raise RuntimeError("连续检测阶段不一致，停止操作")
                if real:
                    current_window = self.backend.prepare(point, self.config["region"])
                    if window is not None and window != current_window:
                        raise RuntimeError("连续检测时前台窗口变化")
                    window = current_window
                previous = result
                if index + 1 < frames:
                    self.gate.wait(self.config["step_sample_seconds"])
            report["before"] = previous
            image.save(folder / "before.png", compress_level=1)
            if not real:
                report.update(status="DRY_RUN", reason=f"已稳定识别 {expected}，仅预演 {LABELS[expected]}，未发送输入")
                return report
            if expected == "P2":
                day = self.day_reader(image, self.config)
                before_day = day["day"]
                report["before_day_ocr"] = day
            # Re-capture immediately before submission; never reuse a preview/file coordinate.
            image, fresh = self.sample(with_ocr=not real)
            if real and expected in ("P1", "P2"):
                fresh, image = self.confirm_stage(expected, fresh, image)
                report["stage_source"] = fresh["ocr"].get("source")
            point = self.validate(fresh, expected, True)
            if not (expected == "P2" and fresh.get("stage") == 2):
                self.check_drift(previous, fresh)
            if (previous.get("stage") is not None and fresh.get("stage") is not None
                    and fresh.get("stage") != previous.get("stage")):
                raise RuntimeError("点击前阶段证据发生变化")
            report["fresh"] = fresh
            preview_target(self.backend, fresh, self.gate)
            # Clear the red outline before re-capturing. Stage pixels must remain
            # within tiny quantization noise; anchors/buttons are checked again.
            ocr_image = image
            image, immediate = self.sample(with_ocr=False)
            if expected in ("P1", "P2"):
                report["stage_pixel_guard"] = compare_stage_pixels(ocr_image, image, expected)
                if not report["stage_pixel_guard"]["unchanged"]:
                    raise RuntimeError("OCR 后阶段数字区域变化，拒绝使用旧阶段证据")
            immediate["stage"], immediate["stage2_evidence"] = fresh["stage"], fresh["stage2_evidence"]
            if expected == "P2" and fresh.get("stage") == 2:
                # Keep the exit target: the no-OCR frame has no stage of its own.
                immediate["button"] = dict(fresh["button"])
            point = self.validate(immediate, expected, True)
            self.check_drift(fresh, immediate)
            self.gate.check()
            report["input_attempted"] = True
            self.gate.submit(self.backend, point, self.config["region"], window)
            report["clicked"] = True
            self.notify(f"已单击 {LABELS[expected]}；验证画面变化中，不再点击")
            self.gate.wait(self.config["step_settle_seconds"])
            deadline = self.clock() + self.config["step_verify_seconds"]
            stable_key, count = None, 0
            while self.clock() < deadline:
                image, result = self.sample(with_ocr=False)
                evidence = None
                if result["state"] in EXPECTED_AFTER[expected]:
                    evidence = ("new_state", result["state"])
                elif expected == "P2" and result["state"] == "P2" and before_day is not None:
                    day = self.day_reader(image, self.config)
                    if day["day"] == before_day + 1:
                        evidence = ("next_day", day["day"])
                    result["day_ocr"] = day
                if self.clock() >= deadline:
                    break
                report["observations"].append({"state": result["state"], "evidence": evidence,
                                                "day_ocr": result.get("day_ocr")})
                if evidence is None:
                    stable_key, count = None, 0
                else:
                    count = count + 1 if evidence == stable_key else 1
                    stable_key = evidence
                if count >= self.config["stable_frames"]:
                    self.gate.check()
                    report.update(status="VERIFIED", reason="单击后连续检测到页面切换或天数递增；已停止，未执行后续动作", after=result)
                    return report
                self.gate.wait(self.config["step_sample_seconds"])
            raise RuntimeError("点击后未确认预期变化，已暂停；不会重试点击。未知画面/二次确认需人工检查")
        except Stage2Detected as exc:
            report.update(status="PAUSED", reason=str(exc), stage2_detected=True)
        except Cancelled as exc:
            report.update(status="CANCELLED", reason=str(exc))
        except Exception as exc:
            report.update(status="PAUSED", reason=str(exc))
            logging.warning("单步停止: %s", exc)
        finally:
            try:
                if self.last_image:
                    self.last_image.save(folder / "last.png", compress_level=1)
                report["last_recognition"] = self.last_result
                report["report_dir"] = str(folder)
                (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                logging.info("single_step %s", json.dumps(report, ensure_ascii=False))
            except Exception:
                logging.exception("单步报告写入失败")
            self.run_lock.release()
        return report

    def confirm_stage(self, expected, result, image, attempts=3):
        """Stage OCR is a hard gate for P1/P2, so give it bounded retries instead of
        discarding the step on a single empty reading."""
        for _ in range(max(1, attempts)):
            if result.get("stage") is not None:
                return result, image
            self.gate.wait(self.config["step_sample_seconds"])
            image, result = self.sample(with_ocr=True)
            if result["state"] != expected:
                raise RuntimeError("阶段确认期间页面已变化，没有点击")
        return result, image

    def check_drift(self, first, second):
        a, b = first["button"]["screen_center"], second["button"]["screen_center"]
        if max(abs(a[0] - b[0]), abs(a[1] - b[1])) > self.config["step_max_target_drift"]:
            raise RuntimeError("目标位置不稳定，停止操作")
        if first["origin"] != second["origin"] or first["size"] != second["size"]:
            raise RuntimeError("截图区域或尺寸变化，停止操作")
