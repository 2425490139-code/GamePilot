"""One GUI instance per signed-in Windows session; no input or background work."""
import ctypes
from ctypes import wintypes
import sys


class AlreadyRunning(RuntimeError):
    pass


class SingleInstance:
    def __init__(self, name="Local\\GamePilot_Desktop_Control_v1"):
        self.name = name

    def __enter__(self):
        self.handle = None
        if sys.platform == "win32":
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            api.CreateMutexW.restype = wintypes.HANDLE
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            self.api = api
            self.handle = api.CreateMutexW(None, False, self.name)
            if not self.handle:
                raise RuntimeError("无法建立单实例锁，已停止")
            if ctypes.get_last_error() == 183:
                api.CloseHandle(self.handle)
                self.handle = None
                raise AlreadyRunning("已有 GamePilot 窗口正在运行（也可能最小化在任务栏）。\n请先退出旧窗口，再打开本版本；不会同时运行两份自动操作。")
        return self

    def __exit__(self, *args):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
