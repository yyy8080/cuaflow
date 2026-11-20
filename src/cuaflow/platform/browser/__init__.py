"""持久化浏览器后端（基于 CDP 驱动的分离式 Chromium / Edge 进程）。"""

from cuaflow.platform.browser.session import (
    BrowserError,
    BrowserLaunchError,
    BrowserNotRunning,
    PersistentBrowser,
    cdp_version,
    default_headless,
    find_channel_executable,
    launch_browser,
    read_session,
    session_alive,
    truncate_text,
    write_session,
)

__all__ = [
    "BrowserError",
    "BrowserLaunchError",
    "BrowserNotRunning",
    "PersistentBrowser",
    "cdp_version",
    "default_headless",
    "find_channel_executable",
    "launch_browser",
    "read_session",
    "session_alive",
    "truncate_text",
    "write_session",
]
