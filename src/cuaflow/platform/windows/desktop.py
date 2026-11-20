"""``WindowsDesktop``: the native ``DesktopBackend`` (UIA via pywinauto/uiautomation, mss, SendInput)."""

from __future__ import annotations

import contextlib
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Iterator

from cuaflow.platform.base import IS_WINDOWS, WindowInfo
from cuaflow.platform.windows import _win32, input as _input, screen as _screen, uia as _uia

_INSTALL_HINT = "install the Windows extras: pip install 'cuaflow[win]'"

# classes that are never "unexpected popups"
_IGNORED_CLASSES = {
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "#32768", "tooltips_class32",
    "IME", "MSCTFIME UI", "Windows.UI.Core.CoreWindow", "SysShadow", "NotifyIconOverflowWindow",
}


class DesktopError(RuntimeError):
    pass


@contextlib.contextmanager
def pywinauto_errors(what: str) -> Iterator[None]:
    """Translate pywinauto/COM exceptions into clear built-in exceptions."""
    try:
        yield
    except ImportError as exc:
        raise DesktopError(f"{what}: missing dependency ({exc}); {_INSTALL_HINT}") from exc
    except (TimeoutError, LookupError, DesktopError):
        raise
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if name == "TimeoutError":  # pywinauto.timings.TimeoutError
            raise TimeoutError(f"{what}: timed out ({exc})") from exc
        if name in ("ElementNotFoundError", "WindowNotFoundError", "ElementAmbiguousError"):
            raise LookupError(f"{what}: {name}: {exc}") from exc
        if name in ("ElementNotEnabled", "ElementNotVisible"):
            raise DesktopError(f"{what}: element not interactable ({name}): {exc}") from exc
        raise DesktopError(f"{what}: {name}: {exc}") from exc


def _uiautomation():
    try:
        import uiautomation as auto  # type: ignore

        return auto
    except Exception:  # noqa: BLE001
        return None


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


class WindowsDesktop:
    """Native Windows desktop backend. All coordinates are physical pixels.

    ``window(...)`` returns a pywinauto ``WindowSpecification`` (UIA backend) bound to the
    matched window handle, so ``win.child_window(...)``, ``win.type_keys(...)``,
    ``win.print_control_identifiers()`` etc. all work in flow steps.
    """

    name = "windows"

    def __init__(self, default_timeout: float = 10.0) -> None:
        if not IS_WINDOWS:
            raise DesktopError("WindowsDesktop is only available on Windows (use FakeDesktop elsewhere)")
        _win32.ensure_dpi_awareness()
        self.default_timeout = default_timeout
        self._pwa: Any = None

    # ------------------------------------------------------------------ helpers
    def pywinauto_desktop(self) -> Any:
        """Lazily created ``pywinauto.Desktop(backend="uia")``."""
        if self._pwa is None:
            with pywinauto_errors("pywinauto init"):
                from pywinauto import Desktop  # type: ignore

                self._pwa = Desktop(backend="uia")
        return self._pwa

    def hwnd_of(self, window: Any | None) -> int:
        """Resolve a window-ish object (None → foreground) to a native handle."""
        if window is None:
            h = _win32.foreground_window()
            if not h:
                raise LookupError("there is no foreground window")
            return h
        if isinstance(window, bool):
            raise TypeError("invalid window")
        if isinstance(window, int):
            return window
        if isinstance(window, WindowInfo):
            if not window.handle:
                raise LookupError(f"WindowInfo {window.title!r} has no handle")
            return int(window.handle)
        if isinstance(window, str):
            return self.hwnd_of(self.window(title_re=window, timeout=self.default_timeout))
        handle = getattr(window, "handle", None)
        if isinstance(handle, int) and handle:
            return handle
        if hasattr(window, "wrapper_object"):
            with pywinauto_errors("resolve window"):
                return int(window.wrapper_object().handle)
        raise TypeError(f"cannot resolve a window handle from {type(window).__name__}")

    def wrapper_of(self, window: Any | None) -> Any:
        """pywinauto UIA wrapper for ``window``."""
        if window is not None and hasattr(window, "wrapper_object"):
            with pywinauto_errors("resolve window"):
                return window.wrapper_object()
        if window is not None and hasattr(window, "element_info"):
            return window
        hwnd = self.hwnd_of(window)
        with pywinauto_errors("resolve window"):
            return self.pywinauto_desktop().window(handle=hwnd).wrapper_object()

    def info(self, hwnd: int, **extra: Any) -> WindowInfo:
        pid = _win32.window_pid(hwnd)
        return WindowInfo(
            title=_win32.window_text(hwnd),
            process=_win32.process_name(pid),
            pid=pid,
            handle=int(hwnd),
            class_name=_win32.class_name(hwnd),
            rect=_win32.window_rect(hwnd),
            is_modal=bool(extra.pop("is_modal", False)),
            extra=extra,
        )

    # ------------------------------------------------------------------ observation
    def screenshot(self, path: str | Path, window: Any | None = None) -> Path:
        """Whole virtual screen, or the on-screen rectangle of ``window`` (not activated)."""
        if window is None:
            return _screen.grab(path)
        return _screen.grab_window(path, self.hwnd_of(window))

    def ui_tree(self, window: Any | None = None, max_depth: int = 8, max_nodes: int = 400) -> str:
        hwnd = self.hwnd_of(window)
        inf = self.info(hwnd)
        header = f"# window hwnd=0x{hwnd:X} process={inf.process or '?'} pid={inf.pid} title={inf.title!r}"
        if _uiautomation() is not None:
            try:
                body = _uia.dump_with_uiautomation(hwnd, max_depth=max_depth, max_nodes=max_nodes)
            except Exception:  # noqa: BLE001
                body = _uia.dump_with_pywinauto(self.wrapper_of(hwnd), max_depth, max_nodes)
        else:
            with pywinauto_errors("ui_tree"):
                body = _uia.dump_with_pywinauto(self.wrapper_of(hwnd), max_depth, max_nodes)
        lines = [header, body]
        try:
            modals = [m for m in self.modal_dialogs() if m.handle != hwnd or m.extra.get("embedded")]
            if modals:
                lines.append("[modal dialogs open: " + "; ".join(
                    f"{m.title!r} ({m.process}, {m.extra.get('reason', '')})" for m in modals) + "]")
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(lines)

    def windows(self) -> list[WindowInfo]:
        """Visible top-level application windows (Alt-Tab-like), topmost first."""
        return [self.info(h) for h in _win32.enum_windows() if _win32.is_app_window(h)]

    def foreground(self) -> WindowInfo | None:
        h = _win32.foreground_window()
        return self.info(h) if h else None

    def modal_dialogs(self) -> list[WindowInfo]:
        """Top-level dialogs/popups that may block automation.

        Detected by: dialog class ``#32770``; owned windows with a caption/dialog frame
        (``owned_modal`` when the owner is disabled, else ``owned_popup``); UIA
        ``WindowPattern.IsModal``; and UIA ``Window`` children embedded inside the
        foreground window (e.g. XAML ContentDialogs in modern apps, ``embedded``).
        """
        auto = _uiautomation()
        found: list[WindowInfo] = []
        for h in _win32.enum_windows():
            try:
                if not _win32.is_visible(h) or _win32.is_cloaked(h):
                    continue
                cls = _win32.class_name(h)
                if cls in _IGNORED_CLASSES:
                    continue
                l, t, r, b = _win32.window_rect(h)
                if r - l < 2 or b - t < 2:
                    continue
                style = _win32.get_long(h, _win32.GWL_STYLE)
                exstyle = _win32.get_long(h, _win32.GWL_EXSTYLE)
                title = _win32.window_text(h)
                own = _win32.owner(h)
                reason = ""
                if cls == "#32770":
                    reason = "dialog_class"
                elif own and (style & _win32.WS_CAPTION or exstyle & _win32.WS_EX_DLGMODALFRAME) and title:
                    reason = "owned_modal" if not _win32.is_enabled(own) else "owned_popup"
                elif auto is not None and title and style & _win32.WS_CAPTION:
                    try:
                        with auto.UIAutomationInitializerInThread():
                            ctrl = auto.ControlFromHandle(h)
                            wp = ctrl.GetPattern(auto.PatternId.WindowPattern) if ctrl else None
                            if wp is not None and bool(wp.IsModal):
                                reason = "uia_is_modal"
                    except Exception:  # noqa: BLE001
                        pass
                if reason:
                    found.append(self.info(h, is_modal=True, reason=reason, owner=own))
            except Exception:  # noqa: BLE001
                continue
        if auto is not None:
            fg = _win32.foreground_window()
            if fg:
                try:
                    with auto.UIAutomationInitializerInThread():
                        root = auto.ControlFromHandle(fg)
                        for ch in (root.GetChildren() if root else []):
                            if getattr(ch, "ControlTypeName", "") == "WindowControl" and not ch.NativeWindowHandle:
                                inf = self.info(fg, is_modal=True, reason="embedded", embedded=True)
                                inf.title = ch.Name or inf.title
                                r = ch.BoundingRectangle
                                inf.rect = (r.left, r.top, r.right, r.bottom)
                                found.append(inf)
                except Exception:  # noqa: BLE001
                    pass
        return found

    # ------------------------------------------------------------------ window / app control
    def launch(self, command: str, args: list[str] | None = None, cwd: str | None = None) -> int:
        """Start a program (or open a document / App Paths name like ``excel``); returns the pid (0 if unknown).

        Note: some apps (Windows 11 Notepad, Office) hand off to another process, so locate
        their windows by title/process rather than by the returned pid.
        """
        args = list(args or [])
        try:
            proc = subprocess.Popen(  # noqa: S603
                [command, *args], cwd=cwd, close_fds=True,
                creationflags=0x00000200,  # CREATE_NEW_PROCESS_GROUP
            )
            return proc.pid
        except OSError:
            pass
        ps = f"$p = Start-Process -FilePath {_ps_quote(command)} -PassThru"
        if args:
            ps += " -ArgumentList @(" + ",".join(_ps_quote(a) for a in args) + ")"
        if cwd:
            ps += f" -WorkingDirectory {_ps_quote(cwd)}"
        ps += "; if ($p) { $p.Id } else { 0 }"
        from cuaflow.platform.shell import LocalShell

        res = LocalShell(shell="powershell", blocked_patterns=[]).run(ps, timeout=60)
        if not res.ok:
            raise DesktopError(f"failed to launch {command!r}: {res.stderr.strip() or res.stdout.strip()}")
        try:
            return int(res.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0

    def find_windows(
        self,
        title: str | None = None,
        title_re: str | None = None,
        process: str | None = None,
        class_name: str | None = None,
    ) -> list[int]:
        """Handles of visible top-level windows matching all given criteria (foreground first).

        ``title`` is exact; ``title_re`` uses ``re.search``; ``process`` is the exe name
        (case-insensitive, ``.exe`` optional) or a pid as a string/int.
        """
        rx = re.compile(title_re) if title_re else None
        proc_l = str(process).lower() if process is not None else None
        if proc_l and not proc_l.isdigit() and not proc_l.endswith(".exe"):
            proc_l += ".exe"
        out: list[int] = []
        for h in _win32.enum_windows():
            if not _win32.is_visible(h) or _win32.is_cloaked(h):
                continue
            l, t, r, b = _win32.window_rect(h)
            if r - l < 2 or b - t < 2:
                continue
            text = _win32.window_text(h)
            if title is not None and text != title:
                continue
            if rx is not None and not rx.search(text):
                continue
            if class_name is not None and _win32.class_name(h) != class_name:
                continue
            if proc_l is not None:
                pid = _win32.window_pid(h)
                if proc_l.isdigit():
                    if pid != int(proc_l):
                        continue
                elif _win32.process_name(pid).lower() != proc_l:
                    continue
            if title is None and rx is None and class_name is None and not text.strip():
                continue  # process-only search: skip untitled helper windows
            out.append(h)
        fg = _win32.foreground_window()
        if fg in out:
            out.remove(fg)
            out.insert(0, fg)
        return out

    def window(
        self,
        title: str | None = None,
        title_re: str | None = None,
        process: str | None = None,
        class_name: str | None = None,
        timeout: float = 10,
    ) -> Any:
        crit = {k: v for k, v in dict(title=title, title_re=title_re, process=process, class_name=class_name).items()
                if v is not None}
        if not crit:
            raise ValueError("window(): give at least one of title, title_re, process, class_name")
        deadline = time.monotonic() + timeout
        hwnd = 0
        while True:
            hits = self.find_windows(title, title_re, process, class_name)
            if hits:
                hwnd = hits[0]
                break
            if time.monotonic() >= deadline:
                visible = ", ".join(repr(w.title) for w in self.windows()[:15])
                raise TimeoutError(f"no window matching {crit} within {timeout:g}s; visible windows: {visible}")
            time.sleep(0.2)
        spec = self.pywinauto_desktop().window(handle=hwnd)
        remaining = max(deadline - time.monotonic(), 1.0)
        try:
            with pywinauto_errors(f"window {crit}"):
                spec.wait("exists visible ready", timeout=remaining)
        except TimeoutError as exc:
            note = ""
            if not _win32.is_enabled(hwnd):
                modals = [m.title for m in self.modal_dialogs()]
                note = f" (window is disabled – probably blocked by a modal dialog: {modals})"
            raise TimeoutError(f"window {crit} (hwnd=0x{hwnd:X}) found but not ready{note}") from exc
        return spec

    def activate(self, window: Any) -> None:
        hwnd = self.hwnd_of(window)
        try:
            self.wrapper_of(hwnd).set_focus()
        except Exception:  # noqa: BLE001
            if not _win32.set_foreground(hwnd):
                raise DesktopError(f"could not bring window 0x{hwnd:X} to the foreground") from None

    def close_window(self, window: Any) -> None:
        """Ask the window to close (WindowPattern.Close / WM_CLOSE). Save prompts are not handled."""
        hwnd = self.hwnd_of(window)
        try:
            self.wrapper_of(hwnd).close()
        except Exception:  # noqa: BLE001
            _win32.post_close(hwnd)

    # ------------------------------------------------------------------ input
    def type_text(self, text: str, interval: float = 0.0) -> None:
        with pywinauto_errors("type_text"):
            _input.type_text(text, interval)

    def type_keys(self, keys: str) -> None:
        with pywinauto_errors("type_keys"):
            _input.send_keys(keys)

    def hotkey(self, *keys: str) -> None:
        with pywinauto_errors("hotkey"):
            _input.hotkey(*keys)

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        with pywinauto_errors("click"):
            _input.click(x, y, button, double)

    def scroll(self, x: int, y: int, clicks: int) -> None:
        with pywinauto_errors("scroll"):
            _input.scroll(x, y, clicks)

    # ------------------------------------------------------------------ conveniences
    def wait_window_gone(self, window: Any = None, timeout: float = 10, **criteria: Any) -> bool:
        from cuaflow.platform.windows.apps import wait_window_gone

        return wait_window_gone(self, window, timeout=timeout, **criteria)

    def dismiss_common_dialogs(self, **kwargs: Any) -> list[str]:
        from cuaflow.platform.windows.apps import dismiss_common_dialogs

        return dismiss_common_dialogs(self, **kwargs)

    def excel_count_rows(self, path: str | Path, sheet: str | None = None) -> int:
        from cuaflow.platform.windows.apps import excel_count_rows

        return excel_count_rows(path, sheet)
