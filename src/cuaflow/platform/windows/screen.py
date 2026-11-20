"""Screenshots with ``mss`` (full virtual screen, a monitor, a region or a window)."""

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
    """Capture ``region`` (left, top, right, bottom in physical pixels) or ``monitor``
    (0 = whole virtual screen, 1 = primary, 2.. = others) to ``path``."""
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
    """Capture the on-screen area of window ``hwnd`` (it should be visible / unobscured)."""
    return grab(path, region=_win32.window_rect(hwnd))
