"""App-level helpers: waiting for windows to disappear, dismissing common dialogs, Excel row counts.

These accept any ``DesktopBackend``; the dialog-clicking part needs ``WindowsDesktop``
(on ``FakeDesktop`` modal windows are simply removed so the logic is testable).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from cuaflow.platform.base import IS_WINDOWS, DesktopBackend, FakeDesktop, WindowInfo

#: Button names tried by :func:`dismiss_common_dialogs`, in priority order.
#: "Don't save / No" first (never persist unexpected changes), then cancel/close, then OK.
DEFAULT_DISMISS_BUTTONS: list[str] = [
    "不保存", "否", "Don't Save", "Don't save", "No",
    "取消", "Cancel",
    "关闭", "Close",
    "确定", "OK", "好",
]


def normalize_button_name(name: str) -> str:
    """Strip accelerators (``&``, ``(N)``, ``(&N)``), unify quotes, lower-case."""
    s = (name or "").replace("&", "").replace("\u2019", "'").replace("\u2018", "'")
    s = re.sub(r"\s*[\(（]\s*\w\s*[\)）]\s*$", "", s)
    return s.strip().rstrip(".…").strip().lower()


def _matches(info: WindowInfo, title: str | None, title_re: str | None, process: str | None) -> bool:
    if title is not None and info.title != title:
        return False
    if title_re is not None and not re.search(title_re, info.title):
        return False
    if process is not None and info.process.lower() not in (process.lower(), process.lower() + ".exe"):
        return False
    return True


def wait_window_gone(
    desktop: DesktopBackend,
    window: Any = None,
    timeout: float = 10,
    title: str | None = None,
    title_re: str | None = None,
    process: str | None = None,
    poll: float = 0.2,
) -> bool:
    """Wait until ``window`` (a window object / ``WindowInfo`` / handle) or every window matching
    the criteria is gone. Returns True if gone, False on timeout."""
    if window is None and title is None and title_re is None and process is None:
        raise ValueError("wait_window_gone(): give a window or title/title_re/process criteria")
    deadline = time.monotonic() + timeout

    def present() -> bool:
        if window is not None:
            if hasattr(window, "exists") and not isinstance(window, WindowInfo):
                try:
                    return bool(window.exists(timeout=0))
                except TypeError:
                    return bool(window.exists())
                except Exception:  # noqa: BLE001
                    return False
            handle = window.handle if isinstance(window, WindowInfo) else window
            if isinstance(handle, int):
                if IS_WINDOWS and handle:
                    from cuaflow.platform.windows import _win32

                    return _win32.is_window(handle) and _win32.is_visible(handle)
                return any(w.handle == handle for w in desktop.windows())
            if isinstance(window, WindowInfo):
                return any(w.title == window.title and w.pid == window.pid for w in desktop.windows())
            return False
        return any(_matches(w, title, title_re, process) for w in desktop.windows())

    while True:
        if not present():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def _click_button_in(desktop: Any, dialog: WindowInfo, names: list[str]) -> str | None:
    """Click the highest-priority button of ``names`` inside ``dialog`` (Windows). Returns its name."""
    wanted = [normalize_button_name(n) for n in names]
    buttons: list[tuple[str, Any]] = []
    if dialog.extra.get("embedded"):
        import uiautomation as auto  # type: ignore

        with auto.UIAutomationInitializerInThread():
            root = auto.ControlFromHandle(dialog.handle)
            dlg = root.WindowControl(searchDepth=3, Name=dialog.title) if root else None
            if dlg is None or not dlg.Exists(0, 0):
                return None
            stack = [(dlg, 0)]
            while stack:
                node, depth = stack.pop()
                for ch in node.GetChildren():
                    if ch.ControlTypeName == "ButtonControl":
                        buttons.append((ch.Name or "", ch))
                    elif depth < 6:
                        stack.append((ch, depth + 1))
            for want in wanted:
                for name, ctrl in buttons:
                    if normalize_button_name(name) == want:
                        try:
                            ctrl.GetInvokePattern().Invoke()
                        except Exception:  # noqa: BLE001
                            ctrl.Click(simulateMove=False)
                        return name
        return None

    wrapper = desktop.wrapper_of(dialog.handle)
    try:
        candidates = wrapper.descendants(control_type="Button")
    except Exception:  # noqa: BLE001
        candidates = []
    for c in candidates:
        try:
            buttons.append((c.window_text() or c.element_info.name or "", c))
        except Exception:  # noqa: BLE001
            continue
    for want in wanted:
        for name, ctrl in buttons:
            if normalize_button_name(name) == want:
                try:
                    ctrl.invoke()
                except Exception:  # noqa: BLE001
                    ctrl.click_input()
                return name
    return None


def dismiss_common_dialogs(
    desktop: DesktopBackend,
    names: list[str] | None = None,
    rounds: int = 3,
    settle: float = 0.5,
) -> list[str]:
    """Best-effort: click a dismissal button on each open modal dialog (repeated up to ``rounds``
    times for cascades). Returns descriptions like ``"'记事本' -> clicked '不保存'"``."""
    names = list(names or DEFAULT_DISMISS_BUTTONS)
    done: list[str] = []
    for _ in range(max(rounds, 1)):
        dialogs = desktop.modal_dialogs()
        if not dialogs:
            break
        clicked_any = False
        for dlg in dialogs:
            if isinstance(desktop, FakeDesktop):
                desktop.remove_window(dlg.title)
                desktop.events.append(("dismiss", dlg.title))
                done.append(f"{dlg.title!r} -> closed")
                clicked_any = True
                continue
            try:
                name = _click_button_in(desktop, dlg, names)
            except Exception as exc:  # noqa: BLE001
                done.append(f"{dlg.title!r} -> error: {type(exc).__name__}: {exc}")
                continue
            if name:
                done.append(f"{dlg.title!r} -> clicked {name!r}")
                clicked_any = True
        if not clicked_any:
            break
        time.sleep(settle)
    return done


def excel_count_rows(path: str | Path, sheet: str | None = None) -> int:
    """Number of non-empty rows (header included) in an ``.xlsx/.xlsm`` sheet (openpyxl) or ``.csv``."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() in (".csv", ".tsv"):
        import csv

        delim = "\t" if path.suffix.lower() == ".tsv" else ","
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            return sum(1 for row in csv.reader(fh, delimiter=delim) if any(c.strip() for c in row))
    try:
        import openpyxl  # type: ignore
    except ImportError as exc:
        raise RuntimeError("excel_count_rows needs openpyxl (pip install openpyxl)") from exc
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.active
        return sum(
            1 for row in ws.iter_rows(values_only=True)
            if any(v is not None and str(v).strip() != "" for v in row)
        )
    finally:
        wb.close()
