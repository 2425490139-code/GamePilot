"""Window-bound input for the visible game; independently implemented Win32 backend.

Design references (no upstream source copied): OK-WW config.py and ok-script
ok/device/interaction_methods/post_message.py. See OK-WW窗口绑定说明.md.
Only user-started actual mode constructs this backend. Tests inject fake APIs.
"""
import ctypes
from ctypes import wintypes
import logging
import os
import time

from capture import find_game_window
from windows_input import WindowsMouse, user32


def message_api():
    api = user32()
    for name in ("IsWindow", "IsWindowVisible", "IsIconic", "BringWindowToTop"):
        function = getattr(api, name)
        function.argtypes, function.restype = [wintypes.HWND], wintypes.BOOL
    api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    api.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    api.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    api.PostMessageW.restype = wintypes.BOOL
    api.SetCursorPos.argtypes, api.SetCursorPos.restype = [ctypes.c_int, ctypes.c_int], wintypes.BOOL
    api.GetCursorPos.argtypes, api.GetCursorPos.restype = [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL
    api.MapVirtualKeyW.argtypes, api.MapVirtualKeyW.restype = [wintypes.UINT, wintypes.UINT], wintypes.UINT
    return api


class WindowMessageMouse(WindowsMouse):
    def __init__(self, target_feedback=None, api=None, sleeper=time.sleep):
        self.api = api if api is not None else message_api()
        self.target_feedback = target_feedback
        self.sleep = sleeper
        self.game_window = None
        self.game_pid = None

    def describe(self, window):
        title, class_name, pid = ctypes.create_unicode_buffer(512), ctypes.create_unicode_buffer(256), wintypes.DWORD()
        if window:
            self.api.GetWindowTextW(window, title, len(title))
            self.api.GetClassNameW(window, class_name, len(class_name))
            self.api.GetWindowThreadProcessId(window, ctypes.byref(pid))
        return {"hwnd": int(window or 0), "pid": pid.value,
                "title": title.value, "class": class_name.value}

    def validate_window(self, region):
        window, api = self.game_window, self.api
        if not window or not api.IsWindow(window) or not api.IsWindowVisible(window) or api.IsIconic(window):
            raise RuntimeError("已绑定的游戏窗口已关闭、隐藏或最小化；已停止")
        info = self.describe(window)
        if info["pid"] != self.game_pid or info["pid"] == os.getpid() or info["class"] != "UnrealWindow":
            raise RuntimeError(f"游戏窗口身份已变化，拒绝输入：{info}")
        rect, origin = wintypes.RECT(), wintypes.POINT()
        if not api.GetClientRect(window, ctypes.byref(rect)) or not api.ClientToScreen(window, ctypes.byref(origin)):
            raise RuntimeError("无法读取游戏客户区")
        current = [origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top]
        if current != self.client_region:
            raise RuntimeError("游戏窗口位置或尺寸已变化，请重新定位后开始")
        x, y, w, h = region
        if not (origin.x <= x < x + w <= origin.x + current[2]
                and origin.y <= y < y + h <= origin.y + current[3]):
            raise RuntimeError("保存的截图区域已不在游戏内容内；请自动定位游戏")
        return window

    def wait_for_game(self, region, gate, timeout=0, notify=lambda text: None, locator=None, **kwargs):
        """Bind exe + class + PID; foreground equality is not an input prerequisite."""
        gate.check()
        target = (locator or find_game_window)()
        self.game_window, self.game_pid = target["window"], target["pid"]
        self.client_region = list(target["region"])
        self.validate_window(region)
        gate.check()
        # A best-effort visibility request, never a synthetic click or Alt key.
        foreground = self.api.GetForegroundWindow()
        if foreground != self.game_window:
            self.api.BringWindowToTop(self.game_window)
            gate.check()
            activated = bool(self.api.SetForegroundWindow(self.game_window))
        else:
            activated = True
        gate.check()
        logging.info("window_binding game=%s foreground=%s activation_requested=%s input=PostMessage region=%s",
                     self.describe(self.game_window), self.describe(self.api.GetForegroundWindow()), activated, region)
        notify(f"已绑定鸣潮 HWND={self.game_window} / PID={self.game_pid}；窗口消息输入，立即识别当前页面")
        return self.game_window

    def prepare(self, point, region):
        window = self.validate_window(region)
        foreground = self.api.GetForegroundWindow()
        if foreground != window:
            raise RuntimeError("游戏已失去前台，已暂停；切回游戏后再继续")
        x, y, w, h = region
        if not (x <= point[0] < x + w and y <= point[1] < y + h):
            raise RuntimeError("目标超出截图区域")
        hit = self.api.WindowFromPoint(wintypes.POINT(*point))
        root = self.api.GetAncestor(hit, 2) if hit else None
        # Desktop capture still requires a visible target. A window message never
        # gets redirected to whichever app has focus or covers the target.
        if root != window:
            raise RuntimeError(f"游戏目标位置被其他窗口遮挡：{self.describe(root)}；已暂停，未点击")
        return window

    def post(self, message, wparam=0, lparam=0):
        if not self.api.PostMessageW(self.game_window, message, wparam, lparam):
            error = ctypes.get_last_error()
            hint = "；请让脚本与游戏使用相同权限级别" if error == 5 else ""
            raise RuntimeError(f"游戏窗口消息提交失败 0x{message:04X}，Windows 错误 {error}{hint}")

    def click(self, point, region, expected_window):
        if self.prepare(point, region) != expected_window:
            raise RuntimeError("游戏窗口绑定已变化，拒绝点击")
        local = wintypes.POINT(*point)
        if not self.api.ScreenToClient(self.game_window, ctypes.byref(local)):
            raise RuntimeError("无法将按钮坐标转换为游戏客户区坐标")
        if not (0 <= local.x <= 32767 and 0 <= local.y <= 32767):
            raise RuntimeError("客户区坐标超出窗口消息范围")
        packed = (local.y << 16) | local.x
        self.post(0x0006, 1)  # WM_ACTIVATE / WA_ACTIVE, as used by OK-WW.
        if not self.api.SetCursorPos(*point):
            raise RuntimeError("光标移动失败，未提交点击")
        moved = self.cursor()
        if moved and moved != tuple(point):
            # A game that clips or recentres the pointer makes real-mouse input
            # ambiguous; the click is still posted, but the log must say so.
            raise RuntimeError(f"光标位置未到目标 {point}，实际 {moved}，拒绝点击")
        self.prepare(point, region)
        self.post(0x0200, 0, packed)  # WM_MOUSEMOVE
        self.post(0x0201, 1, packed)  # WM_LBUTTONDOWN
        try:
            self.sleep(.01)
        finally:
            self.release(0x0202, 0, packed)
        logging.info("window_input click hwnd=%s screen=%s client=%s cursor=%s foreground=%s",
                     self.game_window, point, (local.x, local.y), moved,
                     self.describe(self.api.GetForegroundWindow()))

    def cursor(self):
        """Best-effort readback so 'the cursor never moved' stops being invisible."""
        try:
            point = wintypes.POINT()
            if not self.api.GetCursorPos(ctypes.byref(point)):
                return None
            position = (point.x, point.y)
            return position if position != (0, 0) else None
        except Exception:
            return None

    def release(self, message, wparam, lparam):
        # One bounded release retry if the message queue rejected the first up.
        try:
            self.post(message, wparam, lparam)
        except RuntimeError:
            self.post(message, wparam, lparam)
            raise

    def escape(self, point, region, expected_window):
        if self.prepare(point, region) != expected_window:
            raise RuntimeError("游戏窗口绑定已变化，拒绝 ESC")
        scan = self.api.MapVirtualKeyW(0x1B, 0)
        if not scan:
            raise RuntimeError("无法解析 ESC 扫描码")
        down = 1 | (scan << 16)
        self.post(0x0006, 1)
        self.post(0x0100, 0x1B, down)
        try:
            self.sleep(.01)
        finally:
            self.release(0x0101, 0x1B, down | (1 << 30) | (1 << 31))
        logging.info("window_input escape hwnd=%s", self.game_window)
