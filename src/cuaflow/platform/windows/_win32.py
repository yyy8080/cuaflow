"""Thin ctypes wrappers over user32/kernel32/dwmapi (no pywin32 dependency).

Every function touches ``ctypes.windll`` lazily, so this module imports fine on Linux.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Callable

IS_WINDOWS = sys.platform == "win32"

GW_OWNER = 4
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CAPTION = 0x00C00000
WS_POPUP = 0x80000000
WS_DISABLED = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_TOPMOST = 0x00000008
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
SW_RESTORE = 9

_dpi_done = False


def _user32():
    if not IS_WINDOWS:
        raise RuntimeError("Windows desktop API is only available on Windows")
    return ctypes.windll.user32  # type: ignore[attr-defined]


def ensure_dpi_awareness() -> None:
    """Make this process per-monitor DPI aware (once) so coordinates are physical pixels."""
    global _dpi_done
    if _dpi_done or not IS_WINDOWS:
        return
    _dpi_done = True
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]  # PER_MONITOR_AWARE
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def enum_windows() -> list[int]:
    """Top-level window handles in Z-order (topmost first)."""
    user32 = _user32()
    handles: list[int] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):  # noqa: ANN001
        handles.append(int(hwnd))
        return True

    user32.EnumWindows(proto(_cb), 0)
    return handles


def window_text(hwnd: int) -> str:
    user32 = _user32()
    n = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
    return buf.value


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32().GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def is_window(hwnd: int) -> bool:
    return bool(_user32().IsWindow(wintypes.HWND(hwnd)))


def is_visible(hwnd: int) -> bool:
    return bool(_user32().IsWindowVisible(wintypes.HWND(hwnd)))


def is_enabled(hwnd: int) -> bool:
    return bool(_user32().IsWindowEnabled(wintypes.HWND(hwnd)))


def is_minimized(hwnd: int) -> bool:
    return bool(_user32().IsIconic(wintypes.HWND(hwnd)))


def get_long(hwnd: int, index: int) -> int:
    user32 = _user32()
    fn = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    fn.restype = ctypes.c_ssize_t
    return int(fn(wintypes.HWND(hwnd), index)) & 0xFFFFFFFF


def owner(hwnd: int) -> int:
    user32 = _user32()
    user32.GetWindow.restype = wintypes.HWND
    return int(user32.GetWindow(wintypes.HWND(hwnd), GW_OWNER) or 0)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Visible frame bounds (without drop shadow when DWM is available)."""
    rect = wintypes.RECT()
    try:
        res = ctypes.windll.dwmapi.DwmGetWindowAttribute(  # type: ignore[attr-defined]
            wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
        )
        if res == 0:
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:  # noqa: BLE001
        pass
    _user32().GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return (rect.left, rect.top, rect.right, rect.bottom)


def is_cloaked(hwnd: int) -> bool:
    val = ctypes.c_int(0)
    try:
        res = ctypes.windll.dwmapi.DwmGetWindowAttribute(  # type: ignore[attr-defined]
            wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)
        )
        return res == 0 and val.value != 0
    except Exception:  # noqa: BLE001
        return False


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def foreground_window() -> int:
    user32 = _user32()
    user32.GetForegroundWindow.restype = wintypes.HWND
    return int(user32.GetForegroundWindow() or 0)


def process_name(pid: int) -> str:
    """Executable base name of ``pid`` (psutil if available, else QueryFullProcessImageNameW)."""
    if not pid:
        return ""
    try:
        import psutil  # type: ignore

        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        pass
    if not IS_WINDOWS:
        return ""
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(handle)


def set_foreground(hwnd: int) -> bool:
    """Bring ``hwnd`` to the foreground (restoring it, with the ALT-key focus-steal workaround)."""
    user32 = _user32()
    h = wintypes.HWND(hwnd)
    if user32.IsIconic(h):
        user32.ShowWindow(h, SW_RESTORE)
    if user32.SetForegroundWindow(h):
        return True
    VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x2
    user32.keybd_event(VK_MENU, 0, 0, 0)
    ok = bool(user32.SetForegroundWindow(h))
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    return ok


def post_close(hwnd: int) -> None:
    WM_CLOSE = 0x0010
    _user32().PostMessageW(wintypes.HWND(hwnd), WM_CLOSE, 0, 0)


def is_app_window(hwnd: int) -> bool:
    """Visible, titled, non-tool, non-cloaked top-level window (what a user sees in Alt-Tab)."""
    if not is_visible(hwnd) or is_cloaked(hwnd):
        return False
    if not window_text(hwnd).strip():
        return False
    if get_long(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
        return False
    l, t, r, b = window_rect(hwnd)
    return (r - l) > 1 and (b - t) > 1


def find_windows(predicate: Callable[[int], bool]) -> list[int]:
    return [h for h in enum_windows() if predicate(h)]
