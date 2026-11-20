"""步骤执行上下文，作为参数 ``c`` 传递给每个步骤函数。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from cuaflow.flow.flow import Params
from cuaflow.platform.base import BrowserBackend, DesktopBackend, ShellBackend


def default_browser_factory() -> BrowserBackend:
    from cuaflow.platform.browser import PersistentBrowser

    return PersistentBrowser.connect()


def default_browser_probe() -> BrowserBackend | None:
    from cuaflow.platform.browser import BrowserNotRunning, PersistentBrowser

    st = PersistentBrowser.status()
    if not st.get("alive"):
        return None
    try:
        return PersistentBrowser.connect(launch_if_missing=False)
    except BrowserNotRunning:
        return None


def default_desktop_factory() -> DesktopBackend:
    from cuaflow.platform.base import default_desktop

    return default_desktop()


def default_shell_factory() -> ShellBackend:
    from cuaflow.platform.shell import LocalShell

    return LocalShell()


class Ctx:
    def __init__(
        self,
        params: dict[str, Any],
        run_dir: Path,
        state: dict[str, Any] | None = None,
        browser_factory: Callable[[], BrowserBackend] = default_browser_factory,
        desktop_factory: Callable[[], DesktopBackend] = default_desktop_factory,
        shell_factory: Callable[[], ShellBackend] = default_shell_factory,
        browser_probe: Callable[[], BrowserBackend | None] | None = default_browser_probe,
    ):
        self.params = Params(dict(params))
        self.state: dict[str, Any] = dict(state or {})
        self.output: dict[str, Any] = {}
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.shots_dir = self.run_dir / "screenshots"
        self._browser_factory, self._desktop_factory, self._shell_factory = (
            browser_factory, desktop_factory, shell_factory)
        self._browser_probe = browser_probe
        self._browser: BrowserBackend | None = None
        self._desktop: DesktopBackend | None = None
        self._shell: ShellBackend | None = None
        self.current_step: str | None = None
        self.surfaces_used: set[str] = set()
        self.last_surface: str | None = None
        self._log_path = self.run_dir / "flow_log.txt"
        self._snap_seq = 0

    # --- 惰性连接的后端 -------------------------------------------------
    @property
    def browser(self) -> BrowserBackend:
        if self._browser is None or not self._browser.is_connected():
            self._browser = self._browser_factory()
        self.surfaces_used.add("browser")
        self.last_surface = "browser"
        return self._browser

    @property
    def desktop(self) -> DesktopBackend:
        if self._desktop is None:
            self._desktop = self._desktop_factory()
        self.surfaces_used.add("desktop")
        self.last_surface = "desktop"
        return self._desktop

    @property
    def shell(self) -> ShellBackend:
        if self._shell is None:
            self._shell = self._shell_factory()
        return self._shell

    def has_browser(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    def attach_live_browser(self) -> BrowserBackend | None:
        """若已有运行中的浏览器会话则直接挂载连接（绝不主动启动新浏览器）。"""
        if self.has_browser():
            return self._browser
        if self._browser_probe is None:
            return None
        try:
            br = self._browser_probe()
            if br is not None:
                self._browser = br
        except Exception:  # noqa: BLE001
            pass
        return self._browser if self.has_browser() else None

    def peek_desktop(self) -> DesktopBackend:
        """获取桌面后端，但不将其标记为当前步骤已使用。"""
        if self._desktop is None:
            self._desktop = self._desktop_factory()
        return self._desktop

    def peek_browser(self) -> BrowserBackend | None:
        return self._browser if self.has_browser() else None

    # --- 辅助方法 -------------------------------------------------------
    def log(self, *parts: Any) -> None:
        line = " ".join(str(p) for p in parts)
        stamp = time.strftime("%H:%M:%S")
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] [{self.current_step or '-'}] {line}\n")
        print(line)

    def snap(self, name: str = "snap", surface: str | None = None) -> Path:
        """对浏览器页面（若已使用）或桌面进行截图。"""
        self._snap_seq += 1
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        path = self.shots_dir / f"{self.current_step or 'adhoc'}_{self._snap_seq:03d}_{name}.png"
        surface = surface or self.last_surface or ("browser" if self.has_browser() else "desktop")
        if surface == "browser" and self.has_browser():
            return self._browser.screenshot(path)  # type: ignore[union-attr]
        return self.peek_desktop().screenshot(path)

    def state_json(self) -> dict[str, Any]:
        """返回 ``state`` 的 JSON 安全快照（不可序列化的值会转为字符串）。"""
        return json.loads(json.dumps(self.state, default=str, ensure_ascii=False))

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
