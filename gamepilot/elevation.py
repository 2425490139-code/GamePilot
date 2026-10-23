"""Privilege self-check: Windows UIPI silently discards input from a lower
integrity process to a higher one. Screen capture keeps working, so the symptom
is "it recognises the screen but the cursor never moves".

This module only reads token information; it never changes privileges.
"""
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

LOW, MEDIUM, HIGH, SYSTEM = 0x1000, 0x2000, 0x3000, 0x4000
LEVEL_NAMES = {LOW: "低", MEDIUM: "普通用户", HIGH: "管理员", SYSTEM: "SYSTEM"}

GAME_PROCESSES = ("Client-Win64-Shipping.exe", "Wuthering Waves.exe")


def level_name(rid):
    return LEVEL_NAMES.get(rid, hex(rid) if rid else "未知")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def process_integrity(pid):
    """Return the integrity RID for a pid, or None when it cannot be read."""
    if sys.platform != "win32" or not pid:
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    advapi.OpenProcessToken.argtypes, advapi.OpenProcessToken.restype = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.GetSidSubAuthorityCount.argtypes, advapi.GetSidSubAuthorityCount.restype = [ctypes.c_void_p], ctypes.POINTER(ctypes.c_ubyte)
    advapi.GetSidSubAuthority.argtypes, advapi.GetSidSubAuthority.restype = [ctypes.c_void_p, wintypes.DWORD], ctypes.POINTER(wintypes.DWORD)
    handle = kernel.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return None
        try:
            buffer, size = ctypes.create_string_buffer(128), wintypes.DWORD(128)
            if not advapi.GetTokenInformation(token, 25, buffer, 128, ctypes.byref(size)):  # TokenIntegrityLevel
                return None
            sid = ctypes.c_void_p.from_buffer(buffer).value
            if not sid:
                return None
            count = advapi.GetSidSubAuthorityCount(sid)[0]
            return advapi.GetSidSubAuthority(sid, count - 1)[0]
        finally:
            kernel.CloseHandle(token)
    finally:
        kernel.CloseHandle(handle)


def game_integrity(locator=None):
    """Return {"pid","executable","integrity"} for the running game, or None."""
    try:
        if locator is None:
            from capture import find_game_window
            locator = find_game_window
        target = locator()
    except Exception:
        return None
    return {"pid": target.get("pid"), "executable": target.get("process_name"),
            "integrity": process_integrity(target.get("pid"))}


def relaunch_as_admin():
    """Start a new elevated copy of this program. True means: exit this one.

    A frozen .exe cannot raise its own token, and a plain python launch inherits
    the shell's token, so both go through ShellExecuteW "runas" (one UAC prompt).
    """
    if sys.platform != "win32" or is_admin():
        return False
    if getattr(sys, "frozen", False):
        arguments = subprocess.list2cmdline(sys.argv[1:])
    else:
        arguments = subprocess.list2cmdline([str(os.path.abspath(sys.argv[0]))] + sys.argv[1:])
    try:
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, arguments, None, 1)
    except Exception:
        return False
    return int(result) > 32


def check(locator=None):
    """Compare this process with the game and explain the consequence."""
    mine = process_integrity(os.getpid())
    game = game_integrity(locator)
    result = {"self_pid": os.getpid(), "self_integrity": mine, "self_admin": is_admin(),
              "game": game, "ok": True, "message": ""}
    if game is None or game["integrity"] is None:
        result["message"] = "未检测到运行中的游戏窗口；实际操作前请先启动游戏。"
        return result
    if game["integrity"] > (mine or 0):
        result["ok"] = False
        result["message"] = (
            f"游戏以{level_name(game['integrity'])}权限运行，GamePilot 是{level_name(mine)}权限："
            "Windows 会静默丢弃光标移动和窗口消息。请右键“以管理员身份运行”启动GamePilot.bat，"
            "或使用配套启动脚本（会自动请求管理员权限）。")
    else:
        result["message"] = (
            f"权限匹配：GamePilot {level_name(mine)} / 游戏 {level_name(game['integrity'])}，可以实际点击。")
    return result
