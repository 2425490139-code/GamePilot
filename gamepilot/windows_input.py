"""Windows input backend; focus activation is attempted once at actual startup."""
import ctypes
from ctypes import wintypes
import os
import sys
import threading
import time
import math
import logging
from capture import find_game_window


ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", wintypes.DWORD), ("value", INPUTUNION)]


def user32():
    if sys.platform != "win32":
        raise RuntimeError("实际操作仅支持 Windows")
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.GetForegroundWindow.restype = wintypes.HWND
    api.SetForegroundWindow.argtypes, api.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    api.WindowFromPoint.argtypes, api.WindowFromPoint.restype = [wintypes.POINT], wintypes.HWND
    api.GetAncestor.argtypes, api.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    api.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    api.SendInput.restype = wintypes.UINT
    api.GetAsyncKeyState.argtypes, api.GetAsyncKeyState.restype = [ctypes.c_int], ctypes.c_short
    return api


class WindowsMouse:
    def __init__(self, target_feedback=None):
        self.api = user32()
        self.target_feedback = target_feedback

    def preview_target(self, result, gate):
        if getattr(self, "target_feedback", None):
            self.target_feedback(result, gate)

    def prepare_game_start(self, region, gate, notify=lambda text: None, locator=None):
        """Product startup path; tested with fake window APIs, no timed wait."""
        gate.check()
        target = (locator or find_game_window)()
        x, y, w, h = region
        left, top, width, height = target["region"]
        if not (left <= x < x + w <= left + width and top <= y < y + h <= top + height):
            raise RuntimeError("保存的截图区域已不在游戏内容内；停止后点自动定位游戏，再重新开始")
        gate.check()
        window = target["window"]
        foreground = self.api.GetForegroundWindow()
        logging.info("startup_focus game=%s foreground=%s region=%s", window, foreground, region)
        if foreground != window:
            gate.check()
            if not self.api.SetForegroundWindow(window):
                raise RuntimeError("系统未允许切到游戏前台；请点一下游戏画面再继续。尚未显示红框或提交点击")
        gate.check()
        if self.api.GetForegroundWindow() != window:
            raise RuntimeError("切换后游戏仍不在前台；请点一下游戏画面再继续。尚未显示红框或提交点击")
        self.game_window = window
        notify("游戏前台已确认：立即识别当前页面，不要求从 P1 开始；F8 可停止")
        return window

    def wait_for_game(self, region, gate, timeout=0, notify=lambda text: None,
                      clock=time.monotonic, locator=None):
        """Immediate by default; positive timeout retains optional manual focus wait."""
        if timeout == 0:
            return self.prepare_game_start(region, gate, notify=notify, locator=locator)
        locator = locator or find_game_window
        deadline, previous, stable, last_message = clock() + timeout, None, 0, None
        while True:
            gate.check()
            if clock() >= deadline:
                raise RuntimeError("等待游戏前台超时：请最小化 GamePilot 并切回游戏，再点继续；没有发送输入")
            try:
                target = locator()
            except RuntimeError as exc:
                reason = str(exc)
                previous, stable = None, 0
            else:
                x, y, w, h = region
                left, top, width, height = target["region"]
                if not (left <= x < x + w <= left + width and top <= y < y + h <= top + height):
                    raise RuntimeError("保存的截图区域已不在游戏内容内；停止后点自动定位游戏，再重新开始")
                try:
                    point = (x + w // 2, y + h // 2)
                    active = self.prepare(point, region)
                    if active != target["window"]:
                        raise RuntimeError("当前前台不是已定位的鸣潮游戏客户端")
                except RuntimeError as exc:
                    reason = str(exc)
                    previous, stable = None, 0
                else:
                    stable = stable + 1 if active == previous else 1
                    previous = active
                    if stable >= 2:
                        gate.check()
                        if clock() >= deadline:
                            raise RuntimeError("游戏前台确认超时，没有发送输入")
                        self.game_window = active
                        notify("已连续确认游戏在前台；重新识别当前目标，F8 可停止")
                        return active
                    reason = "正在再次确认游戏前台"
            remaining = max(0, math.ceil(deadline - clock()))
            message = f"等待游戏前台（剩余 {remaining} 秒）：请最小化 GamePilot 并切回游戏；F8 可停止。{reason}"
            if message != last_message:
                notify(message)
                last_message = message
            gate.wait(.25)

    def prepare(self, point, region):
        api = self.api
        window = api.GetAncestor(api.WindowFromPoint(wintypes.POINT(*point)), 2)
        if not window or window != api.GetForegroundWindow():
            raise RuntimeError("目标不属于前台游戏窗口，已暂停；运行中不会自动抢回焦点")
        if getattr(self, "game_window", None) is not None and int(window) != self.game_window:
            raise RuntimeError("前台窗口已不是本轮确认的游戏窗口，已停止")
        pid = wintypes.DWORD()
        api.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value == os.getpid():
            raise RuntimeError("目标是本工具自身，拒绝操作")
        bounds = wintypes.RECT()
        if not api.GetWindowRect(window, ctypes.byref(bounds)):
            raise RuntimeError("无法读取目标窗口边界")
        x, y, w, h = region
        if not (bounds.left <= x < x + w <= bounds.right and bounds.top <= y < y + h <= bounds.bottom):
            raise RuntimeError("截图区域超出前台窗口，请重新设置游戏内容区域")
        return int(window)

    def click(self, point, region, expected_window):
        if self.prepare(point, region) != expected_window:
            raise RuntimeError("前台窗口已变化，拒绝点击")
        api = self.api
        left, top = api.GetSystemMetrics(76), api.GetSystemMetrics(77)
        width, height = api.GetSystemMetrics(78), api.GetSystemMetrics(79)
        x, y = point
        if width <= 1 or height <= 1 or not (left <= x < left + width and top <= y < top + height):
            raise RuntimeError("目标超出桌面")
        # Submit move + down + up as one batch, with virtual-desktop absolute coords.
        move = INPUT(type=0, mi=MOUSEINPUT(round((x - left) * 65535 / (width - 1)),
                     round((y - top) * 65535 / (height - 1)), 0, 0x0001 | 0x8000 | 0x4000, 0, 0))
        down = INPUT(type=0, mi=MOUSEINPUT(0, 0, 0, 0x0002, 0, 0))
        up = INPUT(type=0, mi=MOUSEINPUT(0, 0, 0, 0x0004, 0, 0))
        batch = (INPUT * 3)(move, down, up)
        sent = api.SendInput(3, batch, ctypes.sizeof(INPUT))
        if sent != 3:
            if sent:
                # Release a possibly submitted down before returning an error.
                api.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
            raise RuntimeError(f"系统只接受了 {sent}/3 个鼠标事件，已停止（可能是权限不同）")

    def escape(self, point, region, expected_window):
        if self.prepare(point, region) != expected_window:
            raise RuntimeError("发送 ESC 前窗口已变化，已停止")
        down = INPUT(type=1, ki=KEYBDINPUT(0x1B, 0, 0, 0, 0))
        up = INPUT(type=1, ki=KEYBDINPUT(0x1B, 0, 0x0002, 0, 0))
        batch = (INPUT * 2)(down, up)
        sent = self.api.SendInput(2, batch, ctypes.sizeof(INPUT))
        if sent != 2:
            if sent:
                self.api.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
            raise RuntimeError(f"ESC 输入不完整（{sent}/2），已释放并停止")


class EmergencyWatch:
    """Poll F8 while an actual single step is running; no global hook remains."""
    def __init__(self, cancel, notify=lambda: None, reader=None):
        self.cancel, self.notify = cancel, notify
        if reader is None:
            api = user32()
            reader = lambda: bool(api.GetAsyncKeyState(0x77) & 0x8000)
        self.reader = reader
        self.done = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.work, name="gamepilot-F8", daemon=False)

    def work(self):
        try:
            while not self.done.is_set():
                if self.reader():
                    self.cancel()
                    self.notify()
                    self.ready.set()
                    return
                self.ready.set()
                self.done.wait(.03)
        except Exception as exc:
            self.error = exc
            self.cancel()
            self.ready.set()

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(2) or self.error:
            self.done.set()
            self.thread.join()
            raise RuntimeError(f"F8 紧急停止监听启动失败: {self.error}")
        return self

    def __exit__(self, *args):
        self.done.set()
        self.thread.join()
