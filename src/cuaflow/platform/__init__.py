"""cuaflow 平台抽象层：命令行 Shell、桌面（Windows UIA / 内存模拟）以及持久化浏览器后端。

重量级或可选依赖（Playwright、pywinauto、uiautomation、mss）均按需延迟导入，
因此在任意操作系统上执行 ``import cuaflow.platform`` 都轻量且安全。
"""

from cuaflow.platform.base import (
    IS_WINDOWS,
    BrowserBackend,
    DesktopBackend,
    FakeDesktop,
    FakeWindow,
    ShellBackend,
    ShellResult,
    WindowInfo,
    default_desktop,
)
from cuaflow.platform.browser import (
    BrowserError,
    BrowserLaunchError,
    BrowserNotRunning,
    PersistentBrowser,
    default_headless,
)
from cuaflow.platform.markers import (
    ask_user_marker,
    find_ask_user,
    find_image_markers,
    image_marker,
)
from cuaflow.platform.paths import browser_profile_dir, browser_session_file, data_dir, screenshots_dir
from cuaflow.platform.shell import DEFAULT_BLOCKED_PATTERNS, LocalShell, check_command_safety

__all__ = [
    "IS_WINDOWS",
    "BrowserBackend",
    "BrowserError",
    "BrowserLaunchError",
    "BrowserNotRunning",
    "DEFAULT_BLOCKED_PATTERNS",
    "DesktopBackend",
    "FakeDesktop",
    "FakeWindow",
    "LocalShell",
    "PersistentBrowser",
    "ShellBackend",
    "ShellResult",
    "WindowInfo",
    "ask_user_marker",
    "browser_profile_dir",
    "browser_session_file",
    "check_command_safety",
    "data_dir",
    "default_desktop",
    "default_headless",
    "find_ask_user",
    "find_image_markers",
    "image_marker",
    "screenshots_dir",
]
