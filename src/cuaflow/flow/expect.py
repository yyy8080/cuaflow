"""确定性步骤断言（`Supervisor` L1 级校验）。

所有断言失败均抛出 :class:`ExpectationFailed`，运行器将其上报为 ``error_kind="expectation"``
（以区别于未预期异常对应的 ``"exception"``）。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable


class ExpectationFailed(AssertionError):
    pass


def _poll(check: Callable[[], bool], timeout: float, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if check():
                return True
        except Exception:  # noqa: BLE001 - 遇到瞬时异常时继续轮询
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def that(condition: Any, message: str = "condition is false") -> None:
    if not condition:
        raise ExpectationFailed(message)


def eventually(check: Callable[[], Any], message: str, timeout: float = 10) -> None:
    if not _poll(lambda: bool(check()), timeout):
        raise ExpectationFailed(f"{message} (not true within {timeout}s)")


def equals(actual: Any, expected: Any, message: str = "") -> None:
    if actual != expected:
        raise ExpectationFailed(f"{message + ': ' if message else ''}expected {expected!r}, got {actual!r}")


def contains(haystack: Any, needle: Any, message: str = "") -> None:
    if needle not in haystack:
        snippet = str(haystack)[:300]
        raise ExpectationFailed(f"{message + ': ' if message else ''}{needle!r} not found in {snippet!r}")


# --- 浏览器（Playwright 同步定位器 / 页面） ---------------------------------


def visible(locator: Any, timeout: float = 10) -> None:
    try:
        locator.first.wait_for(state="visible", timeout=timeout * 1000)
    except Exception as exc:  # noqa: BLE001
        raise ExpectationFailed(f"element not visible within {timeout}s: {locator} ({type(exc).__name__})") from None


def hidden(locator: Any, timeout: float = 10) -> None:
    try:
        locator.first.wait_for(state="hidden", timeout=timeout * 1000)
    except Exception:  # noqa: BLE001
        raise ExpectationFailed(f"element still visible after {timeout}s: {locator}") from None


def text(locator: Any, expected: str, exact: bool = False, timeout: float = 10) -> None:
    def ok() -> bool:
        actual = locator.first.inner_text(timeout=1000)
        return actual.strip() == expected if exact else expected in actual

    if not _poll(ok, timeout):
        try:
            actual = locator.first.inner_text(timeout=1000)
        except Exception:  # noqa: BLE001
            actual = "<unavailable>"
        raise ExpectationFailed(f"text {'==' if exact else 'contains'} {expected!r} failed; actual={actual[:300]!r}")


def count(locator: Any, minimum: int = 1, timeout: float = 10) -> int:
    n = 0

    def ok() -> bool:
        nonlocal n
        n = locator.count()
        return n >= minimum

    if not _poll(ok, timeout):
        raise ExpectationFailed(f"expected at least {minimum} matches for {locator}, found {n}")
    return n


def url(page: Any, pattern: str, timeout: float = 10) -> None:
    rx = re.compile(pattern)
    if not _poll(lambda: bool(rx.search(page.url)), timeout):
        raise ExpectationFailed(f"page url {page.url!r} does not match {pattern!r}")


def title(page: Any, pattern: str, timeout: float = 10) -> None:
    rx = re.compile(pattern)
    if not _poll(lambda: bool(rx.search(page.title())), timeout):
        raise ExpectationFailed(f"page title {page.title()!r} does not match {pattern!r}")


# --- 文件 -------------------------------------------------------------------


def file_exists(path: str | Path, timeout: float = 10, min_size: int = 1) -> Path:
    p = Path(path)
    if not _poll(lambda: p.is_file() and p.stat().st_size >= min_size, timeout):
        raise ExpectationFailed(f"file {p} does not exist (or smaller than {min_size} bytes) after {timeout}s")
    return p


def file_contains(path: str | Path, needle: str, encoding: str = "utf-8", timeout: float = 10) -> None:
    p = Path(path)
    if not _poll(lambda: needle in p.read_text(encoding=encoding, errors="replace"), timeout):
        raise ExpectationFailed(f"file {p} does not contain {needle!r}")


# --- 桌面 -------------------------------------------------------------------


def window_exists(desktop: Any, title_re: str, timeout: float = 10) -> Any:
    try:
        return desktop.window(title_re=title_re, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise ExpectationFailed(f"no window matching {title_re!r} within {timeout}s: {exc}") from None


def window_gone(desktop: Any, title_re: str, timeout: float = 10) -> None:
    rx = re.compile(title_re)
    if not _poll(lambda: not any(rx.search(w.title) for w in desktop.windows()), timeout):
        raise ExpectationFailed(f"window matching {title_re!r} still open after {timeout}s")


def no_modal(desktop: Any) -> None:
    dialogs = desktop.modal_dialogs()
    if dialogs:
        raise ExpectationFailed("unexpected modal dialog(s): " + ", ".join(repr(d.title) for d in dialogs))
