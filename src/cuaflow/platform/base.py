"""平台抽象层：命令行 Shell、原生桌面与持久化浏览器后端协议定义。

工作流步骤通过上下文 ``Ctx``（``c.shell``、``c.desktop``、``c.browser``）访问这些能力；
智能体（Author / Repairer）则通过终端 CLI 工具 ``cuaflow-tool`` 调用相同功能
（参见 :mod:`cuaflow.platform.cli_tools`）。

具体实现：

* :mod:`cuaflow.platform.shell` —— ``LocalShell``（Windows 下默认使用 PowerShell，其他平台使用 bash）
* :mod:`cuaflow.platform.windows` —— ``WindowsDesktop``（基于 UIA / pywinauto / mss）
* :mod:`cuaflow.platform.browser` —— ``PersistentBrowser``（分离式 Chromium/Edge 进程 + CDP）
* :class:`FakeDesktop`（见下文）—— 面向测试及非 Windows 主机的内存模拟桌面后端
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

IS_WINDOWS = sys.platform == "win32"


@dataclass
class ShellResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def text(self, limit: int = 12000) -> str:
        out = f"exit_code={self.exit_code}{' (timed out)' if self.timed_out else ''}\n"
        if self.stdout:
            out += f"stdout:\n{self.stdout}\n"
        if self.stderr:
            out += f"stderr:\n{self.stderr}\n"
        if len(out) > limit:
            head = int(limit * 0.75)
            out = out[:head] + "\n[... output truncated ...]\n" + out[-(limit - head) :]
        return out


@dataclass
class WindowInfo:
    title: str
    process: str = ""
    pid: int = 0
    handle: int = 0
    class_name: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # 窗口矩形坐标（左、上、右、下）
    is_modal: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "process": self.process,
            "pid": self.pid,
            "handle": self.handle,
            "class_name": self.class_name,
            "rect": list(self.rect),
            "is_modal": self.is_modal,
        }


@runtime_checkable
class ShellBackend(Protocol):
    def run(
        self,
        command: str,
        timeout: float = 120,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ShellResult: ...


@runtime_checkable
class DesktopBackend(Protocol):
    """原生桌面控制协议。所有坐标均采用物理屏幕像素。"""

    name: str

    # --- 桌面观测 -------------------------------------------------------
    def screenshot(self, path: str | Path, window: Any | None = None) -> Path: ...
    def ui_tree(self, window: Any | None = None, max_depth: int = 8, max_nodes: int = 400) -> str: ...
    def windows(self) -> list[WindowInfo]: ...
    def foreground(self) -> WindowInfo | None: ...
    def modal_dialogs(self) -> list[WindowInfo]: ...

    # --- 窗口与应用控制 -------------------------------------------------
    def launch(self, command: str, args: list[str] | None = None, cwd: str | None = None) -> int: ...
    def window(
        self,
        title: str | None = None,
        title_re: str | None = None,
        process: str | None = None,
        class_name: str | None = None,
        timeout: float = 10,
    ) -> Any:
        """返回就绪的窗口对象（Windows 下为基于 UIA 后端的 pywinauto ``WindowSpecification``）。
        若在 ``timeout`` 秒内未出现匹配窗口，则抛出 ``TimeoutError``。"""
        ...

    def activate(self, window: Any) -> None: ...
    def close_window(self, window: Any) -> None: ...

    # --- 键鼠输入 -------------------------------------------------------
    def type_text(self, text: str, interval: float = 0.0) -> None: ...
    def type_keys(self, keys: str) -> None:
        """遵循 pywinauto 按键语法，例如 ``"^s"``、``"{ENTER}"``、``"%{F4}"``。"""
        ...

    def hotkey(self, *keys: str) -> None: ...
    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None: ...
    def scroll(self, x: int, y: int, clicks: int) -> None: ...


@runtime_checkable
class BrowserBackend(Protocol):
    """持久化浏览器协议。``page()`` 返回 Playwright 同步 API 的 ``Page`` 对象。"""

    def page(self) -> Any: ...
    def new_page(self, url: str | None = None) -> Any: ...
    def pages(self) -> list[Any]: ...
    def context(self) -> Any: ...
    def aria_snapshot(self, page: Any | None = None, max_chars: int = 12000) -> str: ...
    def screenshot(self, path: str | Path, page: Any | None = None, full_page: bool = False) -> Path: ...
    def is_connected(self) -> bool: ...
    def disconnect(self) -> None:
        """断开当前 CDP 连接，但保持后台浏览器进程继续运行。"""
        ...

    def shutdown(self) -> None:
        """彻底终止浏览器进程。"""
        ...


# ---------------------------------------------------------------------------
# 面向测试与非 Windows 主机的模拟桌面后端
# ---------------------------------------------------------------------------


class FakeWindow:
    def __init__(self, desktop: "FakeDesktop", info: WindowInfo):
        self._desktop = desktop
        self.info = info
        self.texts: dict[str, str] = {}
        self.typed: list[str] = []

    def window_text(self) -> str:
        return self.info.title

    def type_keys(self, keys: str, **_: Any) -> "FakeWindow":
        self.typed.append(keys)
        self._desktop.events.append(("type_keys", self.info.title, keys))
        return self

    def set_text(self, text: str) -> "FakeWindow":
        self.texts["text"] = text
        return self

    def child(self, **criteria: Any) -> "FakeWindow":
        key = ",".join(f"{k}={v}" for k, v in sorted(criteria.items()))
        return FakeWindow(self._desktop, WindowInfo(title=key))

    def exists(self, timeout: float = 0) -> bool:  # noqa: ARG002
        return self.info in self._desktop._windows

    def close(self) -> None:
        self._desktop.remove_window(self.info.title)


class FakeDesktop:
    """可编程的内存模拟桌面。测试中可增删窗口并检查 ``events`` 事件记录。"""

    name = "fake"

    def __init__(self) -> None:
        self._windows: list[WindowInfo] = []
        self.events: list[tuple[Any, ...]] = []
        self.clipboard = ""

    # 测试辅助方法
    def add_window(self, title: str, process: str = "app.exe", modal: bool = False) -> WindowInfo:
        info = WindowInfo(title=title, process=process, pid=1000 + len(self._windows), is_modal=modal)
        self._windows.append(info)
        return info

    def remove_window(self, title: str) -> None:
        self._windows = [w for w in self._windows if w.title != title]

    # DesktopBackend 协议实现
    def screenshot(self, path: str | Path, window: Any | None = None) -> Path:  # noqa: ARG002
        from PIL import Image, ImageDraw

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (640, 400), "white")
        draw = ImageDraw.Draw(img)
        for i, w in enumerate(self._windows):
            draw.rectangle([10, 10 + i * 40, 630, 45 + i * 40], outline="black")
            draw.text((16, 20 + i * 40), f"{w.title} ({w.process})", fill="black")
        img.save(path)
        return path

    def ui_tree(self, window: Any | None = None, max_depth: int = 8, max_nodes: int = 400) -> str:  # noqa: ARG002
        lines = ["Desktop"]
        for w in self._windows:
            lines.append(f"  Window name={w.title!r} process={w.process}{' modal' if w.is_modal else ''}")
        return "\n".join(lines)

    def windows(self) -> list[WindowInfo]:
        return list(self._windows)

    def foreground(self) -> WindowInfo | None:
        return self._windows[-1] if self._windows else None

    def modal_dialogs(self) -> list[WindowInfo]:
        return [w for w in self._windows if w.is_modal]

    def launch(self, command: str, args: list[str] | None = None, cwd: str | None = None) -> int:  # noqa: ARG002
        self.events.append(("launch", command, tuple(args or ())))
        info = self.add_window(Path(command).stem, process=Path(command).name)
        return info.pid

    def window(self, title=None, title_re=None, process=None, class_name=None, timeout: float = 10):  # noqa: ARG002
        import re

        for w in reversed(self._windows):
            if title is not None and w.title != title:
                continue
            if title_re is not None and not re.match(title_re, w.title):
                continue
            if process is not None and w.process.lower() != process.lower():
                continue
            return FakeWindow(self, w)
        raise TimeoutError(f"window not found: title={title!r} title_re={title_re!r} process={process!r}")

    def activate(self, window: Any) -> None:
        self.events.append(("activate", getattr(getattr(window, "info", None), "title", window)))

    def close_window(self, window: Any) -> None:
        window.close()

    def type_text(self, text: str, interval: float = 0.0) -> None:  # noqa: ARG002
        self.events.append(("type_text", text))

    def type_keys(self, keys: str) -> None:
        self.events.append(("type_keys", None, keys))

    def hotkey(self, *keys: str) -> None:
        self.events.append(("hotkey", keys))

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        self.events.append(("click", x, y, button, double))

    def scroll(self, x: int, y: int, clicks: int) -> None:
        self.events.append(("scroll", x, y, clicks))


def default_desktop() -> DesktopBackend:
    """返回当前主机的默认桌面后端（非 Windows 平台返回 FakeDesktop）。"""
    if IS_WINDOWS:
        from cuaflow.platform.windows import WindowsDesktop

        return WindowsDesktop()
    return FakeDesktop()
