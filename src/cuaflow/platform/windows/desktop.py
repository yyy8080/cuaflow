"""``WindowsDesktop``：Windows 原生桌面后端实现（基于 pywinauto/uiautomation 的 UIA、mss 截图与 SendInput 输入）。"""

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

# 绝不会作为“意外弹窗”处理的系统窗口类名集合
_IGNORED_CLASSES = {
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "#32768", "tooltips_class32",
    "IME", "MSCTFIME UI", "Windows.UI.Core.CoreWindow", "SysShadow", "NotifyIconOverflowWindow",
}


class DesktopError(RuntimeError):
    pass


@contextlib.contextmanager
def pywinauto_errors(what: str) -> Iterator[None]:
    """将 pywinauto 或 COM 底层异常转换为清晰的 Python 内置异常或 DesktopError。"""
    try:
        yield
    except ImportError as exc:
        raise DesktopError(f"{what}: missing dependency ({exc}); {_INSTALL_HINT}") from exc
    except (TimeoutError, LookupError, DesktopError):
        raise
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if name == "TimeoutError":  # 对应 pywinauto.timings.TimeoutError 超时异常
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
    """Windows 原生桌面后端。所有坐标均采用物理屏幕像素。

    ``window(...)`` 返回绑定到目标窗口句柄的 pywinauto ``WindowSpecification``（UIA 后端），
    因此在工作流步骤中可直接调用 ``win.child_window(...)``、``win.type_keys(...)``、
    ``win.print_control_identifiers()`` 等方法。
    """

    name = "windows"

    def __init__(self, default_timeout: float = 10.0) -> None:
        if not IS_WINDOWS:
            raise DesktopError("WindowsDesktop is only available on Windows (use FakeDesktop elsewhere)")
        _win32.ensure_dpi_awareness()
        self.default_timeout = default_timeout
        self._pwa: Any = None

    # ------------------------------------------------------------------ 内部辅助方法
    def pywinauto_desktop(self) -> Any:
        """按需延迟创建的 ``pywinauto.Desktop(backend="uia")`` 实例。"""
        if self._pwa is None:
            with pywinauto_errors("pywinauto init"):
                from pywinauto import Desktop  # type: ignore

                self._pwa = Desktop(backend="uia")
        return self._pwa

    def hwnd_of(self, window: Any | None) -> int:
        """将各类窗口引用对象（``None`` 表示当前前台窗口）解析为原生窗口句柄 HWND。"""
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
        """获取 ``window`` 对应的 pywinauto UIA 控件包装对象。"""
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

    # ------------------------------------------------------------------ 桌面观测
    def screenshot(self, path: str | Path, window: Any | None = None) -> Path:
        """截取整个虚拟屏幕，或指定 ``window`` 在屏幕上的矩形区域（不会自动激活置顶该窗口）。"""
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
        """获取所有可见顶层应用窗口列表（类似 Alt-Tab 视图，按最顶层优先排序）。"""
        return [self.info(h) for h in _win32.enum_windows() if _win32.is_app_window(h)]

    def foreground(self) -> WindowInfo | None:
        h = _win32.foreground_window()
        return self.info(h) if h else None

    def modal_dialogs(self) -> list[WindowInfo]:
        """检测可能阻塞自动化流程的顶层对话框或模态弹窗。

        识别依据：标准对话框类名 ``#32770``；带标题栏或对话框边框的从属窗口
        （所有者窗口被禁用时记为 ``owned_modal``，否则记为 ``owned_popup``）；
        UIA ``WindowPattern.IsModal`` 属性；以及前台窗口内部嵌入的 UIA ``Window`` 子控件
        （例如现代应用中的 XAML ContentDialog，记为 ``embedded``）。
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

    # ------------------------------------------------------------------ 窗口与应用控制
    def launch(self, command: str, args: list[str] | None = None, cwd: str | None = None) -> int:
        """启动程序（或打开文档 / 启动 ``excel`` 等 App Paths 注册应用），返回进程 PID（未知时返回 0）。

        注意：部分应用（如 Windows 11 记事本、Office）启动后会将窗口移交给其他进程，
        因此后续查找窗口时建议按标题或进程名匹配，而非依赖此处返回的 PID。
        """
        args = list(args or [])
        try:
            proc = subprocess.Popen(  # noqa: S603
                [command, *args], cwd=cwd, close_fds=True,
                creationflags=0x00000200,  # 进程创建标志 CREATE_NEW_PROCESS_GROUP
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
        """返回匹配所有给定条件的可见顶层窗口句柄列表（当前前台窗口优先排在首位）。

        ``title`` 为精确匹配；``title_re`` 使用 ``re.search`` 正则匹配；``process`` 可为进程可执行文件名
        （忽略大小写，``.exe`` 后缀可选）或字符串/整数形式的 PID。
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
                continue  # 仅按进程搜索时：跳过无标题的辅助隐藏窗口
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
        """请求关闭指定窗口（通过 WindowPattern.Close 或发送 WM_CLOSE 消息），不会自动处理未保存提示框。"""
        hwnd = self.hwnd_of(window)
        try:
            self.wrapper_of(hwnd).close()
        except Exception:  # noqa: BLE001
            _win32.post_close(hwnd)

    # ------------------------------------------------------------------ 键鼠输入
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

    # ------------------------------------------------------------------ 便捷封装方法
    def wait_window_gone(self, window: Any = None, timeout: float = 10, **criteria: Any) -> bool:
        from cuaflow.platform.windows.apps import wait_window_gone

        return wait_window_gone(self, window, timeout=timeout, **criteria)

    def dismiss_common_dialogs(self, **kwargs: Any) -> list[str]:
        from cuaflow.platform.windows.apps import dismiss_common_dialogs

        return dismiss_common_dialogs(self, **kwargs)

    def excel_count_rows(self, path: str | Path, sheet: str | None = None) -> int:
        from cuaflow.platform.windows.apps import excel_count_rows

        return excel_count_rows(path, sheet)
