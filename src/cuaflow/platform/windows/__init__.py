"""Windows desktop backend. Importable on any OS; constructing ``WindowsDesktop`` requires Windows.

Optional dependencies (``pip install 'cuaflow[win]'``): pywinauto, uiautomation, pyautogui, pywin32.
``psutil`` is used for process names when present.
"""

from cuaflow.platform.windows.apps import (
    DEFAULT_DISMISS_BUTTONS,
    dismiss_common_dialogs,
    excel_count_rows,
    normalize_button_name,
    wait_window_gone,
)
from cuaflow.platform.windows.desktop import DesktopError, WindowsDesktop, pywinauto_errors

__all__ = [
    "DEFAULT_DISMISS_BUTTONS",
    "DesktopError",
    "WindowsDesktop",
    "dismiss_common_dialogs",
    "excel_count_rows",
    "normalize_button_name",
    "pywinauto_errors",
    "wait_window_gone",
]
