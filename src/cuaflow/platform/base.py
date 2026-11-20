"""Platform abstraction: shell, desktop, and browser backends.

Flow steps receive these through ``Ctx`` (``c.shell``, ``c.desktop``, ``c.browser``).
Agents (Author / Repairer) reach the same functionality through the
``cuaflow-tool`` terminal CLI (see :mod:`cuaflow.platform.cli_tools`).

Concrete implementations:

* :mod:`cuaflow.platform.shell` – ``LocalShell`` (PowerShell on Windows, bash elsewhere)
* :mod:`cuaflow.platform.windows` – ``WindowsDesktop`` (UIA / pywinauto / mss)
* :mod:`cuaflow.platform.browser` – ``PersistentBrowser`` (detached Chromium/Edge + CDP)
* :class:`FakeDesktop` (below) – in-memory backend for tests and non-Windows hosts
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
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom
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
    """Native desktop control. All coordinates are physical screen pixels."""

    name: str

    # --- observation ---------------------------------------------------
    def screenshot(self, path: str | Path, window: Any | None = None) -> Path: ...
    def ui_tree(self, window: Any | None = None, max_depth: int = 8, max_nodes: int = 400) -> str: ...
    def windows(self) -> list[WindowInfo]: ...
    def foreground(self) -> WindowInfo | None: ...
    def modal_dialogs(self) -> list[WindowInfo]: ...

    # --- window / app control -----------------------------------------
    def launch(self, command: str, args: list[str] | None = None, cwd: str | None = None) -> int: ...
    def window(
        self,
        title: str | None = None,
        title_re: str | None = None,
        process: str | None = None,
        class_name: str | None = None,
        timeout: float = 10,
    ) -> Any:
        """Return a ready window object (pywinauto ``WindowSpecification`` with UIA backend on
        Windows). Raises ``TimeoutError`` if no matching window appears within ``timeout``."""
        ...

    def activate(self, window: Any) -> None: ...
    def close_window(self, window: Any) -> None: ...

    # --- input -----------------------------------------------------------
    def type_text(self, text: str, interval: float = 0.0) -> None: ...
    def type_keys(self, keys: str) -> None:
        """pywinauto key syntax, e.g. ``"^s"``, ``"{ENTER}"``, ``"%{F4}"``."""
        ...

    def hotkey(self, *keys: str) -> None: ...
    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None: ...
    def scroll(self, x: int, y: int, clicks: int) -> None: ...


@runtime_checkable
class BrowserBackend(Protocol):
    """Persistent browser. ``page()`` returns a Playwright *sync* ``Page``."""

    def page(self) -> Any: ...
    def new_page(self, url: str | None = None) -> Any: ...
    def pages(self) -> list[Any]: ...
    def context(self) -> Any: ...
    def aria_snapshot(self, page: Any | None = None, max_chars: int = 12000) -> str: ...
    def screenshot(self, path: str | Path, page: Any | None = None, full_page: bool = False) -> Path: ...
    def is_connected(self) -> bool: ...
    def disconnect(self) -> None:
        """Drop the CDP connection but keep the browser process alive."""
        ...

    def shutdown(self) -> None:
        """Terminate the browser process."""
        ...


# ---------------------------------------------------------------------------
# Fake desktop for tests / non-Windows hosts
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
    """Scriptable in-memory desktop. Tests add/remove windows and inspect ``events``."""

    name = "fake"

    def __init__(self) -> None:
        self._windows: list[WindowInfo] = []
        self.events: list[tuple[Any, ...]] = []
        self.clipboard = ""

    # helpers for tests
    def add_window(self, title: str, process: str = "app.exe", modal: bool = False) -> WindowInfo:
        info = WindowInfo(title=title, process=process, pid=1000 + len(self._windows), is_modal=modal)
        self._windows.append(info)
        return info

    def remove_window(self, title: str) -> None:
        self._windows = [w for w in self._windows if w.title != title]

    # protocol
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
    """Return the native desktop backend for this host (Fake on non-Windows)."""
    if IS_WINDOWS:
        from cuaflow.platform.windows import WindowsDesktop

        return WindowsDesktop()
    return FakeDesktop()
