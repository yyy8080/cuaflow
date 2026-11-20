"""``cuaflow-tool``：供执行 Shell 命令的智能体（Author / Repairer）调用的终端工具集。

输出标记约定（由智能体循环解析，参见 :mod:`cuaflow.platform.markers`）：

* ``[[CUAFLOW_IMAGE:<abs path>]]``   —— 将指定图片附加到下一轮观测上下文中
* ``[[CUAFLOW_ASK_USER:<question>]]`` —— 向人类操作员发起提问

环境变量：``CUAFLOW_DATA_DIR``、``CUAFLOW_BROWSER_SESSION``（参见 :mod:`cuaflow.platform.paths`）。
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

import typer

from cuaflow.platform.base import IS_WINDOWS, DesktopBackend, default_desktop
from cuaflow.platform.markers import ask_user_marker, image_marker
from cuaflow.platform.paths import browser_session_file, screenshots_dir

app = typer.Typer(
    name="cuaflow-tool",
    help="cuaflow agent tools: screenshots, UI trees, persistent browser, ask-user.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

MAX_IMAGE_BYTES = 20 * 1024 * 1024


@app.callback()
def _main() -> None:
    if IS_WINDOWS:
        for stream in (sys.stdout, sys.stderr):
            reconf = getattr(stream, "reconfigure", None)
            if reconf:
                try:
                    reconf(encoding="utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# 后端工厂函数（测试中可通过 monkeypatch 替换）
# ---------------------------------------------------------------------------


def get_desktop() -> DesktopBackend:
    return default_desktop()


def get_browser(launch: bool = True) -> Any:
    from cuaflow.platform.browser import PersistentBrowser

    return PersistentBrowser.connect(browser_session_file(), launch_if_missing=launch)


def _fail(msg: str, code: int = 1) -> None:
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(code)


def _browser_or_exit(launch: bool) -> Any:
    from cuaflow.platform.browser import BrowserError

    try:
        return get_browser(launch=launch)
    except BrowserError as exc:
        hint = "" if launch else " (start one with: cuaflow-tool browser-open <url>)"
        _fail(f"{exc}{hint}", 2)


# ---------------------------------------------------------------------------
# 图像校验与截图命令
# ---------------------------------------------------------------------------


def validate_image(path: str | Path) -> Path:
    """校验并返回可读的 PNG/JPEG/GIF/WebP 图像绝对路径，若无效则抛出 ValueError。"""
    p = Path(path).expanduser()
    try:
        p = p.resolve(strict=True)
    except OSError:
        raise ValueError(f"image not found: {path}") from None
    if not p.is_file():
        raise ValueError(f"not a regular file: {p}")
    size = p.stat().st_size
    if size == 0:
        raise ValueError(f"empty file: {p}")
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes: {p}")
    head = p.read_bytes()[:16]
    if not (head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")
            or head.startswith((b"GIF87a", b"GIF89a")) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")):
        raise ValueError(f"unsupported or invalid image (expected PNG/JPEG/GIF/WebP): {p}")
    try:
        from PIL import Image

        with Image.open(p) as im:
            im.verify()
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"corrupt image {p}: {exc}") from None
    return p


def _default_shot_path(prefix: str) -> Path:
    return screenshots_dir() / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"


@app.command()
def screenshot(
    out: Optional[Path] = typer.Option(None, "--out", help="Output PNG path (default: <data_dir>/screenshots/...)."),
    window_title_re: Optional[str] = typer.Option(None, "--window-title-re", help="Capture only this window."),
    browser: bool = typer.Option(False, "--browser", help="Screenshot the persistent browser's active page."),
    full_page: bool = typer.Option(False, "--full-page", help="With --browser: capture the full scrollable page."),
) -> None:
    """截取桌面（或持久化浏览器）屏幕截图并打印图像标记。"""
    path = out or _default_shot_path("browser" if browser else "desktop")
    try:
        if browser:
            br = _browser_or_exit(launch=False)
            try:
                result = br.screenshot(path, full_page=full_page)
            finally:
                br.disconnect()
        else:
            desk = get_desktop()
            win = desk.window(title_re=window_title_re, timeout=5) if window_title_re else None
            result = desk.screenshot(path, window=win)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _fail(f"screenshot failed: {type(exc).__name__}: {exc}")
    result = Path(result).resolve()
    typer.echo(str(result))
    typer.echo(image_marker(result))


@app.command("image-show")
def image_show(path: Path = typer.Option(..., "--path", help="Image file to attach to the next observation.")) -> None:
    """校验本地图像文件并输出图像标记，以便智能体在下一轮观测中查看该图像。"""
    try:
        p = validate_image(path)
    except ValueError as exc:
        _fail(str(exc))
    typer.echo(image_marker(p))


# ---------------------------------------------------------------------------
# 桌面观测命令
# ---------------------------------------------------------------------------


@app.command("ui-tree")
def ui_tree(
    window_title_re: Optional[str] = typer.Option(None, "--window-title-re", help="Window to dump (default: foreground)."),
    depth: int = typer.Option(8, "--depth", help="Maximum tree depth."),
    max_nodes: int = typer.Option(400, "--max-nodes", help="Maximum number of nodes."),
) -> None:
    """导出前台窗口（或正则匹配窗口）的 UI Automation 控件树。"""
    try:
        desk = get_desktop()
        win = desk.window(title_re=window_title_re, timeout=5) if window_title_re else None
        typer.echo(desk.ui_tree(win, max_depth=depth, max_nodes=max_nodes))
    except Exception as exc:  # noqa: BLE001
        _fail(f"ui-tree failed: {type(exc).__name__}: {exc}")


@app.command()
def windows(as_json: bool = typer.Option(False, "--json", help="Print JSON instead of text lines.")) -> None:
    """列出所有可见顶层窗口（并标记当前前台窗口与模态对话框）。"""
    try:
        desk = get_desktop()
        wins = desk.windows()
        fg = desk.foreground()
        modals = desk.modal_dialogs()
    except Exception as exc:  # noqa: BLE001
        _fail(f"windows failed: {type(exc).__name__}: {exc}")
    modal_keys = {(m.handle, m.title) for m in modals}
    if as_json:
        rows = []
        for w in wins:
            d = w.to_dict()
            d["foreground"] = bool(fg and (w.handle, w.title) == (fg.handle, fg.title))
            d["is_modal"] = d["is_modal"] or (w.handle, w.title) in modal_keys
            rows.append(d)
        typer.echo(json.dumps({"windows": rows, "modal_dialogs": [m.to_dict() for m in modals]},
                              ensure_ascii=False, indent=1))
        return
    if not wins:
        typer.echo("(no visible windows)")
    for w in wins:
        flags = []
        if fg and (w.handle, w.title) == (fg.handle, fg.title):
            flags.append("foreground")
        if w.is_modal or (w.handle, w.title) in modal_keys:
            flags.append("modal")
        typer.echo(
            f"{w.title!r} process={w.process or '?'} pid={w.pid} hwnd=0x{w.handle:X} class={w.class_name!r} "
            f"rect={tuple(w.rect)}" + (f" [{' '.join(flags)}]" if flags else "")
        )
    extra = [m for m in modals if all((m.handle, m.title) != (w.handle, w.title) for w in wins)]
    for m in extra:
        typer.echo(f"{m.title!r} process={m.process or '?'} pid={m.pid} [modal {m.extra.get('reason', '')}]".rstrip())


# ---------------------------------------------------------------------------
# 持久化浏览器操作命令
# ---------------------------------------------------------------------------


@app.command()
def aria(max_chars: int = typer.Option(12000, "--max-chars", help="Truncate the snapshot to this many chars.")) -> None:
    """导出持久化浏览器当前活动标签页的 ARIA 无障碍树快照。"""
    br = _browser_or_exit(launch=False)
    try:
        typer.echo(br.aria_snapshot(max_chars=max_chars))
    except Exception as exc:  # noqa: BLE001
        _fail(f"aria failed: {type(exc).__name__}: {exc}")
    finally:
        br.disconnect()


@app.command("browser-open")
def browser_open(
    url: str = typer.Argument(..., help="URL to open."),
    new_tab: bool = typer.Option(False, "--new-tab", help="Open in a new tab instead of the active one."),
    wait_until: str = typer.Option("domcontentloaded", "--wait-until", help="load | domcontentloaded | networkidle | commit"),
    timeout: float = typer.Option(60, "--timeout", help="Navigation timeout in seconds."),
) -> None:
    """在持久化浏览器中打开指定 URL（若浏览器尚未运行则自动启动）。"""
    br = _browser_or_exit(launch=True)
    try:
        page = br.new_page() if new_tab else br.page()
        page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
        typer.echo(f"url: {page.url}")
        typer.echo(f"title: {page.title()}")
    except Exception as exc:  # noqa: BLE001
        _fail(f"browser-open failed: {type(exc).__name__}: {exc}")
    finally:
        br.disconnect()


@app.command("browser-exec")
def browser_exec(
    code_file: Optional[Path] = typer.Option(None, "--code-file", help="Python file to execute."),
    code: Optional[str] = typer.Option(None, "--code", help="Inline Python code (alternative to --code-file)."),
    launch: bool = typer.Option(True, "--launch/--no-launch", help="Launch the browser if no session is live."),
) -> None:
    """针对持久化浏览器执行一段同步 Playwright Python 代码片段。

    内置作用域变量：``page``（当前活动页面）、``context``、``browser``（cuaflow PersistentBrowser）、
    ``pw_browser``（Playwright Browser）。若执行抛出异常则打印调用栈并以退出码 1 退出。
    """
    if (code_file is None) == (code is None):
        _fail("give exactly one of --code-file or --code")
    if code_file is not None:
        try:
            source = Path(code_file).read_text(encoding="utf-8")
        except OSError as exc:
            _fail(f"cannot read {code_file}: {exc}")
        filename = str(code_file)
    else:
        source, filename = code or "", "<browser-exec>"
    br = _browser_or_exit(launch=launch)
    ns: dict[str, Any] = {
        "__name__": "__cuaflow_browser_exec__",
        "page": br.page(),
        "context": br.context(),
        "browser": br,
        "pw_browser": br.playwright_browser,
    }
    failed = False
    try:
        exec(compile(source, filename, "exec"), ns)  # noqa: S102
    except SystemExit as exc:
        failed = bool(exc.code)
    except BaseException:  # noqa: BLE001
        sys.stdout.flush()
        traceback.print_exc()
        failed = True
    finally:
        sys.stdout.flush()
        # 将代码片段结束时所处的页面保留为下次调用的活动页面。
        page = ns.get("page")
        if page is not None and hasattr(page, "is_closed"):
            try:
                if not page.is_closed():
                    br._active = page
            except Exception:  # noqa: BLE001
                pass
        br.disconnect()
    if failed:
        raise typer.Exit(1)


@app.command("browser-status")
def browser_status() -> None:
    """输出持久化浏览器的会话 JSON 信息及其存活状态。"""
    from cuaflow.platform.browser import PersistentBrowser

    typer.echo(json.dumps(PersistentBrowser.status(browser_session_file()), ensure_ascii=False, indent=1))


@app.command("browser-close")
def browser_close() -> None:
    """关闭正在运行的持久化浏览器并删除其会话文件。"""
    from cuaflow.platform.browser import BrowserError, PersistentBrowser

    try:
        br = PersistentBrowser.connect(browser_session_file(), launch_if_missing=False)
    except BrowserError:
        typer.echo("no live browser session")
        return
    br.shutdown()
    typer.echo("browser closed")


# ---------------------------------------------------------------------------
# 人机协同交互命令
# ---------------------------------------------------------------------------


@app.command("ask-user")
def ask_user(question: str = typer.Option(..., "--question", help="Question for the human operator.")) -> None:
    """向人类操作员提问（智能体主循环将暂停执行并转发人工回复）。"""
    typer.echo(ask_user_marker(question))


def main() -> None:  # pragma: no cover
    app()


try:  # 工作流相关扩展子命令（如 try-step 等）由 cuaflow.flow 模块注册
    from cuaflow.flow.tool_cmds import register as _register_flow_cmds
    _register_flow_cmds(app)
except ImportError:
    pass
