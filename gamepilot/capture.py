"""Capture only. Never brings a game to foreground or sends input."""
import ctypes
import sys
import ntpath
from ctypes import wintypes
from PIL import ImageGrab


def enable_dpi_awareness():
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


def capture(region=None):
    bbox = None
    origin = (0, 0)
    if region is not None:
        x, y, w, h = region
        if w <= 0 or h <= 0:
            raise ValueError("截图区域为空")
        bbox, origin = (x, y, x + w, y + h), (x, y)
    # Primary monitor by default. Region coordinates are physical screen pixels.
    return ImageGrab.grab(bbox=bbox).convert("RGB"), origin


def to_screen(box, image_size, origin):
    """Map normalized reference rectangle to physical desktop rectangle."""
    from config import REFERENCE_SIZE
    sx, sy = image_size[0] / REFERENCE_SIZE[0], image_size[1] / REFERENCE_SIZE[1]
    return tuple(round(v * (sx if i % 2 == 0 else sy) + origin[i % 2])
                 for i, v in enumerate(box))


def window_process_name(window):
    """Read executable metadata only; never opens or reads game memory."""
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    pid = wintypes.DWORD()
    api.GetWindowThreadProcessId(window, ctypes.byref(pid))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return None
    try:
        buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return ntpath.basename(buffer.value)
    finally:
        kernel.CloseHandle(handle)


def find_game_window(title_hint=None, process_name="Client-Win64-Shipping.exe",
                     window_class="UnrealWindow"):
    """Locate one visible non-minimized game client in physical pixels; no input."""
    if sys.platform != "win32":
        raise RuntimeError("自动定位窗口仅支持 Windows")
    if title_hint is not None and not title_hint.strip():
        raise ValueError("游戏窗口标题不能为空")
    enable_dpi_awareness()
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    api.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    api.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    api.IsWindowVisible.argtypes = api.IsIconic.argtypes = [wintypes.HWND]
    matches = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(window, _):
        if not api.IsWindowVisible(window) or api.IsIconic(window):
            return True
        title = ctypes.create_unicode_buffer(api.GetWindowTextLengthW(window) + 1)
        api.GetWindowTextW(window, title, len(title))
        if title_hint is not None and title_hint.casefold() not in title.value.casefold():
            return True
        class_name = ctypes.create_unicode_buffer(256)
        api.GetClassNameW(window, class_name, len(class_name))
        if window_class is not None and class_name.value != window_class:
            return True
        executable = window_process_name(window)
        if process_name is not None and (executable is None or executable.casefold() != process_name.casefold()):
            return True
        rect, point = wintypes.RECT(), wintypes.POINT(0, 0)
        if api.GetClientRect(window, ctypes.byref(rect)) and api.ClientToScreen(window, ctypes.byref(point)):
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width > 0 and height > 0:
                pid = wintypes.DWORD()
                api.GetWindowThreadProcessId(window, ctypes.byref(pid))
                matches.append({"window": int(window), "title": title.value.strip(), "process_name": executable,
                                "class_name": class_name.value, "pid": pid.value,
                                "region": [point.x, point.y, width, height]})
        return True

    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    if not api.EnumWindows(visit, 0):
        raise RuntimeError("读取游戏窗口列表失败")
    if len(matches) != 1:
        raise RuntimeError(f"找到 {len(matches)} 个匹配窗口，请打开并恢复一个游戏窗口；不自动猜测")
    return matches[0]
