# -*- coding: utf-8 -*-
"""Windows 托盘图标、开机自启与单实例工具（纯 ctypes，无第三方依赖）。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import winreg

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_NULL = 0x0000
WM_TRAY = 0x8000 + 1
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO = 0x00000001

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
IDI_APPLICATION = 32512

MF_STRING, MF_SEPARATOR, MF_CHECKED, MF_UNCHECKED = 0x0, 0x800, 0x8, 0x0
MF_BYCOMMAND = 0x0
TPM_RETURNCMD, TPM_RIGHTBUTTON, TPM_LEFTALIGN = 0x0100, 0x0002, 0x0

ERROR_ALREADY_EXISTS = 183


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", wt.BYTE * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD),
                ("hWnd", wt.HWND),
                ("uID", wt.UINT),
                ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT),
                ("hIcon", wt.HICON),
                ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD),
                ("dwStateMask", wt.DWORD),
                ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT),
                ("szInfoTitle", wt.WCHAR * 64),
                ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID),
                ("hBalloonIcon", wt.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DestroyWindow.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                              ctypes.c_int, ctypes.c_int, wt.UINT]
user32.LoadImageW.restype = wt.HANDLE
user32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = wt.HICON
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.CheckMenuItem.argtypes = [wt.HMENU, wt.UINT, wt.UINT]
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wt.HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
user32.RegisterWindowMessageW.restype = wt.UINT
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wt.BOOL
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.CreateEventW.restype = wt.HANDLE
kernel32.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
kernel32.OpenEventW.restype = wt.HANDLE
kernel32.SetEvent.argtypes = [wt.HANDLE]
kernel32.SetEvent.restype = wt.BOOL
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD
kernel32.CloseHandle.argtypes = [wt.HANDLE]


# --------------------------------------------------------------------------
# 开机自启
# --------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "SystemMonitor"


def _autostart_command(entry_script: str) -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --startup'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{entry_script}" --startup'


def is_autostart_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
        value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool, entry_script: str) -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _autostart_command(entry_script))
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# 单实例 / 二次启动唤醒
# --------------------------------------------------------------------------
MUTEX_NAME = "SystemMonitor.SingleInstance"
EVENT_NAME = "SystemMonitor.Activate"
_mutex_ref = None


def acquire_single_instance() -> bool:
    """创建命名互斥体；已存在同名实例时返回 False。"""
    global _mutex_ref
    _mutex_ref = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def signal_running_instance() -> None:
    handle = kernel32.OpenEventW(0x0002, False, EVENT_NAME)
    if handle:
        kernel32.SetEvent(handle)
        kernel32.CloseHandle(handle)


class ActivationListener:
    """等待第二个实例发来的唤醒信号，回调在独立线程触发。"""

    def __init__(self, callback):
        self._callback = callback
        self._handle = kernel32.CreateEventW(None, False, False, EVENT_NAME)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            result = kernel32.WaitForSingleObject(self._handle, 500)
            if result == 0 and not self._stop.is_set():
                self._callback()

    def stop(self):
        self._stop.set()
        if self._handle:
            kernel32.SetEvent(self._handle)


# --------------------------------------------------------------------------
# 托盘图标
# --------------------------------------------------------------------------
class TrayIcon:
    """系统托盘图标 + 右键菜单。回调在托盘线程中执行，调用方需自行转投主线程。"""

    def __init__(self, icon_path: str, tooltip: str, on_action):
        self.icon_path = icon_path
        self.tooltip = tooltip
        self._on_action = on_action
        self.state = {"float": False, "autostart": False}
        self._hwnd = None
        self._nid = None
        self._icon = None
        self._taskbar_msg = 0
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._wndproc = WNDPROC(self._wnd_proc)

    # -- 生命周期 --------------------------------------------------------

    def start(self, timeout: float = 5.0) -> bool:
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(timeout)
        return bool(self._hwnd)

    def stop(self):
        with self._lock:
            if self._hwnd:
                if self._nid is not None:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    def set_state(self, float_visible: bool, autostart: bool):
        self.state["float"] = float_visible
        self.state["autostart"] = autostart

    def notify(self, title: str, text: str):
        with self._lock:
            if self._nid is None:
                return
            nid = self._nid
            nid.uFlags = NIF_INFO
            nid.szInfoTitle = title[:63]
            nid.szInfo = text[:255]
            nid.dwInfoFlags = NIIF_INFO
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP

    # -- 内部 ------------------------------------------------------------

    def _run(self):
        try:
            instance = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
            wc.hInstance = instance
            wc.lpszClassName = "SysMonitorTrayWindow"
            user32.RegisterClassW(ctypes.byref(wc))

            self._hwnd = user32.CreateWindowExW(
                0, "SysMonitorTrayWindow", "SysMonitorTray", 0,
                0, 0, 0, 0, None, None, instance, None)
            if not self._hwnd:
                return
            self._taskbar_msg = user32.RegisterWindowMessageW("TaskbarCreated")
            self._icon = self._load_icon()
            self._add_icon()
        finally:
            self._ready.set()

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _load_icon(self):
        handle = 0
        if os.path.exists(self.icon_path):
            handle = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, 32, 32,
                                       LR_LOADFROMFILE)
        if not handle:
            handle = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        return handle

    def _add_icon(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._icon
        nid.szTip = self.tooltip
        if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            self._nid = nid

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event == WM_RBUTTONUP:
                self._show_menu(hwnd)
            elif event == WM_LBUTTONDBLCLK:
                self._on_action("open")
            elif event == WM_LBUTTONUP:
                self._on_action("toggle_float")
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        if self._taskbar_msg and msg == self._taskbar_msg:
            self._add_icon()
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self, hwnd):
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        user32.AppendMenuW(menu, MF_STRING, 1, "打开主界面")
        user32.AppendMenuW(menu, MF_STRING, 2, "显示悬浮窗")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, 3, "开机自动启动")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, 4, "退出")

        user32.CheckMenuItem(menu, 2, MF_BYCOMMAND | (MF_CHECKED if self.state["float"] else MF_UNCHECKED))
        user32.CheckMenuItem(menu, 3, MF_BYCOMMAND | (MF_CHECKED if self.state["autostart"] else MF_UNCHECKED))

        position = wt.POINT()
        user32.GetCursorPos(ctypes.byref(position))
        user32.SetForegroundWindow(hwnd)
        command = user32.TrackPopupMenu(
            menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_LEFTALIGN,
            position.x, position.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)

        if command == 1:
            self._on_action("open")
        elif command == 2:
            self.state["float"] = not self.state["float"]
            self._on_action("toggle_float")
        elif command == 3:
            self.state["autostart"] = not self.state["autostart"]
            self._on_action("toggle_autostart")
        elif command == 4:
            self._on_action("quit")
