"""Windows 原生桌面后端。可在任意操作系统上安全导入；实例化 ``WindowsDesktop`` 则需要 Windows 环境。

可选依赖（``pip install 'cuaflow[win]'``）：pywinauto、uiautomation、pyautogui、pywin32。
若已安装 ``psutil``，将优先用于解析进程名称。
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
