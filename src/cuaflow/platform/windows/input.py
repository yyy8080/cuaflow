"""键盘与鼠标输入封装：优先使用 pywinauto（基于 SendInput，原生支持 Unicode），不可用时回退至 pyautogui。

所有坐标均为物理屏幕像素（调用前会自动启用进程级每显示器 DPI 感知）。
"""

from __future__ import annotations

from cuaflow.platform.windows import _win32

_SPECIAL = set("+^%~(){}[]")

# 常用按键别名 -> pywinauto 虚拟键名映射
_KEYMAP = {
    "ctrl": "VK_CONTROL", "control": "VK_CONTROL", "alt": "VK_MENU", "menu": "VK_MENU",
    "shift": "VK_SHIFT", "win": "VK_LWIN", "windows": "VK_LWIN", "cmd": "VK_LWIN", "super": "VK_LWIN",
    "enter": "VK_RETURN", "return": "VK_RETURN", "esc": "VK_ESCAPE", "escape": "VK_ESCAPE",
    "tab": "VK_TAB", "space": "VK_SPACE", "backspace": "VK_BACK", "delete": "VK_DELETE", "del": "VK_DELETE",
    "insert": "VK_INSERT", "home": "VK_HOME", "end": "VK_END", "pageup": "VK_PRIOR", "pgup": "VK_PRIOR",
    "pagedown": "VK_NEXT", "pgdn": "VK_NEXT", "up": "VK_UP", "down": "VK_DOWN", "left": "VK_LEFT",
    "right": "VK_RIGHT", "capslock": "VK_CAPITAL", "printscreen": "VK_SNAPSHOT", "apps": "VK_APPS",
    **{f"f{i}": f"VK_F{i}" for i in range(1, 25)},
}


def escape_send_keys(text: str) -> str:
    """转义 pywinauto ``send_keys`` 的特殊元字符，确保 ``text`` 按原样字面输入。"""
    return "".join("{" + ch + "}" if ch in _SPECIAL else ch for ch in text)


def hotkey_to_send_keys(*keys: str) -> str:
    """将组合键序列如 ``("ctrl", "shift", "s")`` 转换为 pywinauto 按下/抬起序列 ``"{VK_CONTROL down}{VK_SHIFT down}{s down}{s up}{VK_SHIFT up}{VK_CONTROL up}"``。"""
    names: list[str] = []
    for k in keys:
        low = k.strip().lower()
        if low in _KEYMAP:
            names.append(_KEYMAP[low])
        elif len(k) == 1:
            names.append(k.lower() if k.isalpha() else k)
        elif low.startswith("vk_"):
            names.append(low.upper())
        else:
            raise ValueError(f"unknown key name: {k!r}")
    down = "".join("{%s down}" % n for n in names)
    up = "".join("{%s up}" % n for n in reversed(names))
    return down + up


def type_text(text: str, interval: float = 0.0) -> None:
    """向当前焦点控件按字面输入文本 ``text``（支持包括中日韩字符在内的 Unicode）。"""
    _win32.ensure_dpi_awareness()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        from pywinauto import keyboard  # type: ignore

        keyboard.send_keys(
            escape_send_keys(text), pause=interval, with_spaces=True, with_tabs=True, with_newlines=True
        )
        return
    except ImportError:
        pass
    import pyautogui  # type: ignore

    if not text.isascii():
        raise RuntimeError("non-ASCII typing requires pywinauto (pip install 'cuaflow[win]')")
    pyautogui.write(text, interval=interval)


def send_keys(keys: str) -> None:
    """按 pywinauto 按键语法发送按键，例如 ``"^s"``、``"{ENTER}"``、``"%{F4}"``。"""
    _win32.ensure_dpi_awareness()
    from pywinauto import keyboard  # type: ignore

    keyboard.send_keys(keys)


def hotkey(*keys: str) -> None:
    _win32.ensure_dpi_awareness()
    try:
        from pywinauto import keyboard  # type: ignore

        keyboard.send_keys(hotkey_to_send_keys(*keys))
        return
    except ImportError:
        pass
    import pyautogui  # type: ignore

    pyautogui.hotkey(*keys)


def click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    _win32.ensure_dpi_awareness()
    try:
        from pywinauto import mouse  # type: ignore

        if double:
            mouse.double_click(button=button, coords=(int(x), int(y)))
        else:
            mouse.click(button=button, coords=(int(x), int(y)))
        return
    except ImportError:
        pass
    import pyautogui  # type: ignore

    pyautogui.click(x=int(x), y=int(y), button=button, clicks=2 if double else 1)


def scroll(x: int, y: int, clicks: int) -> None:
    """在指定位置滚动鼠标滚轮：``clicks`` 为正数时向上滚动，为负数时向下滚动。"""
    _win32.ensure_dpi_awareness()
    try:
        from pywinauto import mouse  # type: ignore

        mouse.scroll(coords=(int(x), int(y)), wheel_dist=int(clicks))
        return
    except ImportError:
        pass
    import pyautogui  # type: ignore

    pyautogui.scroll(int(clicks), x=int(x), y=int(y))
