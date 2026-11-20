"""cuaflow platform layer: shell, desktop (Windows UIA / fake) and persistent browser backends.

Heavy/optional dependencies (Playwright, pywinauto, uiautomation, mss) are imported lazily,
so ``import cuaflow.platform`` is cheap and safe on every OS.
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
