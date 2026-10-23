"""Preview and opt-in single-step UI. Worker never calls Tk directly."""
import json
import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageTk
from capture import capture, find_game_window
from config import ROOT, DATA, load_config, save_config
from matcher import Matcher, annotate
from actions import ActionGate, SingleStep, Cancelled
from state_machine import GameLoop
from logger import Session
from overlay import TargetOverlay
from elevation import check as check_privilege
from version import VERSION


class App:
    HIDDEN_STATES = ("iconic", "withdrawn")

    def __init__(self, root):
        self.root = root
        root.title(f"GamePilot {VERSION} · 鸣潮乐园助手 · F8 紧急停止")
        root.iconbitmap(str(ROOT / "assets" / "gamepilot.ico"))
        root.iconbitmap(default=str(ROOT / "assets" / "gamepilot.ico"))
        root.geometry("1120x820")
        self.config = load_config()
        self.matcher = Matcher(self.config)
        self.events = queue.Queue()
        self.frames = queue.Queue(maxsize=2)
        self.stop_event = threading.Event()
        self.gate = ActionGate(self.stop_event)
        self.source_path = None
        self.session = Session(DATA / "reports" / "session.json")
        self.run_mode = None
        self.worker_real = False
        self.paused_mode = None
        self.thread = None
        self.pending_start = None
        self.closing = False
        self.auto_minimized = False
        self.controls_hidden = threading.Event()
        self.target_overlay = TargetOverlay(root)
        self.active_target_request = None
        self.last_key, self.stable_count = None, 0
        self.last_failure = None
        from appearance import build
        build(self)
        root.bind("<Map>", self.on_window_restored, add="+")
        root.report_callback_exception = self.report_ui_exception
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda _: self.stop())
        root.after(100, self.poll)
        root.after(400, self.report_privilege)

    def report_ui_exception(self, kind, error, trace):
        logging.error("GUI callback failed", exc_info=(kind, error, trace))
        self.gate.cancel()
        self.pending_start = None
        self.status.set(f"操作失败：{error}；详情已写入 reports/gamepilot.log")
        messagebox.showerror("操作失败", str(error))

    def privilege_gate(self):
        """Actual input requires matching integrity; UIPI drops it silently."""
        report = check_privilege()
        if not report["ok"]:
            logging.warning("privilege mismatch %s", report["message"])
            self.status.set(report["message"])
            messagebox.showerror("权限不匹配，光标无法移动", report["message"])
            return False
        self.status.set(report["message"])
        return True

    def report_privilege(self):
        try:
            report = check_privilege()
        except Exception:
            logging.exception("权限自检失败")
            return
        if report["game"] is not None:
            logging.info("privilege %s", report["message"])
            self.append_log(report["message"])
            if not report["ok"]:
                self.status.set(report["message"])

    def save(self):
        if self.thread and self.thread.is_alive():
            messagebox.showinfo("请先停止", "停止识别后再修改截图区域。")
            return
        try:
            raw = self.region.get().strip().replace("，", ",")
            region = [int(v.strip()) for v in raw.split(",")] if raw else None
            if region is not None and (len(region) != 4 or region[2] <= 0 or region[3] <= 0):
                raise ValueError("请输入四个整数：左,上,宽,高，宽高须大于零")
            save_config({"region": region, "anchor_threshold": float(self.anchor_value.get()),
                         "button_threshold": float(self.button_value.get()), "interval_ms": int(self.interval_value.get()),
                         "p6_enabled": self.p6_enabled.get()})
            self.config = load_config()
            self.matcher = Matcher(self.config)
            self.status.set("配置已保存，实际操作开关不会保存")
            return True
        except (OSError, ValueError) as exc:
            messagebox.showerror("配置错误", str(exc))
            return False

    def locate_game(self):
        if self.thread and self.thread.is_alive():
            messagebox.showinfo("请先停止", "停止当前检测后再定位游戏窗口。")
            return
        try:
            target = find_game_window()
            self.region.set(",".join(map(str, target["region"])))
            if not self.save():
                return
            self.source_path = None
            self.status.set(f"已定位 {target['title']}，区域 {target['region']}。实际操作将绑定此游戏客户区。")
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror("定位失败", str(exc))

    def start_automation(self):
        """An explicitly labelled Execute button is the session opt-in."""
        self.request_start(True)

    def start_preview(self):
        self.request_start(False)

    def request_start(self, real):
        if self.closing:
            return
        logging.info("start_requested real=%s running=%s", real, bool(self.thread and self.thread.is_alive()))
        if self.thread and self.thread.is_alive():
            if self.worker_real == real:
                self.status.set("当前任务已在运行。")
                return
            self.gate.cancel()
            self.pending_start = real
            self.status.set("正在结束预览并启动自动执行……" if real else "正在停止实际操作并切换预览……")
            self.root.after(50, self.finish_mode_switch)
            return
        self.pending_start = None
        self.real_enabled.set(real)
        self.start()

    def finish_mode_switch(self):
        if self.closing or self.pending_start is None:
            return
        if self.thread and self.thread.is_alive():
            self.root.after(50, self.finish_mode_switch)
            return
        real, self.pending_start = self.pending_start, None
        self.real_enabled.set(real)
        self.start()

    def start(self):
        if self.thread and self.thread.is_alive():
            self.status.set("当前任务正在运行；切换仅识别/实际操作模式前，请先停止并等待退出。")
            return
        real = self.real_enabled.get()
        logging.info("start_dispatch real=%s region=%s", real, self.config["region"])
        if real and self.config["region"] is None:
            self.locate_game()
            if self.config["region"] is None:
                self.status.set("未能自动定位游戏；请到识别设置中保存游戏区域后再开始。")
                return
        if real and not self.privilege_gate():
            return
        self.clear_pending()
        self.source_path = None
        self.last_key, self.stable_count = None, 0
        self.gate.reset()
        self.run_mode, self.paused_mode = real, None
        self.worker_real = real
        self.status.set("隐藏控制窗口后识别当前页面；F8 停止并恢复窗口。" if real else "正在启动只读识别……")
        if real:
            self.minimize_for_run()
        self.thread = threading.Thread(target=self.flow_work, args=(real,), name="gamepilot-loop", daemon=False)
        self.thread.start()
        self.refresh_mode()

    def refresh_mode(self):
        running = bool(self.thread and self.thread.is_alive())
        self.real_checkbox.configure(state="disabled" if running else "normal")
        if self.stop_event.is_set():
            mode, hint = "已暂停/停止", "已暂停/停止 · 当前不发送输入"
        elif running:
            mode = "实际操作" if self.worker_real else "仅识别"
            hint = "实际操作：窗口消息输入，请保持游戏画面可见 · F8 停止" if self.worker_real else "仅识别：不点击 · 切换模式前先停止"
        else:
            mode = "实际操作待启动" if self.real_enabled.get() else "仅识别待启动"
            hint = "实际操作已勾选：开始后隐藏本窗口；F8 停止并恢复" if self.real_enabled.get() else "仅识别：不点击 · 勾选实际操作后重新开始"
        self.root.title(f"GamePilot {VERSION} · {mode} · F8 紧急停止")
        self.mode_hint.set(hint)

    def on_window_restored(self, event):
        if event.widget is self.root:
            self.controls_hidden.clear()
        if (event.widget is self.root and self.auto_minimized and self.worker_real
                and self.thread and self.thread.is_alive() and not self.stop_event.is_set()):
            self.pause()
            self.auto_minimized = False
            self.status.set("已恢复控制窗口并暂停任务；点击继续后才恢复执行。")

    def minimize_for_run(self):
        # Minimise (iconify) rather than withdraw: the window stays on the taskbar
        # and the user can bring it back to stop or change settings. It still does
        # not cover the game, and every screenshot re-hides it if it was restored.
        if getattr(self, "target_overlay", None) is not None:
            try:
                self.cancel_target()
            except Exception:
                logging.exception("隐藏红框失败")
            self.target_overlay.hide()
        self.root.iconify()
        self.root.update_idletasks()
        if self.root.state() != "iconic":
            raise RuntimeError("控制窗口未能最小化，未启动实际操作")
        self.controls_hidden.set()
        self.auto_minimized = True

    def capture_for_run(self, region):
        """UI owns the hidden-state latch. Worker never waits on Tk per frame."""
        self.gate.check()
        if not self.controls_hidden.is_set():
            raise Cancelled("控制窗口已恢复或尚未最小化，未继续截图")
        result = capture(region)
        self.gate.check()
        if not self.controls_hidden.is_set():
            raise Cancelled("截图期间控制窗口已恢复，丢弃本帧")
        return result

    def prepare_capture_request(self, request):
        """UI-thread only; a late/cancelled request must not hide restored controls."""
        try:
            if request["cancelled"].is_set() or self.stop_event.is_set() or self.closing:
                request["errors"].append("截图请求已取消")
                return
            if self.root.state() not in self.HIDDEN_STATES:
                logging.info("capture_guard hiding restored GamePilot controls")
                self.gate.cancel()
                raise RuntimeError("控制窗口已恢复，暂停任务；点击继续后恢复")
            if self.root.state() not in self.HIDDEN_STATES:
                raise RuntimeError("控制窗口未成功隐藏，停止截图以免遮挡阶段文字")
        except Exception as exc:
            request["errors"].append(str(exc))
        finally:
            request["done"].set()

    def restore_controls(self):
        self.controls_hidden.clear()
        if self.auto_minimized and not self.closing:
            self.root.deiconify()
            self.auto_minimized = False

    def warm_ocr_async(self):
        """The OCR host costs ~0.7s to boot; start it while the window is bound."""
        def run():
            try:
                from PIL import Image
                from ocr import recognize_text
                recognize_text(Image.new("RGB", (120, 40), (0, 0, 0)), self.config)
                logging.info("OCR host warmed")
            except Exception:
                logging.exception("OCR 预热失败")
        threading.Thread(target=run, name="gamepilot-ocr-warm", daemon=True).start()

    def flash_target(self, result, gate):
        logging.info("target_feedback requested state=%s box=%s", result["state"], result["button"]["screen_box"])
        request = {"box": list(result["button"]["screen_box"]), "done": threading.Event(),
                   "cancelled": threading.Event(), "errors": []}
        self.events.put((None, {"kind": "target_flash", "request": request}))
        deadline = time.monotonic() + 2
        try:
            while not request["done"].wait(.03):
                gate.check()
                if time.monotonic() >= deadline:
                    raise RuntimeError("目标红框显示或清除超时，已停止输入")
            gate.check()
            if request["errors"]:
                raise RuntimeError(f"目标红框异常：{request['errors'][0]}")
        finally:
            request["cancelled"].set()

    def finish_target(self, request, error=None):
        try:
            if self.active_target_request is request:
                self.active_target_request = None
                self.target_overlay.hide()
                logging.info("target_feedback cleared box=%s", request["box"])
        except Exception as exc:
            request["errors"].append(str(exc))
        finally:
            if error is not None:
                request["errors"].append(str(error))
            request["done"].set()

    def cancel_target(self):
        request = self.active_target_request
        if request is not None:
            request["cancelled"].set()
            self.finish_target(request)

    def show_target_request(self, request):
        if self.stop_event.is_set() or self.closing or request["cancelled"].is_set():
            request["done"].set()
            return
        try:
            self.cancel_target()
            self.active_target_request = request
            self.target_overlay.show(request["box"])
            logging.info("target_feedback shown box=%s", request["box"])
            self.root.after(90, lambda: self.finish_target(request))
        except Exception as exc:
            self.finish_target(request, exc)

    def clear_pending(self):
        try:
            while True:
                self.events.get_nowait()
        except queue.Empty:
            pass
        try:
            while True:
                self.frames.get_nowait()
        except queue.Empty:
            pass

    def pause(self):
        self.pending_start = None
        self.paused_mode = self.run_mode
        self.gate.cancel()
        self.cancel_target()
        self.restore_controls()
        self.real_enabled.set(False)
        self.status.set("已暂停；等待当前检测退出后可继续。")

    def resume(self):
        if self.thread and self.thread.is_alive():
            self.status.set("正在结束上一轮检测，请稍后继续。")
            return
        if self.paused_mode is not None:
            self.real_enabled.set(self.paused_mode)
        self.start()

    def stop(self):
        self.pending_start = None
        self.gate.cancel()
        self.cancel_target()
        self.restore_controls()
        self.run_mode, self.paused_mode = None, None
        self.real_enabled.set(False)
        self.last_key, self.stable_count = None, 0
        self.status.set("已暂停/停止，不再提交输入。当前 OCR 返回或超时后退出；恢复识别不自动执行单步。")

    def post_flow(self, event):
        if event["kind"] == "flow_frame":
            try:
                self.frames.put_nowait(event)
            except queue.Full:
                try:
                    self.frames.get_nowait()
                except queue.Empty:
                    pass
                self.frames.put_nowait(event)
        else:
            if event["kind"] == "flow_status":
                logging.info("startup_or_flow_status %s", event["text"])
            self.events.put((None, event))

    def emergency(self):
        self.gate.cancel()
        self.events.put((None, {"kind": "emergency", "text": "F8 已紧急停止；继续前请检查游戏。"}))

    def flow_work(self, real):
        try:
            backend = None
            if real:
                from windows_input import EmergencyWatch
                from windows_message import WindowMessageMouse
                backend = WindowMessageMouse(target_feedback=self.flash_target)
                self.warm_ocr_async()
            engine = GameLoop(self.matcher, self.config, self.gate, backend, self.session,
                              snapshot=self.capture_for_run if real else capture, notify=self.post_flow)
            if real:
                with EmergencyWatch(self.emergency):
                    backend.wait_for_game(self.config["region"], self.gate, self.config["focus_wait_seconds"],
                                          notify=lambda text: self.post_flow({"kind": "flow_status", "text": text}))
                    outcome = engine.run(real=True)
            else:
                outcome = engine.run()
            self.events.put((None, {"kind": "flow_done", "result": outcome}))
        except Cancelled as exc:
            self.events.put((None, {"kind": "flow_done", "result": {"status": "CANCELLED", "reason": str(exc)}}))
        except Exception as exc:
            logging.exception("自动流程启动或窗口绑定异常")
            self.gate.cancel()
            try:
                self.session.update(last_action=f"启动暂停：{exc}")
            except Exception:
                logging.exception("启动暂停记录保存失败")
            self.events.put((None, {"kind": "flow_done", "result": {"status": "PAUSED", "reason": str(exc)}}))

    def work(self, path=None):
        try:
            while not self.stop_event.is_set():
                if path:
                    with Image.open(path) as im:
                        image = im.convert("RGB")
                    origin = (0, 0)
                else:
                    image, origin = capture(self.config["region"])
                result = self.matcher.detect(image, origin)
                if not self.stop_event.is_set():
                    self.events.put((image, result))
                if path or self.stop_event.wait(self.config["interval_ms"] / 1000):
                    break
        except Exception as exc:
            logging.exception("识别工作线程异常，已停止")
            self.stop_event.set()
            self.events.put((None, str(exc)))

    def open_image(self):
        if self.thread and self.thread.is_alive():
            messagebox.showinfo("请先停止", "请先停止并等待当前检测完成，再打开图片。")
            return
        path = filedialog.askopenfilename(filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp")])
        if path:
            self.clear_pending()
            self.run_mode, self.paused_mode = None, None
            self.real_enabled.set(False)
            self.source_path = path
            self.last_key, self.stable_count = None, 0
            self.gate.reset()
            self.thread = threading.Thread(target=self.work, args=(path,), daemon=False)
            self.thread.start()

    def single_step(self):
        if self.thread and self.thread.is_alive():
            messagebox.showinfo("请先停止", "请先停止持续识别并等待当前检测结束，再进行一次单步测试。")
            return
        real = self.real_enabled.get()
        if real and self.source_path:
            messagebox.showinfo("当前是图片模式", "实际点击只允许实时游戏画面。请先开始实时识别，停止后再进行单步测试。")
            return
        if real and self.config["region"] is None:
            messagebox.showinfo("请设置区域", "实际点击前请先填写并保存游戏内容截图区域。")
            return
        if real and not self.privilege_gate():
            return
        self.clear_pending()
        self.gate.reset()
        self.run_mode, self.paused_mode = None, None
        self.worker_real = real
        target, path = self.step_target.get(), self.source_path
        self.status.set(f"准备单步测试 {target}（{'实际点击倒计时' if real else '只读预演'}）")
        self.real_enabled.set(False)
        if real:
            self.minimize_for_run()
        self.thread = threading.Thread(target=self.step_work, args=(target, real, path), daemon=False)
        self.thread.start()
        self.refresh_mode()

    def step_work(self, target, real, path):
        def notify(text):
            self.events.put((None, {"kind": "step_progress", "text": text}))

        def file_capture(region):
            with Image.open(path) as im:
                return im.convert("RGB"), (0, 0)

        try:
            backend = None
            if real:
                from windows_input import EmergencyWatch
                from windows_message import WindowMessageMouse
                backend = WindowMessageMouse(target_feedback=self.flash_target)
                self.warm_ocr_async()
            engine = SingleStep(self.matcher, self.config, self.gate,
                                snapshot=self.capture_for_run if real else (file_capture if path else capture),
                                backend=backend, notify=notify)
            if real:
                with EmergencyWatch(self.emergency):
                    backend.wait_for_game(self.config["region"], self.gate, self.config["focus_wait_seconds"], notify=notify)
                    result = engine.run(target, real=True)
            else:
                result = engine.run(target)
            self.session.update(last_action=f"单步 {target}：{result['status']} {result['reason']}")
            self.events.put((None, {"kind": "step_done", "result": result}))
        except Cancelled as exc:
            self.events.put((None, {"kind": "step_done", "result": {"status": "CANCELLED", "reason": str(exc)}}))
        except Exception as exc:
            logging.exception("单步操作启动失败")
            self.gate.cancel()
            self.events.put((None, {"kind": "step_done", "result": {"status": "PAUSED", "reason": str(exc)}}))

    def poll(self):
        try:
            while True:
                image, result = self.events.get_nowait()
                if image is None:
                    if isinstance(result, dict):
                        if result["kind"] == "capture_ready":
                            self.prepare_capture_request(result["request"])
                        elif result["kind"] == "target_flash":
                            self.show_target_request(result["request"])
                        elif result["kind"] in ("step_done", "flow_done"):
                            outcome = result["result"]
                            self.gate.cancel()
                            self.cancel_target()
                            self.restore_controls()
                            if result["kind"] == "flow_done" and outcome["status"] == "PAUSED":
                                self.paused_mode = self.run_mode
                            self.status.set(f"{outcome['status']}：{outcome['reason']}")
                            self.append_log(json.dumps(outcome, ensure_ascii=False))
                            self.real_enabled.set(False)
                            self.update_activity(self.session.snapshot())
                        elif result["kind"] == "emergency":
                            self.cancel_target()
                            self.restore_controls()
                            self.real_enabled.set(False)
                            self.run_mode, self.paused_mode = None, None
                            self.status.set(result["text"])
                        elif result["kind"] == "flow_action":
                            self.update_activity(result["session"])
                            self.append_log(json.dumps({key: result["result"].get(key) for key in
                                ("status", "reason", "clicked", "report_dir")}, ensure_ascii=False))
                        elif not self.stop_event.is_set():
                            self.status.set(result["text"])
                        continue
                    self.status.set(f"ERROR：{result}")
                    continue
                if self.stop_event.is_set() or self.closing:
                    continue
                key = (result["state"], result["stage"])
                self.stable_count = self.stable_count + 1 if key == self.last_key else 1
                self.last_key = key
                stable = self.stable_count >= self.config["stable_frames"] and result["state"] != "WAIT_SCREEN"
                stage = result["stage"] if result["stage"] is not None else "未知"
                label = result["button"]["label"] if result["button"] else "无可靠目标"
                self.status.set(f"{result['state']} | 阶段：{stage} | 目标：{label} | 连续确认：{self.stable_count} / {self.config['stable_frames']} | {'稳定' if stable else '待确认'} | P6 第2阶段证据：{'有' if result['stage2_evidence'] else '无'}（不执行）")
                preview = annotate(image, result, max_width=self.preview_width(image))
                self.photo = ImageTk.PhotoImage(preview)
                self.canvas.configure(image=self.photo)
                text = json.dumps({"state": result["state"], "stage": result["stage"], "reason": result["reason"], "ocr": result["ocr"]}, ensure_ascii=False)
                self.log.configure(state="normal")
                self.log.insert("end", text + "\n")
                if int(self.log.index("end-1c").split(".")[0]) > 160:
                    self.log.delete("1.0", "40.0")
                self.log.see("end")
                self.log.configure(state="disabled")
                logging.info("preview %s", text)
                failure = result["state"] == "WAIT_SCREEN" or result["ocr"]["error"]
                if failure and self.last_failure != text:
                    from datetime import datetime
                    path = DATA / "reports" / f"unknown_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
                    image.save(path)
                self.last_failure = text if failure else None
        except queue.Empty:
            pass
        latest = None
        try:
            while True:
                latest = self.frames.get_nowait()
        except queue.Empty:
            pass
        if latest and not self.stop_event.is_set() and not self.closing:
            self.render_flow(latest)
        if not self.closing:
            self.refresh_mode()
            # Tighter poll: the worker waits for this thread to confirm "hidden"
            # before every screenshot, so 100ms used to add up per capture.
            self.root.after(20, self.poll)

    def append_log(self, text):
        # Full evidence lives on disk. Hidden Tk Text.see on long wrapped JSON
        # blocked the UI for seconds and starved screenshot/outline callbacks.
        text = str(text)
        if len(text) > 1200:
            text = text[:1200] + "…（完整证据见 reports）"
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 160:
            self.log.delete("1.0", "40.0")
        if self.log.winfo_ismapped():
            self.log.see("end")
        self.log.configure(state="disabled")

    def update_activity(self, session):
        latch = "可触发" if session["p6_armed"] else "已锁定，防重复"
        self.activity.set(f"最近动作：{session['last_action']} | P6：{latch} | 最近触发：{session['p6_triggered_at'] or '无'} | 结果：{session['p6_result'] or '无'} | 重启验证次数：{session['restart_count']}")

    def preview_width(self, image):
        width = max(200, self.canvas.winfo_width() - 16)
        height = max(100, self.canvas.winfo_height() - 16)
        return max(1, min(840, width, int(height * image.width / image.height)))

    def render_flow(self, event):
        result = event["result"]
        mode = "实际操作" if event["real"] else "仅识别"
        self.status.set(f"{mode} | {result['state']} | 阶段：{result['stage'] or '未知'} | 连续确认：{event['stable']}/{self.config['stable_frames']}")
        self.update_activity(event["session"])
        if event["real"] and self.root.state() in self.HIDDEN_STATES:
            # The control window is hidden during actual input: turning a 2560x1600
            # frame into a Tk bitmap here stalls the very thread that must confirm
            # "window hidden" before every screenshot.
            return
        image = annotate(event["image"], result, read_only=not event["real"], max_width=self.preview_width(image))
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.configure(image=self.photo)
        self.append_log(json.dumps({"state": result["state"], "stage": result["stage"], "reason": result["reason"]}, ensure_ascii=False))

    def close(self):
        self.pending_start = None
        self.closing = True
        self.gate.cancel()
        self.cancel_target()
        self.target_overlay.close()
        self.real_enabled.set(False)
        for button in self.buttons:
            button.configure(state="disabled")
        self.status.set("正在等待检测退出后关闭……")
        self.finish_close()

    def finish_close(self):
        if self.thread and self.thread.is_alive():
            self.root.after(100, self.finish_close)
        else:
            self.root.destroy()


def run():
    import ctypes
    import sys
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("GamePilot.Desktop.1.0")
    root = tk.Tk()
    App(root)
    root.mainloop()
