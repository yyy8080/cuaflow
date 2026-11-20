"""基于 ``mss`` 的屏幕截图工具（支持全虚拟屏幕、指定显示器、矩形区域或单个窗口）。"""

from __future__ import annotations

from pathlib import Path

from cuaflow.platform.windows import _win32


def _save(shot, path: Path) -> Path:  # noqa: ANN001
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".png":
        import mss.tools

        mss.tools.to_png(shot.rgb, shot.size, output=str(path))
    else:
        from PIL import Image

        Image.frombytes("RGB", shot.size, shot.rgb).save(path)
    return path.resolve()


def grab(
    path: str | Path,
    region: tuple[int, int, int, int] | None = None,
    monitor: int = 0,
) -> Path:
    """截取物理像素矩形区域 ``region``（左、上、右、下）或指定显示器 ``monitor``
    （0 表示整个虚拟屏幕，1 表示主显示器，2 及以上表示其他显示器），并保存至 ``path``。"""
    import mss

    _win32.ensure_dpi_awareness()
    with mss.mss() as sct:
        monitors = sct.monitors
        if region is not None:
            virt = monitors[0]
            l, t, r, b = region
            l, t = max(l, virt["left"]), max(t, virt["top"])
            r = min(r, virt["left"] + virt["width"])
            b = min(b, virt["top"] + virt["height"])
            if r - l < 1 or b - t < 1:
                raise ValueError(f"region {region} is outside the visible screen")
            box = {"left": l, "top": t, "width": r - l, "height": b - t}
        else:
            if monitor < 0 or monitor >= len(monitors):
                raise ValueError(f"monitor {monitor} out of range (0..{len(monitors) - 1})")
            box = monitors[monitor]
        shot = sct.grab(box)
    return _save(shot, Path(path))


def grab_window(path: str | Path, hwnd: int) -> Path:
    """截取窗口 ``hwnd`` 在屏幕上的矩形区域（窗口需处于可见且未被遮挡状态）。"""
    return grab(path, region=_win32.window_rect(hwnd))
