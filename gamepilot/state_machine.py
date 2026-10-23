"""Screen-driven loop and priority restart flow. Unknown screens never cause input."""
from pathlib import Path
import time
from config import ROOT, DATA
from capture import capture
from actions import SingleStep, LABELS, Cancelled, preview_target
from stage_guard import compare_stage_pixels
from logger import Session, Evidence


class RestartFlow:
    def __init__(self, matcher, config, gate, backend, session, snapshot=capture,
                 notify=lambda text: None, clock=time.monotonic, report_root=None):
        self.matcher, self.config, self.gate = matcher, config, gate
        self.backend, self.session, self.snapshot = backend, session, snapshot
        self.notify, self.clock = notify, clock
        self.report_root = Path(report_root) if report_root else DATA / "reports" / "p6"
        self.last_image = None

    def sample(self, with_ocr=True):
        self.gate.check()
        image, origin = self.snapshot(self.config["region"])
        self.last_image = image
        result = self.matcher.detect(image, origin, with_ocr=with_ocr)
        self.gate.check()
        if result.get("blocked_reason"):
            raise RuntimeError(result["blocked_reason"])
        return image, result

    def target(self, result):
        button = result.get("button")
        if not button:
            raise RuntimeError("P6 缺少可定位的目标，已暂停")
        x, y = button["screen_center"]
        l, t, r, b = button["screen_box"]
        ox, oy = result["origin"]
        w, h = result["size"]
        if not (ox <= l <= x < r <= ox + w and oy <= t <= y < b <= oy + h):
            raise RuntimeError("P6 目标超出截图范围")
        return (x, y)

    def stage2(self):
        previous, window, ocr_image = None, None, None
        confirmed, tries, limit = 0, 0, max(4, self.config["stable_frames"] * 3)
        while confirmed < self.config["stable_frames"]:
            tries += 1
            if tries > limit:
                raise RuntimeError(f"连续 {limit} 次采样都没有读到第 2 阶段证据，未发送 ESC")
            image, result = self.sample()
            if result["state"] not in ("P1", "P2"):
                raise RuntimeError(f"发送 ESC 前第 2 阶段画面已消失（当前 {result['state']}），未发送 ESC")
            if result["stage"] != 2 or result["ocr"]["error"]:
                # A single unreadable frame is not evidence of stage 1: drop the
                # streak and look again instead of aborting the whole run.
                confirmed = 0
                self.gate.wait(self.config["step_sample_seconds"])
                continue
            point = self.target(result)
            active = self.backend.prepare(point, self.config["region"])
            if previous is not None and (result["state"] != previous["state"] or active != window):
                raise RuntimeError("第 2 阶段检测期间页面或窗口变化，已暂停")
            previous, window, ocr_image = result, active, image
            confirmed += 1
            if confirmed < self.config["stable_frames"]:
                self.gate.wait(self.config["step_sample_seconds"])
        # The final non-OCR frame protects against an OCR result that is already stale.
        image, immediate = self.sample(with_ocr=False)
        if immediate["state"] != previous["state"]:
            raise RuntimeError("发送 ESC 前页面已变化")
        if immediate["origin"] != previous["origin"] or immediate["size"] != previous["size"]:
            raise RuntimeError("发送 ESC 前截图区域变化")
        if not compare_stage_pixels(ocr_image, image, previous["state"])["unchanged"]:
            raise RuntimeError("发送 ESC 前阶段数字变化")
        return self.target(immediate), window, previous

    def wait_menu(self, window):
        deadline, stable, previous = self.clock() + self.config["p6_menu_timeout_seconds"], 0, None
        while self.clock() < deadline:
            _, result = self.sample(with_ocr=False)
            if result["state"] == "P6" and result["button"]["label"] == "重新开始":
                point = self.target(result)
                if self.backend.prepare(point, self.config["region"]) != window:
                    raise RuntimeError("暂停菜单不属于原游戏窗口")
                if previous and max(abs(a - b) for a, b in zip(point, previous)) <= self.config["step_max_target_drift"]:
                    stable += 1
                else:
                    stable = 1
                previous = point
                if stable >= self.config["stable_frames"]:
                    return result
            else:
                stable, previous = 0, None
            self.gate.wait(self.config["step_sample_seconds"])
        return None

    def run(self, start_at_menu=False):
        evidence = Evidence(self.report_root, "restart")
        attempted, clicked = False, False
        try:
            if not self.config["p6_enabled"]:
                raise RuntimeError("P6 已关闭；暂停菜单不会自动点击")
            if self.config["region"] is None or self.backend is None:
                raise RuntimeError("P6 实际执行需要明确截图区域和输入后端")
            if not start_at_menu and not self.session.snapshot()["p6_armed"]:
                raise RuntimeError("本次第 2 阶段的 P6 已触发，禁止重复；等待明确阶段 1 证据")
            if start_at_menu:
                # The board's top-right X already opened the menu: no ESC needed.
                _, opened = self.sample(with_ocr=False)
                if opened["state"] != "P6" or opened["button"]["label"] != "重新开始":
                    raise RuntimeError("暂停菜单已消失，没有点击重新开始")
                point = self.target(opened)
                window = self.backend.prepare(point, self.config["region"])
                evidence.event("start_at_menu", state=opened["state"])
            else:
                point, window, phase = self.stage2()
                evidence.image("stage2", self.last_image)
                if not self.session.trigger():
                    raise RuntimeError("P6 触发锁已关闭")
                evidence.event("trigger", state=phase["state"], stage=phase["stage"], text=phase["ocr"]["text"])
                for attempt in range(self.config["p6_esc_max_attempts"]):
                    if attempt:
                        # Only retry ESC when the screen still explicitly shows stage 2;
                        # never toggle ESC based on a timer or an unknown popup.
                        point, active, phase = self.stage2()
                        if active != window:
                            raise RuntimeError("ESC 重试前窗口变化")
                    self.gate.check()
                    attempted = True
                    self.gate.submit_escape(self.backend, point, self.config["region"], window)
                    self.session.update(last_action="P6：已发送 ESC，等待暂停菜单")
                    evidence.event("escape", attempt=attempt + 1)
                    self.notify("P6：已发送 ESC，等待连续确认暂停菜单")
                    menu = self.wait_menu(window)
                    if menu is not None:
                        break
                else:
                    raise RuntimeError("ESC 有限次数尝试后仍未识别暂停菜单，已暂停")
            if start_at_menu:
                menu = self.wait_menu(window)
                if menu is None:
                    raise RuntimeError("暂停菜单未稳定出现，没有点击重新开始")
            evidence.image("pause_menu", self.last_image)
            preview_target(self.backend, menu, self.gate)
            # Fresh recognition again after PNG writing; never click a stale menu.
            _, fresh = self.sample(with_ocr=False)
            if fresh["state"] != "P6" or fresh["button"]["label"] != "重新开始":
                raise RuntimeError("准备点击重新开始时暂停菜单已消失")
            a, b = self.target(menu), self.target(fresh)
            if max(abs(x - y) for x, y in zip(a, b)) > self.config["step_max_target_drift"]:
                raise RuntimeError("重新开始按钮位置不稳定")
            self.gate.submit(self.backend, b, self.config["region"], window)
            clicked = True
            self.session.update(last_action="P6：已点击重新开始，验证阶段 1")
            evidence.event("restart_click")
            self.notify("P6：已点击重新开始，等待明确的阶段 1；未知二次确认将暂停")
            self.gate.wait(self.config["step_settle_seconds"])
            deadline, stable, previous = self.clock() + self.config["p6_restart_timeout_seconds"], 0, None
            while self.clock() < deadline:
                _, result = self.sample()
                evidence.event("verify", state=result["state"], stage=result["stage"], ocr=result["ocr"])
                if self.clock() >= deadline:
                    break
                if result["state"] in ("P1", "P2") and result["stage"] == 1 and not result["ocr"]["error"]:
                    key = (result["state"], result["stage"])
                    stable = stable + 1 if key == previous else 1
                    previous = key
                    if stable >= self.config["stable_frames"]:
                        self.gate.check()
                        info = self.session.snapshot()
                        self.session.update(p6_result="VERIFIED", restart_count=info["restart_count"] + 1,
                                            last_action="P6：已验证重新开始回到阶段 1")
                        # Keep latch closed here. Normal loop must also establish phase 1.
                        evidence.image("restart_verified", self.last_image)
                        return evidence.finish(status="VERIFIED", reason="已验证回到阶段 1；恢复界面检测", clicked=clicked)
                else:
                    stable, previous = 0, None
                self.gate.wait(self.config["step_sample_seconds"])
            raise RuntimeError("重启后未连续确认阶段 1；可能有未知二次确认，已暂停并记录截图")
        except Cancelled as exc:
            self.session.update(p6_result="CANCELLED" if attempted else self.session.snapshot()["p6_result"])
            evidence.image("cancelled", self.last_image)
            return evidence.finish(status="CANCELLED", reason=str(exc), clicked=clicked)
        except Exception as exc:
            if attempted:
                self.session.update(p6_result="PAUSED", last_action=f"P6 暂停：{exc}")
            evidence.image("paused", self.last_image)
            return evidence.finish(status="PAUSED", reason=str(exc), clicked=clicked)


class GameLoop:
    def __init__(self, matcher, config, gate, backend=None, session=None, snapshot=capture,
                 notify=lambda event: None, clock=time.monotonic, report_root=None,
                 step_factory=SingleStep, restart_factory=RestartFlow):
        self.matcher, self.config, self.gate = matcher, config, gate
        self.backend, self.session = backend, session if session is not None else Session()
        self.snapshot, self.notify, self.clock = snapshot, notify, clock
        self.report_root = Path(report_root) if report_root else DATA / "reports" / "runs"
        self.step_factory, self.restart_factory = step_factory, restart_factory
        self.last_image = None
        # Set once the board's stage-2 X has been clicked: only then may the
        # pause menu's "重新开始" be pressed.
        self.stage2_exit_pending = False

    def run(self, real=False, max_observations=None):
        evidence = Evidence(self.report_root, "live" if real else "readonly")
        previous, stable, unknown, unknown_since, observations = None, 0, 0, None, 0
        try:
            if real and (self.config["region"] is None or self.backend is None):
                raise RuntimeError("自动操作前必须配置游戏区域和输入后端")
            if real:
                for remaining in range(self.config["step_countdown_seconds"], 0, -1):
                    self.notify({"kind": "flow_status", "text": f"{remaining} 秒后开始自动流程，请将游戏置于前台；F8 中止"})
                    self.gate.wait(1)
            while max_observations is None or observations < max_observations:
                self.gate.check()
                image, origin = self.snapshot(self.config["region"])
                self.last_image = image
                result = self.matcher.detect(image, origin, with_ocr=not real)
                self.gate.check()
                observations += 1
                key = (result["state"], result["stage"])
                stable = stable + 1 if key == previous else 1
                previous = key
                self.notify({"kind": "flow_frame", "image": image, "result": result,
                             "stable": stable, "session": self.session.snapshot(), "real": real})
                if result.get("blocked_reason"):
                    raise RuntimeError(result["blocked_reason"])
                if result["state"] == "WAIT_SCREEN":
                    unknown += 1
                    if unknown_since is None:
                        unknown_since = self.clock()
                    if unknown >= self.config["unknown_max_frames"] or self.clock() - unknown_since >= self.config["unknown_timeout_seconds"]:
                        raise RuntimeError("未知画面连续超过限制，已暂停；没有盲目点击")
                    self.gate.wait(self.config["step_sample_seconds"] if real else self.config["interval_ms"] / 1000)
                    continue
                unknown, unknown_since = 0, None
                if stable < self.config["stable_frames"]:
                    self.gate.wait(self.config["step_sample_seconds"] if real else self.config["interval_ms"] / 1000)
                    continue
                if not real:
                    self.notify({"kind": "flow_status", "text": f"只读：{result['state']} / 阶段 {result['stage']}，不发送输入"})
                    self.gate.wait(self.config["interval_ms"] / 1000)
                    continue
                if result["state"] in ("P1", "P2") and result["stage"] == 1 and not result["ocr"]["error"]:
                    self.session.rearm()
                if result["state"] == "P6":
                    if not self.config["p6_enabled"]:
                        raise RuntimeError("发现暂停菜单，P6 已关闭，停止普通操作")
                    if result["button"]["label"] != "重新开始":
                        raise RuntimeError("暂停菜单里没有可确认的重新开始按钮")
                    if not self.stage2_exit_pending:
                        raise RuntimeError("发现独立暂停菜单；没有本轮退出证据，停止而不直接重启")
                    flow = self.restart_factory(self.matcher, self.config, self.gate, self.backend, self.session,
                                                snapshot=self.snapshot, notify=lambda t: self.notify({"kind": "flow_status", "text": t}),
                                                clock=self.clock, report_root=evidence.folder / "p6")
                    outcome = flow.run(start_at_menu=True)
                    evidence.event("p6_result", result=outcome)
                    if outcome["status"] == "VERIFIED":
                        self.stage2_exit_pending = False
                elif result["state"] in LABELS:
                    # The loop already confirmed this page stable_frames times, so
                    # the step needs one fewer confirmation round before clicking.
                    # The post-click verification still uses the full requirement.
                    step_config = {**self.config, "step_countdown_seconds": 0,
                                   "step_confirm_frames": max(1, self.config["stable_frames"] - 1)}
                    step = self.step_factory(self.matcher, step_config, self.gate, snapshot=self.snapshot,
                                             backend=self.backend, notify=lambda t: self.notify({"kind": "flow_status", "text": t}),
                                             clock=self.clock, report_root=evidence.folder / "steps")
                    outcome = step.run(result["state"], real=True)
                    evidence.event("step_result", result=outcome)
                    if outcome["status"] == "VERIFIED" and outcome.get("fresh", {}).get("stage") == 1:
                        self.session.rearm()
                    if (result["state"] == "P2" and outcome.get("fresh", {}).get("stage") == 2
                            and outcome["status"] == "VERIFIED"):
                        # The board's X was clicked; the pause menu is expected next.
                        self.stage2_exit_pending = True
                    self.session.update(last_action=f"{result['state']}：{outcome['status']} {outcome['reason']}")
                    if outcome.get("stage2_detected"):
                        previous, stable = None, 0
                        continue
                else:
                    raise RuntimeError("发现未知界面；没有明确的页面或阶段证据，停止而不盲点")
                self.notify({"kind": "flow_action", "result": outcome, "session": self.session.snapshot()})
                if outcome["status"] != "VERIFIED":
                    return evidence.finish(status=outcome["status"], reason=outcome["reason"], observations=observations)
                previous, stable = None, 0
                self.gate.wait(self.config["step_sample_seconds"] if real else self.config["interval_ms"] / 1000)
            return evidence.finish(status="OBSERVED", reason="指定离线观测次数已结束", observations=observations)
        except Cancelled as exc:
            evidence.image("cancelled", self.last_image)
            return evidence.finish(status="CANCELLED", reason=str(exc), observations=observations)
        except Exception as exc:
            evidence.image("paused", self.last_image)
            self.notify({"kind": "flow_status", "text": f"PAUSED：{exc}"})
            return evidence.finish(status="PAUSED", reason=str(exc), observations=observations)
