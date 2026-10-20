# -*- coding: utf-8 -*-
"""
key2touch - 用实体键盘模拟触摸屏 (Windows 绿色单文件版)
------------------------------------------------------
核心思路:
  主键盘区(不含小键盘)的物理键位 -> 归一化坐标 -> 屏幕/指定区域上的绝对触点
  Q 在 A 的左上方, 屏幕上的 Q 触点也在 A 触点的左上方, 保留肌肉记忆。

注入方式:
  1) 真·触摸注入  Windows Touch Injection API (InitializeTouchInjection / InjectTouchInput)
  2) 鼠标注入      SendInput, 兼容安卓模拟器等只认鼠标的窗口
  3) 双发          两者同时发送, 兼容性最高

仅依赖 Python 标准库 (ctypes + tkinter), 打包后为单个 exe, 免安装。
"""

import os
import sys
import json
import time
import math
import ctypes
import threading
import queue
import traceback
from ctypes import (wintypes, byref, c_int, c_uint, c_uint32, c_long, c_ulong,
                    c_ulonglong, c_void_p, c_short, c_ushort, POINTER,
                    Structure, Union, sizeof)
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME = "key2touch"
APP_VER = "1.0.2"

# --------------------------------------------------------------------------
# Win32 基础
# --------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
except Exception:
    shcore = None

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
LONG_PTR = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long

# ---- DPI Awareness: 必须最先调用, 否则多屏/DPI 缩放后坐标会偏
def init_dpi():
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return "PerMonitorV2"
    except Exception:
        pass
    try:
        shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return "PerMonitor"
    except Exception:
        return "Unaware"

DPI_MODE = init_dpi()

# ---- 结构体
class POINT(Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

class RECT(Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

class MONITORINFOEX(Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong, ctypes.c_ulong,
                                     POINTER(RECT), ctypes.c_long)

class MOUSEINPUT(Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]

class KEYBDINPUT(Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

class HARDWAREINPUT(Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

class INPUT_UNION(Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

class INPUT(Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

# ---- Touch Injection
PT_TOUCH = 0x00000002
POINTER_FLAG_NEW       = 0x00000800
POINTER_FLAG_INRANGE   = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_FIRSTBUTTON = 0x00100000
POINTER_FLAG_PRIMARY   = 0x00002000
POINTER_FLAG_DOWN      = 0x00010000
POINTER_FLAG_UPDATE    = 0x00020000
POINTER_FLAG_UP        = 0x00040000

TOUCH_MASK_CONTACTAREA = 0x00000001
TOUCH_MASK_ORIENTATION = 0x00000002
TOUCH_MASK_PRESSURE    = 0x00000004

TOUCH_FEEDBACK_DEFAULT = 0x00000001
TOUCH_FEEDBACK_INDIRECT = 0x00000002
TOUCH_FEEDBACK_NONE = 0x00000003


class POINTER_INFO(Structure):
    _fields_ = [
        ("pointerType", wintypes.DWORD),
        ("pointerId", ctypes.c_uint32),
        ("frameId", ctypes.c_uint32),
        ("pointerFlags", ctypes.c_int),
        ("sourceDevice", wintypes.HANDLE),
        ("hwndTarget", wintypes.HWND),
        ("ptPixelLocation", POINT),
        ("ptHimetricLocation", POINT),
        ("ptPixelLocationRaw", POINT),
        ("ptHimetricLocationRaw", POINT),
        ("dwTime", wintypes.DWORD),
        ("historyCount", ctypes.c_uint32),
        ("InputData", ctypes.c_int32),
        ("dwKeyStates", wintypes.DWORD),
        ("PerformanceCount", ctypes.c_uint64),
        ("ButtonChangeType", ctypes.c_int),
    ]


class POINTER_TOUCH_INFO(Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("touchFlags", ctypes.c_int),
        ("touchMask", ctypes.c_int),
        ("rcContact", RECT),
        ("rcContactRaw", RECT),
        ("orientation", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
    ]


user32.InitializeTouchInjection.argtypes = [ctypes.c_uint32, wintypes.DWORD]
user32.InitializeTouchInjection.restype = ctypes.c_bool
user32.InjectTouchInput.argtypes = [ctypes.c_uint32, POINTER(POINTER_TOUCH_INFO)]
user32.InjectTouchInput.restype = ctypes.c_bool
user32.SendInput.argtypes = [c_uint, POINTER(INPUT), c_int]
user32.SendInput.restype = c_uint

# ---- 键盘低层钩子
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
HC_ACTION = 0


class KBDLLHOOKSTRUCT(Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]

HOOKPROC = ctypes.WINFUNCTYPE(LONG_PTR, c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = [c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LONG_PTR
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.GetMessageW.argtypes = [POINTER(wintypes.MSG), wintypes.HWND, c_uint, c_uint]
user32.GetMessageW.restype = ctypes.c_bool
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.GetAsyncKeyState.argtypes = [c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, POINTER(RECT)]
user32.GetCursorPos.argtypes = [POINTER(POINT)]
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, c_int]
user32.GetWindowLongPtrW.restype = LONG_PTR
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, c_int, LONG_PTR]
user32.SetWindowLongPtrW.restype = LONG_PTR
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, c_int, c_int,
                                c_int, c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000


# --------------------------------------------------------------------------
# 键盘物理拓扑布局
# --------------------------------------------------------------------------
# 值: (x_units, row)  x_units 以标准键宽为单位(1.0), 体现真实错位; row: 0=数字行 ... 4=空格行
X_MIN, X_MAX, ROW_MAX = 0.0, 14.0, 4

BASE_LAYOUT = {
    0xC0: (0.0, 0),            # `
    0x31: (1.0, 0), 0x32: (2.0, 0), 0x33: (3.0, 0), 0x34: (4.0, 0), 0x35: (5.0, 0),
    0x36: (6.0, 0), 0x37: (7.0, 0), 0x38: (8.0, 0), 0x39: (9.0, 0), 0x30: (10.0, 0),
    0xBD: (11.0, 0), 0xBB: (12.0, 0),                    # - =
    0x08: (13.5, 0),                                      # Backspace

    0x51: (1.5, 1), 0x57: (2.5, 1), 0x45: (3.5, 1), 0x52: (4.5, 1), 0x54: (5.5, 1),
    0x59: (6.5, 1), 0x55: (7.5, 1), 0x49: (8.5, 1), 0x4F: (9.5, 1), 0x50: (10.5, 1),
    0xDB: (11.5, 1), 0xDD: (12.5, 1), 0xDC: (13.5, 1),    # Q..P [ ] \

    0x41: (1.75, 2), 0x53: (2.75, 2), 0x44: (3.75, 2), 0x46: (4.75, 2), 0x47: (5.75, 2),
    0x48: (6.75, 2), 0x4A: (7.75, 2), 0x4B: (8.75, 2), 0x4C: (9.75, 2),
    0xBA: (10.75, 2), 0xDE: (11.75, 2),                   # A..L ; '

    0x5A: (2.25, 3), 0x58: (3.25, 3), 0x43: (4.25, 3), 0x56: (5.25, 3), 0x42: (6.25, 3),
    0x4E: (7.25, 3), 0x4D: (8.25, 3), 0xBC: (9.25, 3), 0xBE: (10.25, 3), 0xBF: (11.25, 3),

    0x20: (7.5, 4),                                       # Space
}

# 扩展键: 默认关闭, 打开后边缘键也可用(会占用 Tab/Caps/Shift/Enter/RAlt 等)
EXT_LAYOUT = {
    0x09: (0.75, 1),            # Tab
    0x14: (0.875, 2),           # CapsLock
    0xA0: (1.125, 3),           # LShift
    0x0D: (13.875, 2),          # Enter
    0xA1: (13.375, 3),          # RShift
}

KEY_NAMES = {
    0xC0: "`", 0xBD: "-", 0xBB: "=", 0x08: "Bksp",
    0xDB: "[", 0xDD: "]", 0xDC: "\\", 0xBA: ";", 0xDE: "'",
    0xBC: ",", 0xBE: ".", 0xBF: "/", 0x20: "Space",
    0x09: "Tab", 0x14: "Caps", 0xA0: "LSh", 0x0D: "Enter", 0xA1: "RSh",
    0x91: "Scroll", 0xA4: "LAlt", 0xA5: "RAlt", 0xA2: "LCtrl", 0xA3: "RCtrl",
    0x5B: "LWin", 0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓",
    0x70: "F1", 0x71: "F2", 0x72: "F3", 0x73: "F4", 0x74: "F5", 0x75: "F6",
    0x76: "F7", 0x77: "F8", 0x78: "F9", 0x79: "F10", 0x7A: "F11", 0x7B: "F12",
    0xA0: "LShift", 0xA1: "RShift", 0x10: "Shift",
}

HOTKEY_CHOICES = [
    (0x91, "Scroll Lock"), (0x14, "Caps Lock"), (0x7A, "F11"), (0x7B, "F12"),
    (0x78, "F9"), (0x79, "F10"), (0xC0, "` (反引号)"), (0x2D, "Insert"),
    (0x03, "Ctrl+Break(需组合)"),
]
HOLD_CHOICES = [
    (0, "不使用"), (0xA5, "右 Alt"), (0xA4, "左 Alt"), (0xA3, "右 Ctrl"),
    (0x14, "Caps Lock"), (0x10, "Shift"), (0x5B, "左 Win"),
]


def vk_name(vk):
    if vk in KEY_NAMES:
        return KEY_NAMES[vk]
    n = user32.MapVirtualKeyW(vk, 2)
    if n:
        try:
            return chr(n)
        except Exception:
            pass
    return "VK%02X" % vk


user32.MapVirtualKeyW.argtypes = [c_uint, c_uint]
user32.MapVirtualKeyW.restype = c_uint


# --------------------------------------------------------------------------
# 显示器枚举
# --------------------------------------------------------------------------
def enum_monitors():
    monitors = []

    def _cb(hmon, hdc, lprc, data):
        mi = MONITORINFOEX()
        mi.cbSize = sizeof(MONITORINFOEX)
        user32.GetMonitorInfoW(hmon, byref(mi))
        r = mi.rcMonitor
        monitors.append({
            "x": r.left, "y": r.top,
            "w": r.right - r.left, "h": r.bottom - r.top,
            "name": mi.szDevice,
            "primary": bool(mi.dwFlags & 1),
        })
        return True

    user32.GetMonitorInfoW.argtypes = [ctypes.c_ulong, POINTER(MONITORINFOEX)]
    user32.EnumDisplayMonitors(user32.GetDC(None), None, MONITORENUMPROC(_cb), 0)
    monitors.sort(key=lambda m: (m["x"], m["y"]))
    return monitors


def monitor_at_cursor(monitors):
    p = POINT()
    user32.GetCursorPos(byref(p))
    for i, m in enumerate(monitors):
        if m["x"] <= p.x < m["x"] + m["w"] and m["y"] <= p.y < m["y"] + m["h"]:
            return i
    return 0


# --------------------------------------------------------------------------
# 注入器
# --------------------------------------------------------------------------
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
INPUT_MOUSE = 0


class Injector:
    """真触摸注入 + 鼠标注入"""

    def __init__(self, max_points=10, feedback=TOUCH_FEEDBACK_NONE):
        self.max_points = max_points
        self.touch_ready = False
        self.last_error = ""
        # 系统是否真的报告了触摸屏: 没有 digitizer 时注入可能被静默丢弃,
        # 只剩系统画的"触摸反馈圆点", 造成"看着点了其实没点中"的假象
        self.digitizer = user32.GetSystemMetrics(94)          # SM_DIGITIZER
        self.touch_capable = bool(self.digitizer & 0x03)      # 集成或外接触摸屏
        self.vx = user32.GetSystemMetrics(76)     # SM_XVIRTUALSCREEN
        self.vy = user32.GetSystemMetrics(77)     # SM_YVIRTUALSCREEN
        self.vw = user32.GetSystemMetrics(78)     # SM_CXVIRTUALSCREEN
        self.vh = user32.GetSystemMetrics(79)     # SM_CYVIRTUALSCREEN
        self.init_touch(feedback)

    def init_touch(self, feedback=TOUCH_FEEDBACK_NONE):
        try:
            ok = user32.InitializeTouchInjection(self.max_points, feedback)
            self.touch_ready = bool(ok)
            if not ok:
                self.last_error = "InitializeTouchInjection 失败 err=%s" % ctypes.get_last_error()
        except Exception as e:
            self.touch_ready = False
            self.last_error = str(e)
        return self.touch_ready

    # ---- 触摸
    # 实测要点(踩过的坑):
    #   1) 不要填 rcContact / rcContactRaw : 填了会返回 ERROR_INVALID_PARAMETER
    #   2) 不要填 ptPixelLocationRaw        : 同上
    #   3) 多指必须"一帧内一次性提交全部触点", 单独逐个 Inject 第二指会失败
    def _mk(self, pid, x, y, flags):
        info = POINTER_TOUCH_INFO()
        info.pointerInfo.pointerType = PT_TOUCH
        info.pointerInfo.pointerId = pid
        info.pointerInfo.pointerFlags = flags
        info.pointerInfo.ptPixelLocation.x = int(x)
        info.pointerInfo.ptPixelLocation.y = int(y)
        info.touchMask = TOUCH_MASK_CONTACTAREA | TOUCH_MASK_ORIENTATION | TOUCH_MASK_PRESSURE
        info.touchFlags = 0
        info.orientation = 90
        info.pressure = 1024
        return info

    @staticmethod
    def _flags(kind):
        if kind == "down":
            return POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
        if kind == "move":
            return POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
        return POINTER_FLAG_UP

    def touch_frame(self, points, changed_pid, x, y, kind):
        """points: [(pid,x,y)] 当前全部有效触点(含本次变化的那个)"""
        if not self.touch_ready:
            return False
        items = []
        for pid, px, py in points:
            if pid == changed_pid:
                items.append(self._mk(pid, x, y, self._flags(kind)))
            else:
                items.append(self._mk(pid, px, py, self._flags("move")))
        if not items:
            return False
        arr = (POINTER_TOUCH_INFO * len(items))(*items)
        return bool(user32.InjectTouchInput(len(items), arr))

    # ---- 鼠标
    # 分成"移动"和"按键"两次 SendInput, 比在一条 INPUT 里同时塞 MOVE+LEFTDOWN 可靠
    def _send_mouse(self, dx, dy, flags):
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.u.mi.dx = dx
        inp.u.mi.dy = dy
        inp.u.mi.mouseData = 0
        inp.u.mi.dwFlags = flags
        inp.u.mi.time = 0
        inp.u.mi.dwExtraInfo = 0
        return bool(user32.SendInput(1, byref(inp), sizeof(INPUT)))

    def mouse_move(self, x, y):
        nx = int((x - self.vx) * 65535.0 / max(1, self.vw - 1))
        ny = int((y - self.vy) * 65535.0 / max(1, self.vh - 1))
        nx = max(0, min(65535, nx))
        ny = max(0, min(65535, ny))
        return self._send_mouse(nx, ny,
                                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)

    def mouse_btn(self, kind):
        flags = MOUSEEVENTF_LEFTDOWN if kind == "down" else MOUSEEVENTF_LEFTUP
        return self._send_mouse(0, 0, flags)

    def mouse(self, x, y, kind):
        if kind == "down":
            ok = self.mouse_move(x, y)
            # 先让光标停稳, 目标窗口收到 WM_MOUSEMOVE 进入 hover 之后再按下。
            # 间隔太短(<=10ms)时, 部分程序会来不及切换悬停目标, 表现为"光标到了但点不中"。
            time.sleep(0.025)
            return self.mouse_btn("down") and ok
        if kind == "up":
            return self.mouse_btn("up")
        return self.mouse_move(x, y)

    def emit(self, pid, x, y, kind, mode, others=None):
        """mode: 'touch' | 'mouse' | 'both';  others: 其它在按触点 [(pid,x,y)]"""
        ok = None
        if mode in ("touch", "both"):
            pts = list(others or []) + [(pid, x, y)]
            ok = self.touch_frame(pts, pid, x, y, kind)
        if mode in ("mouse", "both"):
            m = self.mouse(x, y, kind)
            ok = m if ok is None else (ok or m)
        return ok

    def keepalive(self, points):
        """长按心跳: 防止系统把长时间不动的触点当作失效释放"""
        if self.touch_ready and points:
            self.touch_frame(list(points), -1, 0, 0, "move")


user32.GetSystemMetrics.argtypes = [c_int]
user32.GetSystemMetrics.restype = c_int
user32.GetDC.argtypes = [wintypes.HWND]
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, POINTER(RECT), MONITORENUMPROC, ctypes.c_long]


# --------------------------------------------------------------------------
# 应用主体
# --------------------------------------------------------------------------
DEFAULT_CFG = {
    "mode": "mouse",              # touch / mouse / both (首次运行会按系统触摸能力自动选)
    "touch_feedback": False,      # 系统触摸视觉反馈: 会画圆点, 容易造成"点中了"的假象, 默认关
    "margin_pct": 3.0,            # 映射区相对屏幕边缘的内缩百分比
    "monitor_index": 0,
    "follow_cursor_monitor": False,
    "region": None,               # None=整屏, 或 [x,y,w,h] 绝对虚拟坐标
    "use_ext_keys": False,
    "toggle_vk": 0x91,            # Scroll Lock
    "hold_vk": 0xA5,              # 右 Alt
    "overlay": False,             # 默认不开: 覆盖层是辅助工具, 需要时按 F12 或点按钮打开
    "overlay_alpha": 0.55,
    "overlay_labels": True,
    "overlay_vk": 0x7B,           # F12
    "prev_mon_vk": 0x78,          # F9
    "next_mon_vk": 0x79,          # F10
    "nudge_enabled": True,
    "nudge_step": 6,
    "nudge_fast": 5,
    "repeat_enabled": False,
    "repeat_hz": 12.0,
    "repeat_delay_ms": 400,
    "min_tap_ms": 80,             # 按压时长: 太短(20~30ms)时很多游戏/模拟器认不出这次点击
    "cfg_version": 2,
    "auto_minimize": True,        # 启用后自动最小化, 避免本窗口挡住落点
    "enabled": False,
}


def app_dir():
    if getattr(sys, "frozen", False):
        d = os.path.dirname(sys.executable)
    else:
        d = os.path.dirname(os.path.abspath(__file__))
    return d


# ---------------------------------------------------------------- 前台目标诊断
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD


def fg_target_info(my_hwnd=0):
    """取前台窗口信息 -> dict(hwnd, title, exe, elevated, self)
    elevated 判定: 用 PROCESS_QUERY_LIMITED_INFORMATION 打不开对方进程,
    说明对方完整性级别更高(管理员运行), 此时低权限进程注入的点击会被 UIPI 拦掉,
    但光标照样会移动 —— 这正是"鼠标挪过去了却没点中"的典型现象。"""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(n, 8) + 2)
        user32.GetWindowTextW(hwnd, buf, len(buf))
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, byref(pid))
        exe, elevated = "", False
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            elevated = True          # 打不开 = 权限比本进程高
        else:
            try:
                sz = wintypes.DWORD(1024)
                pb = ctypes.create_unicode_buffer(1024)
                if kernel32.QueryFullProcessImageNameW(h, 0, pb, byref(sz)):
                    exe = os.path.basename(pb.value)
            finally:
                kernel32.CloseHandle(h)
        return {"hwnd": int(hwnd), "title": buf.value, "exe": exe,
                "elevated": elevated, "pid": pid.value,
                "self": bool(my_hwnd and int(hwnd) == int(my_hwnd))}
    except Exception:
        return None


GA_ROOT = 2
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wintypes.HWND


def window_at_info(x, y):
    """这个屏幕坐标下面到底是哪个窗口 —— 直接回答"我的点击会打到谁"。
    如果答案是本程序自己或桌面, 就说明落点根本不在游戏上。"""
    try:
        hwnd = user32.WindowFromPoint(POINT(int(x), int(y)))
        if not hwnd:
            return ""
        root = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        n = user32.GetWindowTextLengthW(root)
        buf = ctypes.create_unicode_buffer(max(n, 8) + 2)
        user32.GetWindowTextW(root, buf, len(buf))
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(root, byref(pid))
        exe = ""
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if h:
            try:
                sz = wintypes.DWORD(1024)
                pb = ctypes.create_unicode_buffer(1024)
                if kernel32.QueryFullProcessImageNameW(h, 0, pb, byref(sz)):
                    exe = os.path.basename(pb.value)
            finally:
                kernel32.CloseHandle(h)
        title = buf.value or ""
        if len(title) > 22:
            title = title[:22] + "…"
        return "%s%s" % (exe or "?", (" · " + title) if title else "")
    except Exception:
        return ""


DEBUG_LOG = os.path.join(app_dir(), "key2touch_debug.log")


def dbg(msg):
    """诊断日志: 始终写到程序同目录, 用于定位"按了没反应"到底卡在哪一步"""
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def cfg_path():
    d = app_dir()
    p = os.path.join(d, "key2touch.config.json")
    # 注意: 这里不能顺手 open(p,'a') 建文件, 否则"首次运行"判断会失效
    if os.path.exists(p):
        if os.access(p, os.W_OK):
            return p
    elif os.access(d, os.W_OK):
        return p
    base = os.environ.get("APPDATA", os.path.expanduser("~"))
    d2 = os.path.join(base, "key2touch")
    try:
        os.makedirs(d2, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d2, "key2touch.config.json")


class Key2TouchApp:
    def __init__(self):
        # pyinstaller onefile 下, tkinter 的 tcl/tk 数据目录
        if getattr(sys, "frozen", False):
            base = getattr(sys, "_MEIPASS", app_dir())
            os.environ.setdefault("TCL_LIBRARY", os.path.join(base, "tcl"))
            os.environ.setdefault("TK_LIBRARY", os.path.join(base, "tk"))

        self.cfg = dict(DEFAULT_CFG)
        self.cfg_path = cfg_path()
        try:
            self.first_run = (not os.path.exists(self.cfg_path)
                              or os.path.getsize(self.cfg_path) < 10)
        except Exception:
            self.first_run = True
        self.load_cfg()
        self.monitors = enum_monitors()
        if not self.monitors:
            self.monitors = [{"x": 0, "y": 0, "w": user32.GetSystemMetrics(0),
                              "h": user32.GetSystemMetrics(1), "name": "\\\\.\\DISPLAY1",
                              "primary": True}]
        self.cfg["monitor_index"] = min(self.cfg.get("monitor_index", 0), len(self.monitors) - 1)
        if not os.path.exists(self.cfg_path):   # 首次运行默认选主屏, 而不是排序后的第一个
            for i, m in enumerate(self.monitors):
                if m["primary"]:
                    self.cfg["monitor_index"] = i
                    break

        fb = TOUCH_FEEDBACK_DEFAULT if self.cfg.get("touch_feedback") else TOUCH_FEEDBACK_NONE
        self.inj = Injector(max_points=10, feedback=fb)
        if self.first_run:
            # 首次运行: 系统没有触摸屏支持就直接给最可靠的鼠标模式
            self.cfg["mode"] = "touch" if self.inj.touch_capable else "mouse"
        self.events = queue.Queue()
        self.active = {}            # vk -> dict(pid, x, y, t_down)
        self.pid_pool = set()
        self.hold_down = False
        self.stop_flag = False
        self.log_lines = []
        self.hook_thread = None
        self.hook_id = None
        self.hook_tid = 0
        self.hook_cb = None
        self.last_touch_vk = None
        self.diag = None
        self.diag_test = None
        dbg("=" * 60)
        dbg("key2touch %s 启动 | frozen=%s | dir=%s" % (APP_VER, getattr(sys, "frozen", False), app_dir()))

        dbg("步骤1 配置与显示器 OK, 显示器数=%d" % len(self.monitors))
        self.build_ui()
        dbg("步骤2 界面构建 OK")
        self.start_hook()
        dbg("步骤3 钩子线程已启动")
        self.start_worker()
        self.root.after(80, self.pump_ui)
        if self.cfg["overlay"]:
            self.root.after(300, lambda: self.set_overlay(True))

    # ---------------- 配置 ----------------
    def load_cfg(self):
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.cfg.update({k: v for k, v in data.items() if k in DEFAULT_CFG})
                # v2: 早期版本按压时长只有 35ms, 很多游戏/模拟器认不出这种"过快"的点击,
                # 老配置升级时统一抬到 80ms
                if int(data.get("cfg_version", 1)) < 2:
                    self.cfg["min_tap_ms"] = max(80, int(self.cfg.get("min_tap_ms", 80)))
                    self.cfg["cfg_version"] = 2
        except Exception:
            pass

    def save_cfg(self):
        try:
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
            self.log("配置已保存: %s" % self.cfg_path)
        except Exception as e:
            self.log("保存失败: %s" % e)

    def log(self, msg):
        line = time.strftime("%H:%M:%S") + "  " + msg
        self.log_lines.append(line)
        if len(self.log_lines) > 300:
            self.log_lines = self.log_lines[-300:]
        try:
            self.logbox.configure(state="normal")
            self.logbox.insert("end", line + "\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        except Exception:
            pass

    # ---------------- 映射 ----------------
    def layout(self):
        lay = dict(BASE_LAYOUT)
        if self.cfg["use_ext_keys"]:
            lay.update(EXT_LAYOUT)
        return lay

    def region(self):
        idx = self.cfg["monitor_index"]
        if idx >= len(self.monitors):      # 显示器被拔掉等情况的兜底
            idx = self.cfg["monitor_index"] = 0
        m = self.monitors[idx]
        r = self.cfg["region"]
        if r and len(r) == 4:
            return [int(r[0]), int(r[1]), int(r[2]), int(r[3])]
        return [m["x"], m["y"], m["w"], m["h"]]

    def map_key(self, vk):
        lay = self.layout()
        if vk not in lay:
            return None
        xu, row = lay[vk]
        rx, ry, rw, rh = self.region()
        mg = self.cfg["margin_pct"] / 100.0
        nx = (xu - X_MIN) / (X_MAX - X_MIN)
        ny = row / float(ROW_MAX)
        px = rx + rw * mg + nx * (rw - 2 * rw * mg)
        py = ry + rh * mg + ny * (rh - 2 * rh * mg)
        return int(round(px)), int(round(py))

    def all_points(self):
        out = []
        for vk in self.layout():
            p = self.map_key(vk)
            if p:
                out.append((vk, p[0], p[1]))
        return out

    # ---------------- 键盘钩子 ----------------
    def start_hook(self):
        self.hook_cb = HOOKPROC(self._hook_proc)

        def run():
            dbg("  钩子线程已运行 tid=%s" % kernel32.GetCurrentThreadId())
            self.hook_tid = kernel32.GetCurrentThreadId()
            # WH_KEYBOARD_LL 是低级钩子, hMod 必须为 NULL:
            # 打包成单文件 exe 后 GetModuleHandleW(None) 会返回无效句柄导致 err=126
            self.hook_id = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self.hook_cb, None, 0)
            if not self.hook_id:
                e1 = ctypes.get_last_error()
                self.hook_id = user32.SetWindowsHookExW(
                    WH_KEYBOARD_LL, self.hook_cb, kernel32.GetModuleHandleW(None), 0)
                if not self.hook_id:
                    dbg("!! 键盘钩子安装失败 err(NULL)=%s err(module)=%s" %
                        (e1, ctypes.get_last_error()))
                    self.events.put(("hookfail", ctypes.get_last_error()))
                    return
            dbg("键盘钩子已安装 hook_id=%s" % self.hook_id)
            self.events.put(("log", "键盘钩子已安装"))
            msg = wintypes.MSG()
            dbg("  进入消息循环, 等待按键…")
            while not self.stop_flag:
                r = user32.GetMessageW(byref(msg), None, 0, 0)
                if r == 0 or r == -1:
                    break
                user32.TranslateMessage(byref(msg))
                user32.DispatchMessageW(byref(msg))
            try:
                if self.hook_id:
                    user32.UnhookWindowsHookEx(self.hook_id)
            except Exception:
                pass

        self.hook_thread = threading.Thread(target=run, daemon=True)
        self.hook_thread.start()

    def _hook_proc(self, nCode, wParam, lParam):
        """钩子回调: 只做极轻量处理, 避免触发系统低级钩子超时"""
        try:
            if nCode == HC_ACTION:
                kb = ctypes.cast(lParam, POINTER(KBDLLHOOKSTRUCT)).contents
                vk = kb.vkCode
                down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                if self.pre_handle(vk, down):
                    return 1
        except Exception:
            return 0
        return user32.CallNextHookEx(self.hook_id, nCode, wParam, lParam)

    def sys_modifier_down(self):
        # Ctrl / Win / (未被占用的)Alt 按下时一律放行, 保证 Alt+Tab、Ctrl+C 等系统组合键永远可用
        # 注意: 只查物理键码, 且排除用户指定的"按住生效键", 否则按住模式会被误判为系统组合键
        hv = self.cfg["hold_vk"]
        keys = [0xA2, 0xA3, 0x5B, 0x5C]          # Ctrl / Win
        for a in (0xA4, 0xA5):                    # Alt
            if a != hv:
                keys.append(a)
        for k in keys:
            if user32.GetAsyncKeyState(k) & 0x8000:
                return True
        return False

    def pre_handle(self, vk, down):
        """返回 True = 吞掉该按键"""
        cfg = self.cfg

        # 总开关
        if down and vk == cfg["toggle_vk"]:
            dbg("热键 总开关 -> 切换")
            self.events.put(("toggle", None))
            return True
        if down and vk == cfg["overlay_vk"]:
            self.events.put(("overlay", None))
            return True
        if down and vk == cfg["prev_mon_vk"]:
            self.events.put(("mon", -1))
            return True
        if down and vk == cfg["next_mon_vk"]:
            self.events.put(("mon", 1))
            return True

        # 按住生效修饰键
        hold_vk = cfg["hold_vk"]
        if hold_vk and vk == hold_vk:
            dbg("按住键 %s %s" % (vk_name(vk), "按下" if down else "松开"))
            self.events.put(("hold", down))
            return True

        if self.sys_modifier_down():
            dbg("放行 %s (系统修饰键按下中)" % vk_name(vk))
            return False

        if not self.active_now():
            dbg("放行 %s (未启用: 点面板上的启用按钮或按 %s)" %
                (vk_name(vk), vk_name(cfg["toggle_vk"])))
            return False

        # 方向键微调(有触点按下时)
        if cfg["nudge_enabled"] and vk in (0x25, 0x26, 0x27, 0x28) and self.active:
            self.events.put(("nudge", (vk, down)))
            return True

        if vk in self.layout():
            dbg("捕获 %s %s" % (vk_name(vk), "按下" if down else "松开"))
            self.events.put(("key", (vk, down)))
            return True

        dbg("放行 %s (不在映射表内)" % vk_name(vk))
        return False

    def active_now(self):
        if self.cfg["enabled"]:
            return True
        hv = self.cfg["hold_vk"]
        if hv and (user32.GetAsyncKeyState(hv) & 0x8000):
            return True
        return self.hold_down

    # ---------------- 触点处理(worker 线程) ----------------
    def start_worker(self):
        def run():
            last_ka = 0.0
            while not self.stop_flag:
                try:
                    ev, data = self.events.get(timeout=0.05)
                except Exception:
                    ev, data = None, None
                if ev:
                    try:
                        self.handle_event(ev, data)
                    except Exception:
                        self.events.put(("log", traceback.format_exc(limit=2)))
                # 连点
                if self.cfg["repeat_enabled"] and self.active:
                    self.do_repeat()
                # 长按心跳
                now = time.time()
                if now - last_ka > 0.8 and self.active:
                    last_ka = now
                    if self.cfg["mode"] in ("touch", "both"):
                        self.inj.keepalive([(a["pid"], a["x"], a["y"])
                                            for a in self.active.values()])
        t = threading.Thread(target=run, daemon=True)
        t.start()

    def handle_event(self, ev, data):
        if ev == "log":
            self.events_ui(("log", data))
        elif ev == "hookfail":
            self.events_ui(("log", "钩子安装失败 err=%s (尝试以管理员运行)" % data))
        elif ev == "toggle":
            self.cfg["enabled"] = not self.cfg["enabled"]
            self.events_ui(("state", None))
            if not self.cfg["enabled"]:
                self.release_all()
            elif self.cfg.get("auto_minimize", True):
                # 热键启用时同样要让路, 否则点击全落在自己窗口上
                self.events_ui(("minimize", None))
        elif ev == "hold":
            self.hold_down = data
            if not data:
                self.release_all()
            self.events_ui(("state", None))
        elif ev == "overlay":
            self.events_ui(("overlay", None))
        elif ev == "mon":
            n = len(self.monitors)
            self.cfg["monitor_index"] = (self.cfg["monitor_index"] + data) % n
            self.release_all()
            self.events_ui(("state", None))
        elif ev == "key":
            vk, down = data
            if down:
                self.touch_down(vk)
            else:
                self.touch_up(vk)
        elif ev == "nudge":
            vk, down = data
            if down:
                self.do_nudge(vk)

    def events_ui(self, pair):
        self.ui_queue.put(pair)

    def alloc_pid(self):
        for i in range(10):
            if i not in self.pid_pool:
                self.pid_pool.add(i)
                return i
        return None

    def others(self, exclude_vk):
        return [(a["pid"], a["x"], a["y"]) for k, a in self.active.items() if k != exclude_vk]

    def note_inject(self, ok):
        """注入失败时提示(限流), 最常见的就是 UIPI 权限不足"""
        if ok is False:
            now = time.time()
            if now - getattr(self, "_last_fail", 0) > 3:
                self._last_fail = now
                self.events_ui(("log", "注入被拒绝: 目标窗口权限高于本程序, 请以管理员身份运行 key2touch"))

    def touch_down(self, vk):
        if vk in self.active:
            return
        p = self.map_key(vk)
        if not p:
            return
        pid = self.alloc_pid()
        if pid is None:
            return
        self.active[vk] = {"pid": pid, "x": p[0], "y": p[1],
                           "t": time.time(), "next": 0}
        self.last_touch_vk = vk
        ok = self.inj.emit(pid, p[0], p[1], "down", self.cfg["mode"], others=self.others(vk))
        self.note_inject(ok)
        # 这个坐标下面到底是哪个窗口: 直接回答"我的点击会打到谁"
        target = window_at_info(p[0], p[1])
        dbg("  触点 down 键=%s 坐标=(%d,%d) 模式=%s 结果=%s 落点窗口=%s" %
            (vk_name(vk), p[0], p[1], self.cfg["mode"],
             "成功" if ok else ("失败" if ok is False else "未发送"), target))
        self.events_ui(("hit", (vk_name(vk), p[0], p[1], ok, target)))

    def touch_up(self, vk):
        a = self.active.pop(vk, None)
        if not a:
            return
        # 保证最短按压时长, 避免点击过快被忽略
        need = self.cfg["min_tap_ms"] / 1000.0
        dt = time.time() - a["t"]
        if dt < need:
            time.sleep(need - dt)
        self.inj.emit(a["pid"], a["x"], a["y"], "up", self.cfg["mode"],
                      others=self.others(vk))
        self.pid_pool.discard(a["pid"])
        if self.last_touch_vk == vk:
            ks = [k for k in self.active]
            self.last_touch_vk = ks[-1] if ks else None

    def release_all(self):
        for vk in list(self.active.keys()):
            self.touch_up(vk)

    def do_nudge(self, vk):
        if not self.active:
            return
        target = self.last_touch_vk if self.last_touch_vk in self.active else list(self.active)[-1]
        a = self.active[target]
        step = self.cfg["nudge_step"]
        if user32.GetAsyncKeyState(0x10) & 0x8000:
            step *= max(1, int(self.cfg["nudge_fast"]))
        if vk == 0x25:
            a["x"] -= step
        elif vk == 0x27:
            a["x"] += step
        elif vk == 0x26:
            a["y"] -= step
        elif vk == 0x28:
            a["y"] += step
        rx, ry, rw, rh = self.region()
        a["x"] = max(rx, min(rx + rw - 1, a["x"]))
        a["y"] = max(ry, min(ry + rh - 1, a["y"]))
        self.inj.emit(a["pid"], a["x"], a["y"], "move", self.cfg["mode"],
                      others=self.others(target))

    def do_repeat(self):
        now = time.time()
        for vk, a in list(self.active.items()):
            if now - a["t"] < self.cfg["repeat_delay_ms"] / 1000.0:
                continue
            if now < a["next"]:
                continue
            a["next"] = now + 1.0 / max(0.5, float(self.cfg["repeat_hz"]))
            o = self.others(vk)
            self.inj.emit(a["pid"], a["x"], a["y"], "up", self.cfg["mode"], others=o)
            time.sleep(max(0.005, self.cfg["min_tap_ms"] / 1000.0 / 2))
            self.inj.emit(a["pid"], a["x"], a["y"], "down", self.cfg["mode"], others=o)

    # ---------------- UI ----------------
    def build_ui(self):
        try:
            self.is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            self.is_admin = False
        dbg("  管理员权限=%s" % self.is_admin)
        self.ui_queue = queue.Queue()
        self.root = tk.Tk()
        self.root.title("%s %s - 键盘模拟触摸屏" % (APP_NAME, APP_VER))
        self.root.geometry("760x660")
        self.root.configure(bg="#F5F7FA")
        try:
            self.root.iconbitmap(default="")
        except Exception:
            pass

        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("TFrame", background="#F5F7FA")
        st.configure("TLabel", background="#F5F7FA", foreground="#1F2933", font=("Microsoft YaHei UI", 9))
        st.configure("TLabelframe", background="#F5F7FA")
        st.configure("TLabelframe.Label", background="#F5F7FA", foreground="#0B5FA5",
                     font=("Microsoft YaHei UI", 9, "bold"))
        st.configure("Header.TLabel", font=("Microsoft YaHei UI", 13, "bold"), foreground="#0B5FA5")
        st.configure("Big.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=6)
        st.configure("On.TButton", background="#A5D6A7")
        st.configure("Off.TButton", background="#E8EDF2")

        top = ttk.Frame(self.root, padding=(12, 10))
        top.pack(fill="x")
        ttk.Label(top, text="key2touch", style="Header.TLabel").pack(side="left")
        ttk.Label(top, text="用实体键盘模拟触摸屏 · 绿色版 v" + APP_VER).pack(side="left", padx=10)

        self.btn_enable = tk.Button(top, text="未启用", width=14, bg="#E8EDF2", fg="#1F2933",
                                    font=("Microsoft YaHei UI", 10, "bold"),
                                    command=self.toggle_enable, relief="flat", padx=8, pady=6)
        self.btn_enable.pack(side="right")

        # 新手引导条(启用后自动隐藏)
        self.var_guide = tk.StringVar()
        self.lbl_guide = tk.Label(self.root, textvariable=self.var_guide, bg="#FFF3C4",
                                  fg="#7A4E00", font=("Microsoft YaHei UI", 10, "bold"),
                                  pady=6, anchor="w", padx=12)
        self.lbl_guide.pack(fill="x", padx=12, pady=(0, 4))

        # 状态条
        stat = ttk.LabelFrame(self.root, text=" 运行状态 ", padding=8)
        stat.pack(fill="x", padx=12, pady=4)
        self.var_state = tk.StringVar()
        self.var_mon = tk.StringVar()
        self.var_touchapi = tk.StringVar()
        self.var_points = tk.StringVar()
        row = ttk.Frame(stat)
        row.pack(fill="x")
        ttk.Label(row, textvariable=self.var_state).pack(side="left", padx=(0, 16))
        ttk.Label(row, textvariable=self.var_mon).pack(side="left", padx=(0, 16))
        ttk.Label(row, textvariable=self.var_touchapi).pack(side="left", padx=(0, 16))
        ttk.Label(row, textvariable=self.var_points).pack(side="left")
        row2 = ttk.Frame(stat)
        row2.pack(fill="x", pady=(4, 0))
        self.var_last = tk.StringVar(value="最后一次按键: —")
        ttk.Label(row2, textvariable=self.var_last, foreground="#0B5FA5",
                  font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        self.var_fg = tk.StringVar(value="前台目标: —")
        self.lbl_fg = tk.Label(stat, textvariable=self.var_fg, anchor="w",
                               bg="#F5F7FA", fg="#5A6672", font=("Microsoft YaHei UI", 9))
        self.lbl_fg.pack(fill="x", pady=(3, 0))

        # 注入 / 显示器
        box = ttk.LabelFrame(self.root, text=" 注入方式与目标屏幕 ", padding=8)
        box.pack(fill="x", padx=12, pady=4)

        r1 = ttk.Frame(box)
        r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="注入方式:").pack(side="left")
        self.var_mode = tk.StringVar(value=self.cfg["mode"])
        for val, txt in (("touch", "真·触摸注入"), ("mouse", "鼠标事件"), ("both", "触摸+鼠标双发")):
            ttk.Radiobutton(r1, text=txt, value=val, variable=self.var_mode,
                            command=self.on_mode).pack(side="left", padx=8)
        ttk.Label(r1, text="(模拟器/普通程序选 鼠标；多点触摸程序选 触摸)",
                  foreground="#8A94A6").pack(side="left")
        r1b = ttk.Frame(box)
        r1b.pack(fill="x", pady=(0, 2))
        self.var_fb = tk.BooleanVar(value=self.cfg.get("touch_feedback", False))
        ttk.Checkbutton(r1b, text="显示系统触摸圆点反馈(仅供观察位置, 不代表真的点中了)",
                        variable=self.var_fb, command=self.on_fb).pack(side="left")
        ttk.Label(r1b, text="  ※ 触摸会被系统自动转成鼠标点击, 「双发」可能造成点击两次",
                  foreground="#B26A00").pack(side="left")

        r2 = ttk.Frame(box)
        r2.pack(fill="x", pady=2)
        ttk.Label(r2, text="目标显示器:").pack(side="left")
        self.var_mon_sel = tk.StringVar()
        names = ["%d. %s  %dx%d%s" % (i + 1, m["name"].replace("\\\\.\\", ""), m["w"], m["h"],
                                      "  (主)" if m["primary"] else "")
                 for i, m in enumerate(self.monitors)]
        self.cmb_mon = ttk.Combobox(r2, values=names, width=42, state="readonly",
                                    textvariable=self.var_mon_sel)
        self.cmb_mon.current(self.cfg["monitor_index"])
        self.cmb_mon.pack(side="left", padx=6)
        self.cmb_mon.bind("<<ComboboxSelected>>", self.on_mon_sel)
        ttk.Button(r2, text="重新检测", command=self.refresh_monitors).pack(side="left", padx=4)
        self.var_follow = tk.BooleanVar(value=self.cfg["follow_cursor_monitor"])
        ttk.Checkbutton(r2, text="自动跟随鼠标所在屏幕", variable=self.var_follow,
                        command=self.on_follow).pack(side="left", padx=10)

        r3 = ttk.Frame(box)
        r3.pack(fill="x", pady=2)
        self.var_region = tk.StringVar(value="整屏")
        ttk.Label(r3, text="映射区域:").pack(side="left")
        ttk.Label(r3, textvariable=self.var_region, foreground="#0B5FA5").pack(side="left", padx=6)
        ttk.Button(r3, text="整屏", command=lambda: self.set_region(None)).pack(side="left", padx=3)
        ttk.Button(r3, text="取当前窗口区域", command=self.grab_window).pack(side="left", padx=3)
        ttk.Button(r3, text="手动输入…", command=self.ask_region).pack(side="left", padx=3)

        # 映射参数
        mp = ttk.LabelFrame(self.root, text=" 映射参数 ", padding=8)
        mp.pack(fill="x", padx=12, pady=4)
        self.var_margin = tk.DoubleVar(value=self.cfg["margin_pct"])
        self.var_ext = tk.BooleanVar(value=self.cfg["use_ext_keys"])
        self.var_nudge = tk.BooleanVar(value=self.cfg["nudge_enabled"])
        self.var_step = tk.IntVar(value=self.cfg["nudge_step"])
        self.var_fast = tk.IntVar(value=self.cfg["nudge_fast"])

        rr = ttk.Frame(mp)
        rr.pack(fill="x", pady=2)
        ttk.Label(rr, text="边缘内缩 %:").pack(side="left")
        ttk.Scale(rr, from_=0, to=20, orient="horizontal", length=180,
                  variable=self.var_margin, command=lambda e: self.on_num()).pack(side="left")
        ttk.Label(rr, textvariable=self.var_margin).pack(side="left", padx=4)
        ttk.Checkbutton(rr, text="启用边缘扩展键(Tab/Caps/Shift/Enter)",
                        variable=self.var_ext, command=self.on_num).pack(side="left", padx=14)

        rr2 = ttk.Frame(mp)
        rr2.pack(fill="x", pady=2)
        ttk.Checkbutton(rr2, text="按住时方向键微调触点", variable=self.var_nudge,
                        command=self.on_num).pack(side="left")
        ttk.Label(rr2, text="步长 px:").pack(side="left", padx=(10, 2))
        ttk.Spinbox(rr2, from_=1, to=100, width=5, textvariable=self.var_step,
                    command=self.on_num).pack(side="left")
        ttk.Label(rr2, text="Shift 加速 x:").pack(side="left", padx=(10, 2))
        ttk.Spinbox(rr2, from_=1, to=20, width=5, textvariable=self.var_fast,
                    command=self.on_num).pack(side="left")

        # 连点
        rp = ttk.LabelFrame(self.root, text=" 连点 / 长按 ", padding=8)
        rp.pack(fill="x", padx=12, pady=4)
        self.var_rep = tk.BooleanVar(value=self.cfg["repeat_enabled"])
        self.var_hz = tk.DoubleVar(value=self.cfg["repeat_hz"])
        self.var_delay = tk.IntVar(value=self.cfg["repeat_delay_ms"])
        self.var_tap = tk.IntVar(value=self.cfg["min_tap_ms"])
        ttk.Checkbutton(rp, text="按住自动连点", variable=self.var_rep,
                        command=self.on_num).pack(side="left")
        ttk.Label(rp, text="频率 Hz:").pack(side="left", padx=(12, 2))
        ttk.Spinbox(rp, from_=1, to=60, width=5, textvariable=self.var_hz,
                    command=self.on_num).pack(side="left")
        ttk.Label(rp, text="触发延迟 ms:").pack(side="left", padx=(12, 2))
        ttk.Spinbox(rp, from_=0, to=5000, width=6, textvariable=self.var_delay,
                    command=self.on_num).pack(side="left")
        ttk.Label(rp, text="最短按压 ms:").pack(side="left", padx=(12, 2))
        ttk.Spinbox(rp, from_=0, to=500, width=5, textvariable=self.var_tap,
                    command=self.on_num).pack(side="left")

        # 热键 + 覆盖层
        hk = ttk.LabelFrame(self.root, text=" 热键与可视化覆盖层 ", padding=8)
        hk.pack(fill="x", padx=12, pady=4)
        self.var_toggle = tk.IntVar(value=self.cfg["toggle_vk"])
        self.var_hold = tk.IntVar(value=self.cfg["hold_vk"])
        self.var_ovk = tk.IntVar(value=self.cfg["overlay_vk"])
        self.var_ov = tk.BooleanVar(value=self.cfg["overlay"])
        self.var_olab = tk.BooleanVar(value=self.cfg["overlay_labels"])
        self.var_alpha = tk.DoubleVar(value=self.cfg["overlay_alpha"])

        a = ttk.Frame(hk)
        a.pack(fill="x", pady=2)
        ttk.Label(a, text="总开关:").pack(side="left")
        self.hotkey_combo(a, self.var_toggle, HOTKEY_CHOICES, 14).pack(side="left", padx=4)
        ttk.Label(a, text="按住生效键:").pack(side="left", padx=(14, 0))
        self.hotkey_combo(a, self.var_hold, HOLD_CHOICES, 12).pack(side="left", padx=4)
        ttk.Label(a, text="覆盖层开关:").pack(side="left", padx=(14, 0))
        self.hotkey_combo(a, self.var_ovk, HOTKEY_CHOICES, 14).pack(side="left", padx=4)

        b = ttk.Frame(hk)
        b.pack(fill="x", pady=2)
        ttk.Checkbutton(b, text="显示键位覆盖层", variable=self.var_ov,
                        command=self.on_num).pack(side="left")
        ttk.Checkbutton(b, text="显示键名", variable=self.var_olab,
                        command=self.on_num).pack(side="left", padx=8)
        ttk.Label(b, text="覆盖层浓度:").pack(side="left", padx=(12, 2))
        ttk.Scale(b, from_=0.15, to=1.0, orient="horizontal", length=140,
                  variable=self.var_alpha, command=lambda e: self.on_num()).pack(side="left")
        ttk.Button(b, text="打开覆盖层", command=lambda: self.set_overlay(True)).pack(side="left", padx=12)
        ttk.Button(b, text="关闭", command=lambda: self.set_overlay(False)).pack(side="left")

        self.var_automin = tk.BooleanVar(value=self.cfg.get("auto_minimize", True))
        ttk.Checkbutton(hk, text="启用后自动最小化（避免本窗口挡住按键落点）",
                        variable=self.var_automin, command=self.on_num).pack(anchor="w", pady=(3, 0))

        # 日志
        lg = ttk.LabelFrame(self.root, text=" 日志 ", padding=6)
        lg.pack(fill="both", expand=True, padx=12, pady=4)
        self.logbox = tk.Text(lg, height=8, wrap="word", bg="#FFFFFF", fg="#1F2933",
                              relief="flat", font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True)
        self.logbox.configure(state="disabled")

        # 底部按钮
        bt = ttk.Frame(self.root, padding=(12, 8))
        bt.pack(fill="x")
        ttk.Button(bt, text="保存配置", command=self.save_cfg).pack(side="left")
        ttk.Button(bt, text="恢复默认", command=self.reset_cfg).pack(side="left", padx=6)
        ttk.Button(bt, text="键位图…", command=self.show_map).pack(side="left", padx=6)
        ttk.Button(bt, text="帮助", command=self.show_help).pack(side="left", padx=6)
        ttk.Button(bt, text="按键落点图", command=self.open_diag).pack(side="left", padx=6)
        self.btn_test = tk.Button(bt, text="① 点我做自动诊断", command=self.manual_probe,
                                  bg="#A8DAF5", fg="#0B3A5E", relief="flat",
                                  font=("Microsoft YaHei UI", 9, "bold"), padx=8, pady=3)
        self.btn_test.pack(side="left", padx=6)
        if not self.is_admin:
            ttk.Button(bt, text="以管理员重启", command=self.restart_admin).pack(side="left", padx=6)
        self.var_top = tk.BooleanVar(value=False)
        ttk.Checkbutton(bt, text="窗口置顶", variable=self.var_top,
                        command=self.on_top).pack(side="left", padx=10)
        self.var_admin = tk.StringVar(value="")
        ttk.Label(bt, textvariable=self.var_admin).pack(side="left", padx=12)
        ttk.Button(bt, text="退出", command=self.quit_app).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.overlay = None
        self.update_state_labels()

        # 不做自动注入探测: 那会真的在屏幕上点一下。改用系统 digitizer 报告判断能力,
        # 需要真机验证时点按钮「自检注入」。
        if self.inj.touch_ready and not self.inj.touch_capable:
            self.cfg["mode"] = "mouse"
            self.var_mode.set("mouse")
            self.log("本系统未报告触摸屏支持(SM_DIGITIZER=0x%X): 触摸注入很可能无效, "
                     "已自动改用鼠标模式" % self.inj.digitizer)
        elif not self.inj.touch_ready:
            self.log("警告: 触摸注入不可用 (%s)。已自动回退到鼠标模式。" % self.inj.last_error)
            self.cfg["mode"] = "mouse"
            self.var_mode.set("mouse")
        else:
            self.log("触摸注入已就绪 (最多 %d 点)" % self.inj.max_points)
        self.log("DPI 模式: %s | 虚拟桌面: %dx%d @(%d,%d)" %
                 (DPI_MODE, self.inj.vw, self.inj.vh, self.inj.vx, self.inj.vy))
        self.log("检测到 %d 台显示器" % len(self.monitors))
        self.log("配置: %s" % self.cfg_path)
        if not self.is_admin:
            self.var_admin.set("提示: 若游戏/模拟器以管理员运行, 请以管理员身份启动本程序")
        self.update_region_label()
        self.update_state_labels()

    def hotkey_combo(self, parent, intvar, choices, width):
        table = {v: t for v, t in choices}
        names = [t for _, t in choices]
        s = tk.StringVar(value=table.get(intvar.get(), names[0]))
        cb = ttk.Combobox(parent, values=names, width=width, state="readonly", textvariable=s)

        def on_sel(e=None):
            val = s.get()
            for v, t in table.items():
                if t == val:
                    intvar.set(v)
                    self.on_num()
                    return
        cb.bind("<<ComboboxSelected>>", on_sel)
        return cb

    # ------------- UI 回调 -------------
    def on_mode(self):
        self.cfg["mode"] = self.var_mode.get()
        self.log("注入方式: %s" % self.cfg["mode"])
        if self.cfg["mode"] == "touch" and not self.inj.touch_capable:
            self.log("注意: 本系统未检测到触摸屏支持(SM_DIGITIZER=0x%X), 触摸注入可能无效, "
                     "建议用「鼠标事件」" % self.inj.digitizer)
        self.update_state_labels()

    def on_fb(self):
        self.cfg["touch_feedback"] = bool(self.var_fb.get())
        fb = TOUCH_FEEDBACK_DEFAULT if self.cfg["touch_feedback"] else TOUCH_FEEDBACK_NONE
        self.inj.init_touch(fb)
        self.log("系统触摸圆点反馈: %s" % ("开" if self.cfg["touch_feedback"] else "关"))

    def on_mon_sel(self, e=None):
        self.cfg["monitor_index"] = self.cmb_mon.current()
        self.release_all()
        self.update_region_label()
        self.refresh_overlay()

    def on_follow(self):
        self.cfg["follow_cursor_monitor"] = self.var_follow.get()

    def refresh_monitors(self):
        self.monitors = enum_monitors()
        names = ["%d. %s  %dx%d%s" % (i + 1, m["name"].replace("\\\\.\\", ""), m["w"], m["h"],
                                      "  (主)" if m["primary"] else "")
                 for i, m in enumerate(self.monitors)]
        self.cmb_mon["values"] = names
        self.cmb_mon.current(min(self.cfg["monitor_index"], len(self.monitors) - 1))
        self.log("已重新检测显示器: %d 台" % len(self.monitors))
        self.refresh_overlay()

    def toggle_enable(self):
        self.cfg["enabled"] = not self.cfg["enabled"]
        if not self.cfg["enabled"]:
            self.release_all()
        self.update_state_labels()
        if self.cfg["enabled"]:
            self.log("已启用。热键 %s 可随时关闭。" % vk_name(self.cfg["toggle_vk"]))
            if self.cfg.get("auto_minimize", True):
                # 本窗口留在最前面时, 注入的点击会落在本窗口上而不是游戏里,
                # 所以启用后自动让路。想保留窗口就取消勾选「启用后自动最小化」。
                self.root.after(1500, self._auto_min)

    def _auto_min(self):
        if self.cfg["enabled"] and not self.diag_test and not self.diag:
            try:
                self.root.iconify()
                self.log("已自动最小化(避免挡住落点)。要改设置从任务栏点回来即可。")
            except Exception:
                pass

    def on_num(self):
        c = self.cfg
        try:
            c["margin_pct"] = float(self.var_margin.get())
            c["use_ext_keys"] = bool(self.var_ext.get())
            c["nudge_enabled"] = bool(self.var_nudge.get())
            c["nudge_step"] = int(self.var_step.get())
            c["nudge_fast"] = int(self.var_fast.get())
            c["repeat_enabled"] = bool(self.var_rep.get())
            c["repeat_hz"] = float(self.var_hz.get())
            c["repeat_delay_ms"] = int(self.var_delay.get())
            c["min_tap_ms"] = int(self.var_tap.get())
            c["toggle_vk"] = int(self.var_toggle.get())
            c["hold_vk"] = int(self.var_hold.get())
            c["overlay_vk"] = int(self.var_ovk.get())
            c["overlay"] = bool(self.var_ov.get())
            c["overlay_labels"] = bool(self.var_olab.get())
            c["overlay_alpha"] = float(self.var_alpha.get())
            c["auto_minimize"] = bool(self.var_automin.get())
        except Exception:
            pass
        self.update_state_labels()
        self.refresh_overlay()

    def set_region(self, r):
        self.cfg["region"] = r
        self.update_region_label()
        self.refresh_overlay()

    def probe_touch(self):
        """真发一次注入验证是否真的可用(在无头/远程桌面/无触摸屏设备上会失败)。
           位置取目标区域左上角内侧 2px, 基本不会误触任何界面元素。"""
        if not self.inj.touch_ready:
            return
        rx, ry, rw, rh = self.region()
        x, y = rx + 2, ry + 2
        try:
            ok = self.inj.touch_frame([(0, x, y)], 0, x, y, "down")
            time.sleep(0.03)
            ok2 = self.inj.touch_frame([(0, x, y)], 0, x, y, "up")
            if not (ok and ok2):
                self.inj.touch_ready = False
                self.inj.last_error = "InjectTouchInput 返回失败(本机不支持触摸注入或权限不足)"
        except Exception as e:
            self.inj.touch_ready = False
            self.inj.last_error = str(e)

    # ------------- 诊断画布 -------------
    def open_diag(self):
        if self.diag:
            try:
                self.diag.lift()
                return
            except Exception:
                pass
        win = tk.Toplevel(self.root)
        win.title("诊断画布 — 按键落点自检")
        w, h = 900, 540
        win.geometry("%dx%d" % (w, h))
        win.configure(bg="#FFFFFF")
        cv = tk.Canvas(win, width=w - 20, height=h - 90, bg="#FFFDF5",
                       highlightthickness=1, highlightbackground="#FFD966")
        cv.pack(padx=10, pady=6)
        cv.bind("<Button-1>", self.diag_click)
        self.diag_cv = cv
        self.diag_cw, self.diag_ch = w - 20, h - 90
        self.diag_info = tk.StringVar()
        tk.Label(win, textvariable=self.diag_info, bg="#FFFFFF", fg="#1F2933",
                 font=("Microsoft YaHei UI", 9), justify="left", wraplength=w - 20).pack(anchor="w", padx=10)
        self.diag_hits = []
        self.diag = win
        win.protocol("WM_DELETE_WINDOW", self.close_diag)
        # 让画布能接收真实触摸: 注册后触摸点击会 promotion 成鼠标按下, tk 就能收到
        try:
            win.update()
            hwnd = int(win.winfo_id())
            user32.RegisterTouchWindow.argtypes = [wintypes.HWND, wintypes.ULONG]
            user32.RegisterTouchWindow.restype = ctypes.c_bool
            self.diag_touch = bool(user32.RegisterTouchWindow(hwnd, 0))
        except Exception:
            self.diag_touch = False
        self.draw_diag()
        self.log("诊断画布已打开: 蓝十字=按键已捕获, 红点=真实点击已到达画布"
                 + ("（已注册触摸接收）" if self.diag_touch else "（触摸注册失败, 仅能验证鼠标模式）"))

    def close_diag(self):
        try:
            self.diag.destroy()
        except Exception:
            pass
        self.diag = None

    def diag_map(self, x, y):
        rx, ry, rw, rh = self.region()
        nx = (x - rx) / float(rw)
        ny = (y - ry) / float(rh)
        return 20 + nx * (self.diag_cw - 40), 20 + ny * (self.diag_ch - 40)

    def draw_diag(self):
        if not self.diag:
            return
        cv = self.diag_cv
        cv.delete("all")
        cv.create_rectangle(20, 20, self.diag_cw - 20, self.diag_ch - 20,
                            outline="#0B5FA5", width=2)
        for vk, px, py in self.all_points():
            x, y = self.diag_map(px, py)
            cv.create_oval(x - 2, y - 2, x + 2, y + 2, fill="#9BB7CC")
            cv.create_text(x, y - 9, text=vk_name(vk), fill="#7A8B99",
                           font=("Microsoft YaHei UI", 7))
        for item in self.diag_hits:
            name, x, y = item
            cx, cy = self.diag_map(x, y)
            cv.create_line(cx - 9, cy, cx + 9, cy, fill="#0B5FA5", width=2)
            cv.create_line(cx, cy - 9, cx, cy + 9, fill="#0B5FA5", width=2)
            cv.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, outline="#0B5FA5", width=2)
            cv.create_text(cx, cy + 15, text=name, fill="#0B3A5E",
                           font=("Microsoft YaHei UI", 8, "bold"))
        self.update_diag_info()

    def diag_hit(self, name, x, y):
        self.diag_hits.append((name, x, y))
        if len(self.diag_hits) > 40:
            self.diag_hits = self.diag_hits[-40:]
        self.draw_diag()

    def diag_click(self, event):
        """画布自己收到鼠标左键 = 注入确实送达了系统"""
        if not self.diag:
            return
        cv = self.diag_cv
        cv.create_oval(event.x - 4, event.y - 4, event.x + 4, event.y + 4,
                       fill="#E53935", outline="#B71C1C")
        cv.create_text(event.x, event.y - 12, text="收到", fill="#B71C1C",
                       font=("Microsoft YaHei UI", 7))
        self.log("画布收到鼠标点击 (%d,%d) — 说明注入已送达系统" % (event.x, event.y))

    def update_diag_info(self):
        if not self.diag:
            return
        self.diag_info.set(
            "蓝十字 = 按键被捕获后的落点（出现即说明钩子与映射正常）\n"
            "红点   = 画布真的收到了点击（出现才说明产生了真实点击，不是视觉反馈）\n"
            "状态: %s | 模式: %s | 触摸API: %s | 系统触摸支持: %s | 钩子: %s" %
            ("已启用" if self.active_now() else "未启用",
             self.cfg["mode"],
             "可用" if self.inj.touch_ready else "不可用",
             "是" if self.inj.touch_capable else "否(建议用鼠标模式)",
             "已安装" if self.hook_id else "未安装"))

    def restart_admin(self):
        try:
            import subprocess
            exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
            ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, "", app_dir(), 1)
            self.quit_app()
        except Exception as e:
            self.log("以管理员重启失败: %s" % e)

    def on_top(self):
        try:
            self.root.attributes("-topmost", bool(self.var_top.get()))
        except Exception:
            pass

    def manual_probe(self):
        """闭环自动诊断: 自己建一个置顶靶子窗口 -> 自动注入一次点击 -> 看靶子是否真的收到。
        不需要用户按任何键, 结论直接指出问题断在哪一层。"""
        if self.diag_test:
            try:
                self.diag_test.lift()
                return
            except Exception:
                pass
        win = tk.Toplevel(self.root)
        win.title("自动诊断 — 自动执行中，请勿操作")
        w, h = 560, 400
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
        win.configure(bg="#FFFFFF")
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        cv = tk.Canvas(win, bg="#FFFDF5", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        self.diag_test = win

        hits = {"n": 0, "pts": []}

        def on_click(e):
            hits["n"] += 1
            hits["pts"].append((e.x, e.y))
            cv.create_oval(e.x - 14, e.y - 14, e.x + 14, e.y + 14,
                           outline="#E53935", width=3)
            cv.create_text(e.x, e.y + 28, text="第 %d 次真实点击" % hits["n"],
                           fill="#B71C1C", font=("Microsoft YaHei UI", 10, "bold"))

        cv.bind("<Button-1>", on_click)
        win.protocol("WM_DELETE_WINDOW", lambda: None)   # 诊断期间禁止关闭
        # 注册触摸接收: 否则 WM_TOUCH 不会被转换成 tk 能看到的 Button-1
        try:
            win.update()
            user32.RegisterTouchWindow.argtypes = [wintypes.HWND, wintypes.ULONG]
            user32.RegisterTouchWindow.restype = ctypes.c_bool
            user32.RegisterTouchWindow(int(win.winfo_id()), 0)
        except Exception:
            pass

        def draw(state, text, color="#0B5FA5"):
            cv.delete("all")
            cw, ch = max(cv.winfo_width(), 2), max(cv.winfo_height(), 2)
            r = min(cw, ch) // 4
            cx, cy = cw // 2, ch // 3
            cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=3)
            cv.create_oval(cx - 8, cy - 8, cx + 8, cy + 8, outline=color, width=2)
            cv.create_text(cw // 2, ch - 26, text=state, fill=color,
                           font=("Microsoft YaHei UI", 12, "bold"))
            cv.create_text(cw // 2, 18, text=text, fill="#5A6672",
                           font=("Microsoft YaHei UI", 9), width=cw - 30)

        def pump(ms):
            end = time.time() + ms / 1000.0
            while time.time() < end:
                try:
                    win.update()
                except Exception:
                    return
                time.sleep(0.008)

        def run():
            # 先把窗口摆稳并抢到前台: 未激活时第一次鼠标点击会被系统吃掉用于激活窗口,
            # 会让诊断误判成"注入没生效"
            try:
                win.deiconify()
                win.lift()
                win.attributes("-topmost", True)
                win.update_idletasks()
                win.update()
                user32.SetForegroundWindow(int(win.winfo_id()))
                win.focus_force()
            except Exception:
                pass
            pump(260)
            done = {"v": False}

            def do_inject():
                if done["v"]:
                    return
                done["v"] = True
                win.update_idletasks()
                cvw, cvh = max(cv.winfo_width(), 2), max(cv.winfo_height(), 2)
                cx = cv.winfo_rootx() + cvw // 2
                cy = cv.winfo_rooty() + cvh // 3
                mode = self.cfg["mode"]
                draw("正在注入一次点击…", "目标点 (%d, %d)　模式 %s" % (cx, cy, mode))
                win.update()
                pump(120)
                ok_t = ok_m = None
                pid = 5
                try:
                    if mode in ("touch", "both"):
                        ok_t = self.inj.touch_frame([(pid, cx, cy)], pid, cx, cy, "down")
                        pump(70)
                        self.inj.touch_frame([(pid, cx, cy)], pid, cx, cy, "up")
                    if mode in ("mouse", "both"):
                        ok_m = self.inj.mouse(cx, cy, "down")
                        pump(70)
                        self.inj.mouse(cx, cy, "up")
                except Exception as e:
                    self.log("自动诊断注入异常: %s" % e)
                pump(450)          # 等系统把触摸提升成鼠标消息并派发到窗口
                n = hits["n"]
                fg = fg_target_info(int(win.winfo_id()))
                dbg("自动诊断注入 mode=%s 目标=(%d,%d) hits=%d fg_self=%s"
                    % (mode, cx, cy, n, bool(fg and fg.get("self"))))
                self.finish_test(win, cv, n, ok_t, ok_m, mode, fg)

            # 等窗口真正拿到几何信息再注入(否则 winfo_rootx 可能还是 0)
            def wait_geom(tries=0):
                if cv.winfo_width() > 50 and cv.winfo_rootx() > 0 or tries > 25:
                    do_inject()
                else:
                    win.after(80, lambda: wait_geom(tries + 1))
            wait_geom()

        win.after(300, run)

    def finish_test(self, win, cv, n, ok_t, ok_m, mode, fg):
        """根据诊断结果给出明确结论, 不留"你猜"的空间"""
        cw = max(cv.winfo_width(), 2)
        lines = []
        ok = n > 0
        if ok:
            title = "✓ 注入完全正常（收到 %d 次真实点击）" % n
            color = "#2E7D32"
            lines.append("程序内部的钩子、坐标换算、注入方式和权限这一整条链路都是通的，"
                         "系统确实把你的注入当成了真实点击。")
            lines.append("")
            lines.append("所以「游戏里没反应」只剩下这几种可能：")
            lines.append("① 本窗口/覆盖层挡住了目标 —— 按键前把本程序最小化，让游戏在前台。")
            lines.append("② 游戏以管理员权限运行 —— 低权限的点击会被系统拦掉，"
                         "但光标照样会动（这就是「移过去了却点不到」）。请点「以管理员重启」。")
            lines.append("③ 游戏只认触摸或只认鼠标 —— 换另一种注入方式再试（触摸/鼠标/双发）。")
            lines.append("④ 触点落在按钮之间的空隙 —— 开覆盖层看清楚落点，或按方向键微调。")
        else:
            color = "#C62828"
            if mode in ("touch", "both"):
                title = "✗ 触摸注入没有生效"
                lines.append("注入函数返回%s，但目标窗口一个点击都没收到。"
                             % ("成功" if ok_t else "失败"))
                lines.append("这台机器很可能没有真实触摸屏（或触摸被系统丢弃）。"
                             "触摸注入在没有 digitizer 的设备上会静默失效，只剩一个视觉圆点。")
                lines.append("→ 请把注入方式改成「鼠标事件」，再点一次自动诊断。")
            else:
                title = "✗ 鼠标注入没有生效"
                lines.append("注入函数返回%s，但目标窗口没收到点击。" % ("成功" if ok_m else "失败"))
                lines.append("最常见原因：权限被拦截，或注入被安全软件阻止。")
                lines.append("→ 请点「以管理员重启」，再点一次自动诊断。")
        if fg and not fg["self"] and not ok:
            lines.append("")
            lines.append("（诊断窗口当时没拿到前台，前台是 %s，点击可能落到了它身上）"
                         % (fg["exe"] or fg["title"] or "未知"))
        body = "\n".join(lines)
        cv.delete("all")
        cv.create_text(20, 40, text=title, fill=color, anchor="nw",
                       font=("Microsoft YaHei UI", 15, "bold"))
        cv.create_text(20, 80, text=body, fill="#33414E", anchor="nw",
                       width=cw - 40, font=("Microsoft YaHei UI", 10), justify="left")
        cv.create_text(20, cv.winfo_height() - 24,
                       text="点此关闭（结果也已写入日志）", fill="#8A94A6", anchor="w",
                       font=("Microsoft YaHei UI", 9))
        cv.bind("<Button-1>", lambda e: self.close_test())

        self.log("自动诊断: 模式=%s 触摸返回=%s 鼠标返回=%s 收到点击=%d -> %s"
                 % (mode, ok_t, ok_m, n, "正常" if ok else "异常"))
        dbg("自动诊断 mode=%s touch=%s mouse=%s hits=%d" % (mode, ok_t, ok_m, n))

    def close_test(self):
        try:
            self.diag_test.destroy()
        except Exception:
            pass
        self.diag_test = None
        self.update_state_labels()

    def update_region_label(self):
        r = self.cfg["region"]
        if r:
            self.var_region.set("自定义 [%d, %d, %dx%d]" % (r[0], r[1], r[2], r[3]))
        else:
            m = self.monitors[self.cfg["monitor_index"]]
            self.var_region.set("整屏 %s  %dx%d" % (m["name"].replace("\\\\.\\", ""), m["w"], m["h"]))

    def grab_window(self):
        self.root.iconify()
        self.root.after(400, self._grab_window_do)

    def _grab_window_do(self):
        hwnd = user32.GetForegroundWindow()
        r = RECT()
        if hwnd and user32.GetWindowRect(hwnd, byref(r)):
            self.set_region([r.left, r.top, r.right - r.left, r.bottom - r.top])
            self.log("已取窗口区域: [%d,%d,%dx%d]" % (r.left, r.top, r.right - r.left, r.bottom - r.top))
        else:
            self.log("取窗口失败")
        self.root.deiconify()

    def ask_region(self):
        r = self.cfg["region"] or self.region()
        win = tk.Toplevel(self.root)
        win.title("自定义映射区域")
        win.geometry("320x180")
        win.configure(bg="#F5F7FA")
        vs = [tk.StringVar(value=str(v)) for v in r]
        for i, lab in enumerate(["X", "Y", "宽", "高"]):
            row = ttk.Frame(win, padding=4)
            row.pack(fill="x")
            ttk.Label(row, text=lab, width=6).pack(side="left")
            ttk.Entry(row, textvariable=vs[i], width=12).pack(side="left")

        def ok():
            try:
                vals = [int(v.get()) for v in vs]
                self.set_region(vals)
                self.log("区域设为: %s" % vals)
            except Exception:
                messagebox.showerror("错误", "请输入整数")
            win.destroy()
        ttk.Button(win, text="确定", command=ok).pack(pady=10)

    def reset_cfg(self):
        if messagebox.askyesno("确认", "恢复默认设置?"):
            self.cfg = dict(DEFAULT_CFG)
            self.save_cfg()
            self.log("已恢复默认设置, 重启程序生效")

    def quit_app(self):
        self.release_all()
        self.stop_flag = True
        try:
            if self.hook_tid:
                user32.PostThreadMessageW(self.hook_tid, 0x0012, 0, 0)  # WM_QUIT
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    # ------------- 状态刷新 -------------
    def fg_hint(self):
        """前台目标提示 —— 决定"注入的点击最后打到谁"。
        "光标移过去了却没点中"最常见的原因就在这一行：
        (a) 本程序窗口在最前面, 点击落在自己身上;
        (b) 目标以管理员运行, 低权限的点击被 UIPI 拦掉(但光标照样会动)。"""
        fg = fg_target_info(int(self.root.winfo_id()))
        if not fg:
            self.var_fg.set("前台目标: 无法读取")
            return
        name = fg["exe"] or (fg["title"][:28] or "未知窗口")
        if fg["self"]:
            self.var_fg.set("前台目标: 本程序自己 ← 按键的点击只会落在本窗口上，点不到游戏")
            self.lbl_fg.configure(fg="#B26A00")
        elif fg["elevated"] and not self.is_admin:
            self.var_fg.set("前台目标: %s【管理员运行】← 本程序不是管理员，点击会被系统拦掉"
                            "（光标会动但点不中）" % name)
            self.lbl_fg.configure(fg="#C62828")
        elif fg["elevated"]:
            self.var_fg.set("前台目标: %s【管理员运行】· 本程序同为管理员，可以注入 ✓" % name)
            self.lbl_fg.configure(fg="#2E7D32")
        else:
            self.var_fg.set("前台目标: %s · 普通权限 ✓" % name)
            self.lbl_fg.configure(fg="#2E7D32")

    def update_state_labels(self):
        on = self.active_now()
        self.btn_enable.configure(text="已启用 (点击关闭)" if on else "点此启用 / 热键",
                                  bg="#A5D6A7" if on else "#FFD966")
        self.var_state.set("状态: %s" % ("启用中" if on else "未启用"))
        m = self.monitors[self.cfg["monitor_index"]]
        self.var_mon.set("目标: %d/%d %s" % (self.cfg["monitor_index"] + 1, len(self.monitors),
                                             m["name"].replace("\\\\.\\", "")))
        self.var_touchapi.set("触摸API: %s | 系统触摸支持: %s" %
                              ("可用" if self.inj.touch_ready else "不可用",
                               "是" if self.inj.touch_capable else "否"))
        self.var_points.set("触点: %d | 模式: %s" % (len(self.active), self.cfg["mode"]))
        try:
            self.fg_hint()
        except Exception:
            pass
        if self.cfg["mode"] == "touch" and not self.inj.touch_capable and not self.active_now():
            self.var_guide.set("⚠ 本系统未检测到触摸屏支持 — 请把「注入方式」改成「鼠标事件」再用 "
                               "(触摸模式下你只会看到圆点反馈, 不会产生真实点击)")
        elif self.active_now():
            self.var_guide.set("已启用 — 请把本窗口最小化后切到游戏里按键；按 %s 关闭"
                               % vk_name(self.cfg["toggle_vk"]))
        else:
            self.var_guide.set("① 点右上角「点此启用」或按 %s  ② 最小化本窗口，切到游戏  "
                               "③ 按键盘字母/数字键  ← 不确定就先点「① 点我做自动诊断」"
                               % vk_name(self.cfg["toggle_vk"]))
        if self.diag:
            self.update_diag_info()

    def pump_ui(self):
        try:
            while True:
                ev, data = self.ui_queue.get_nowait()
                if ev == "log":
                    self.log(data)
                elif ev == "state":
                    self.update_state_labels()
                    if self.cfg["overlay"]:
                        self.refresh_overlay()
                elif ev == "overlay":
                    self.set_overlay(not bool(self.overlay))
                elif ev == "hit":
                    name, x, y, ok = data[0], data[1], data[2], data[3]
                    target = data[4] if len(data) > 4 else ""
                    self.var_last.set("最后一次按键: %s → (%d, %d) %s%s" %
                                      (name, x, y,
                                       "已注入" if ok else ("注入失败" if ok is False else "未知"),
                                       ("　落点窗口: " + target) if target else ""))
                    # 落点还在本程序身上 = 自己的窗口挡路了, 这时点多少都不会命中目标
                    if target.lower().startswith(APP_NAME.lower()) or APP_NAME.lower() in target.lower():
                        self.var_guide.set("⚠ 你的点击落在本程序自己身上了 —— 把本窗口最小化后切到目标程序再按键")
                        try:
                            self.lbl_guide.configure(bg="#FFD5D5", fg="#8B1A1A")
                        except Exception:
                            pass
                    elif self.active_now():
                        try:
                            self.lbl_guide.configure(bg="#FFF3C4", fg="#7A4E00")
                        except Exception:
                            pass
                    if self.diag:
                        self.diag_hit(name, x, y)
                elif ev == "minimize":
                    if self.cfg.get("auto_minimize", True) and not self.diag_test:
                        if self.diag:
                            self.close_diag()   # 诊断画布是置顶大窗口, 一样会挡住落点
                        try:
                            self.root.iconify()
                            self.log("已自动最小化(避免挡住落点)。要改设置从任务栏点回来。")
                        except Exception:
                            pass
        except Exception:
            pass
        # 跟随鼠标所在屏幕
        if self.cfg["follow_cursor_monitor"] and len(self.monitors) > 1:
            idx = monitor_at_cursor(self.monitors)
            if idx != self.cfg["monitor_index"] and not self.active:
                self.cfg["monitor_index"] = idx
                self.cmb_mon.current(idx)
                self.update_state_labels()
                self.refresh_overlay()
        prev = getattr(self, "_last_active_count", -1)
        if len(self.active) != prev:
            self._last_active_count = len(self.active)
            self.update_state_labels()
            if self.overlay:
                self.draw_overlay()
        self.root.after(80, self.pump_ui)

    # ------------- 覆盖层 -------------
    def set_overlay(self, on):
        if on:
            if self.overlay:
                self.refresh_overlay()
                return
            self.build_overlay()
        else:
            if self.overlay:
                try:
                    self.overlay.destroy()
                except Exception:
                    pass
                self.overlay = None
        self.cfg["overlay"] = on
        try:
            self.var_ov.set(on)
        except Exception:
            pass

    def build_overlay(self):
        rx, ry, rw, rh = self.region()
        ov = tk.Toplevel(self.root)
        ov.overrideredirect(True)
        ov.geometry("%dx%d+%d+%d" % (rw, rh, rx, ry))
        ov.configure(bg="#FFFFFF")
        ov.attributes("-topmost", True)
        ov.attributes("-transparentcolor", "#FFFFFF")
        # 注意: 绝不能再叠加 -alpha! 实测(像素级)证明 Tk 里 -alpha 会清掉
        # LWA_COLORKEY, 整个白色窗口会以半透明"白幕"糊满屏幕。
        # 半透明效果改用 draw_overlay 里的颜色深浅来模拟。
        cv = tk.Canvas(ov, width=rw, height=rh, bg="#FFFFFF", highlightthickness=0)
        cv.pack()
        self.overlay = ov
        self.overlay_cv = cv
        ov.update()
        self._make_click_through(ov)
        if not self._verify_color_key(ov):
            # 色键没生效 = 这层就是不透明的白幕, 直接关掉, 宁可没有辅助层也不挡屏
            try:
                ov.destroy()
            except Exception:
                pass
            self.overlay = None
            self.overlay_cv = None
            self.cfg["overlay"] = False
            try:
                self.var_ov.set(False)
            except Exception:
                pass
            self.log("⚠ 覆盖层无法透明(会变成白幕), 已自动关闭。可用 F12 重开。")
            dbg("覆盖层色键验证失败, 已关闭覆盖层")
            return
        dbg("覆盖层透明色键验证通过")
        self.draw_overlay()

    def _verify_color_key(self, ov):
        """当场确认"白色完全透明"真的生效。
        覆盖层是白底全屏窗口, 一旦色键(LWA_COLORKEY)没设上,
        它就是一层不透明的白色遮罩 —— 这正是之前"白幕挡屏"的原因。
        没生效就返回 False, 由调用方关掉覆盖层。"""
        try:
            hwnd = int(ov.winfo_id())
            root_hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
            ex = user32.GetWindowLongPtrW(root_hwnd, GWL_EXSTYLE)
            if not (int(ex) & WS_EX_LAYERED):
                return False
            key = wintypes.DWORD()
            alpha = ctypes.c_ubyte()
            flags = wintypes.DWORD()
            user32.GetLayeredWindowAttributes.argtypes = [
                wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(wintypes.DWORD)]
            user32.GetLayeredWindowAttributes.restype = wintypes.BOOL
            ok = user32.GetLayeredWindowAttributes(
                root_hwnd, ctypes.byref(key), ctypes.byref(alpha), ctypes.byref(flags))
            if not ok:
                return False
            has_key = bool(int(flags.value) & 1)          # LWA_COLORKEY
            white = (int(key.value) & 0xFFFFFF) == 0xFFFFFF
            dbg("覆盖层分层属性 flags=%s crKey=#%06X alpha=%s" %
                (int(flags.value), int(key.value) & 0xFFFFFF, int(alpha.value)))
            return has_key and white
        except Exception as e:
            dbg("覆盖层色键检查异常: %s" % e)
            return False

    def _make_click_through(self, ov):
        """让覆盖层真正鼠标穿透, 并且**当场验证**。
        这是最容易被忽略的致命点: 覆盖层一旦不穿透, 它会把所有注入的点击
        全部吃掉, 表现就是"光标移过去了、却点不动任何东西"。"""
        try:
            hwnd = int(ov.winfo_id())
            root_hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
            SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
            SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0010, 0x0020
            ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            for h in {hwnd, root_hwnd}:
                st = user32.GetWindowLongPtrW(h, GWL_EXSTYLE)
                user32.SetWindowLongPtrW(h, GWL_EXSTYLE, st | ex)
                user32.SetWindowPos(h, 0, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            ov.update()
            time.sleep(0.05)
            ov.update()
            # 验证: 覆盖层区域中心点下面如果还是自己, 说明没穿透
            rx, ry, rw, rh = self.region()
            hit = user32.WindowFromPoint(POINT(rx + rw // 2, ry + rh // 2))
            hit_root = user32.GetAncestor(hit, GA_ROOT) if hit else 0
            if hit_root and int(hit_root) == int(root_hwnd):
                self.log("⚠ 覆盖层无法鼠标穿透(会吃掉所有点击), 已自动关闭覆盖层")
                dbg("覆盖层穿透失败 hwnd=%s root=%s" % (hwnd, root_hwnd))
                try:
                    ov.destroy()
                except Exception:
                    pass
                self.overlay = None
                self.cfg["overlay"] = False
                try:
                    self.var_ov.set(False)
                except Exception:
                    pass
            else:
                dbg("覆盖层穿透验证通过 (下方窗口=%s)" % window_at_info(rx + rw // 2, ry + rh // 2))
        except Exception as e:
            dbg("覆盖层穿透设置异常: %s" % e)

    def refresh_overlay(self):
        if not self.overlay:
            return
        try:
            rx, ry, rw, rh = self.region()
            self.overlay.geometry("%dx%d+%d+%d" % (rw, rh, rx, ry))
            self.overlay_cv.configure(width=rw, height=rh)
            self.overlay.attributes("-alpha", self.cfg["overlay_alpha"])
            self.draw_overlay()
        except Exception:
            pass

    def draw_overlay(self):
        if not self.overlay:
            return
        cv = self.overlay_cv
        cv.delete("all")
        rx, ry, rw, rh = self.region()
        # 背景是完全透明的(色键), "不透明度"滑块改为控制元素颜色的深浅
        a = max(0.0, min(1.0, float(self.cfg.get("overlay_alpha", 0.8))))
        a = 0.35 + 0.65 * a          # 保底可见度, 拉到最低也不会完全消失
        def mix(c2):
            r1, g1, b1 = 255, 255, 255
            r2 = int(c2[1:3], 16); g2 = int(c2[3:5], 16); b2 = int(c2[5:7], 16)
            return "#%02X%02X%02X" % (int(r1 + (r2 - r1) * a),
                                      int(g1 + (g2 - g1) * a),
                                      int(b1 + (b2 - b1) * a))
        border_col = mix("#0B5FA5")
        line_col = mix("#9BB7CC")
        dot_col = mix("#0B5FA5")
        act_col = mix("#E53935")
        lbl_col = mix("#0B3A5E")
        # 区域边框
        cv.create_rectangle(2, 2, rw - 2, rh - 2, outline=border_col, width=2, dash=(6, 4))
        pts = self.all_points()
        # 网格辅助线(行)
        for row in range(ROW_MAX + 1):
            y = None
            for vk, px, py in pts:
                if self.layout()[vk][1] == row:
                    y = py - ry
                    break
            if y is not None:
                cv.create_line(0, y, rw, y, fill=line_col, dash=(2, 6))
        for vk, px, py in pts:
            x, y = px - rx, py - ry
            act = vk in self.active
            color = act_col if act else dot_col
            rr = 13 if act else 9
            cv.create_oval(x - rr, y - rr, x + rr, y + rr, fill=color, outline="#FFFFFF", width=2)
            if self.cfg["overlay_labels"]:
                cv.create_text(x, y - 20, text=vk_name(vk), fill=lbl_col,
                               font=("Microsoft YaHei UI", 9, "bold"))
        for vk, a2 in self.active.items():
            x, y = a2["x"] - rx, a2["y"] - ry
            cv.create_oval(x - 20, y - 20, x + 20, y + 20, outline=act_col, width=2, dash=(3, 3))

    # ------------- 键位图 / 帮助 -------------
    def show_map(self):
        win = tk.Toplevel(self.root)
        win.title("键位映射图")
        win.geometry("720x420")
        win.configure(bg="#FFFFFF")
        cv = tk.Canvas(win, bg="#FFFFFF", highlightthickness=0)
        cv.pack(fill="both", expand=True, padx=10, pady=10)
        rx, ry, rw, rh = self.region()
        sc = min((680 - 40) / rw, (380 - 40) / rh)
        ox, oy = 20, 20
        cv.create_rectangle(ox, oy, ox + rw * sc, oy + rh * sc, outline="#0B5FA5", width=2)
        for vk, px, py in self.all_points():
            x = ox + (px - rx) * sc
            y = oy + (py - ry) * sc
            cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#0B5FA5")
            cv.create_text(x, y - 12, text=vk_name(vk), fill="#1F2933",
                           font=("Microsoft YaHei UI", 8))
        ttk.Label(win, text="红/蓝点为各按键对应的屏幕触点位置; 方向键可在按住时微调").pack(pady=4)

    def show_help(self):
        txt = (
            "【怎么用】\n"
            "1. 选好目标显示器和注入方式, 点右上角「点此启用」或按总开关热键(默认 Scroll Lock)。\n"
            "2. 启用后主键盘区的每个键 = 屏幕上一个固定触点, 按下=手指按下, 松开=抬起。\n"
            "3. 同时按住多个键 = 真多点触控(最多 10 指), 可做双指缩放。\n"
            "4. 按住不放 = 长按; 开着连点 = 按住自动连发。\n"
            "5. 按住任意映射键时, 方向键可微调该触点位置(Shift 加速), 用于补精度。\n\n"
            "【不误触】\n"
            "· 总开关关闭时键盘完全正常打字。\n"
            "· 也可以只用「按住生效键」(默认右Alt): 按住时才变触摸屏, 松开立刻恢复。\n"
            "· Ctrl/Alt/Win 组合键永远放行, Alt+Tab、Ctrl+C 不受影响。\n\n"
            "【多显示器】\n"
            "· 下拉选择, 或 F9/F10 快速切换(可改热键), 或勾选「跟随鼠标所在屏幕」。\n"
            "· 用「取当前窗口区域」可以把映射限定在模拟器窗口内, 精度更高。\n\n"
            "【不生效怎么办】\n"
            "1. 游戏/模拟器以管理员运行 → 本程序也要「以管理员身份运行」(UIPI 限制)。\n"
            "2. 安卓模拟器多半只认鼠标 → 注入方式改「鼠标事件」或「双发」。\n"
            "3. 触摸API不可用(老系统/远程桌面) → 程序会自动回退到鼠标模式。\n\n"
            "【配置】\n"
            "· 配置保存在程序同目录 key2touch.config.json, 删掉即恢复默认, 真正的绿色版。\n"
        )
        messagebox.showinfo("使用说明", txt)

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------
def selftest():
    print("key2touch selftest")
    print("  sizeof POINTER_INFO       =", sizeof(POINTER_INFO))
    print("  sizeof POINTER_TOUCH_INFO =", sizeof(POINTER_TOUCH_INFO))
    print("  sizeof INPUT              =", sizeof(INPUT))
    inj = Injector(10)
    print("  touch injection ready     =", inj.touch_ready, inj.last_error)
    print("  virtual screen            =", (inj.vx, inj.vy, inj.vw, inj.vh))
    for i, m in enumerate(enum_monitors()):
        print("  monitor %d: %s %dx%d @(%d,%d) primary=%s" %
              (i, m["name"], m["w"], m["h"], m["x"], m["y"], m["primary"]))
    app = Key2TouchApp.__new__(Key2TouchApp)
    app.cfg = dict(DEFAULT_CFG)
    app.monitors = enum_monitors()
    print("  mapped points             =", len(app.all_points()))
    pts = app.all_points()[:6]
    print("  sample:", [(vk_name(v), x, y) for v, x, y in pts])
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        Key2TouchApp().run()
    except Exception:
        err = traceback.format_exc()
        dbg("!! 启动异常:\n" + err)
        try:
            messagebox.showerror("key2touch 启动失败", err)
        except Exception:
            pass
        try:
            sys.stderr.write(err)
        except Exception:
            pass
        sys.exit(1)
