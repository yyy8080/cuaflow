"""``cuaflow-tool``: terminal tools for agents (Author / Repairer) running shell commands.

Output conventions (parsed by the agent loop, see :mod:`cuaflow.platform.markers`):

* ``[[CUAFLOW_IMAGE:<abs path>]]``   – attach the image to the next observation
* ``[[CUAFLOW_ASK_USER:<question>]]`` – ask the human

Environment: ``CUAFLOW_DATA_DIR``, ``CUAFLOW_BROWSER_SESSION`` (see :mod:`cuaflow.platform.paths`).
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
# factories (monkeypatched in tests)
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
# images
# ---------------------------------------------------------------------------


def validate_image(path: str | Path) -> Path:
    """Return the resolved path of a readable PNG/JPEG/GIF/WebP image or raise ValueError."""
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
    """Take a desktop (or browser) screenshot and print an image marker."""
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
    """Validate an image and print the image marker so the agent sees it."""
    try:
        p = validate_image(path)
    except ValueError as exc:
        _fail(str(exc))
    typer.echo(image_marker(p))


# ---------------------------------------------------------------------------
# desktop
# ---------------------------------------------------------------------------


@app.command("ui-tree")
def ui_tree(
    window_title_re: Optional[str] = typer.Option(None, "--window-title-re", help="Window to dump (default: foreground)."),
    depth: int = typer.Option(8, "--depth", help="Maximum tree depth."),
    max_nodes: int = typer.Option(400, "--max-nodes", help="Maximum number of nodes."),
) -> None:
    """Dump the UI Automation tree of the foreground (or matching) window."""
    try:
        desk = get_desktop()
        win = desk.window(title_re=window_title_re, timeout=5) if window_title_re else None
        typer.echo(desk.ui_tree(win, max_depth=depth, max_nodes=max_nodes))
    except Exception as exc:  # noqa: BLE001
        _fail(f"ui-tree failed: {type(exc).__name__}: {exc}")


@app.command()
def windows(as_json: bool = typer.Option(False, "--json", help="Print JSON instead of text lines.")) -> None:
    """List visible top-level windows (foreground and modal dialogs flagged)."""
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
# browser
# ---------------------------------------------------------------------------


@app.command()
def aria(max_chars: int = typer.Option(12000, "--max-chars", help="Truncate the snapshot to this many chars.")) -> None:
    """ARIA snapshot of the persistent browser's active page."""
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
    """Open URL in the persistent browser (launching it if needed)."""
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
    """Run a sync-Playwright Python snippet against the persistent browser.

    In scope: ``page`` (active page), ``context``, ``browser`` (cuaflow PersistentBrowser),
    ``pw_browser`` (Playwright Browser). Exit code 1 with a traceback on exception.
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
        # Keep the page the snippet ended on as the active page for the next call.
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
    """Print the persistent browser session JSON and whether it is alive."""
    from cuaflow.platform.browser import PersistentBrowser

    typer.echo(json.dumps(PersistentBrowser.status(browser_session_file()), ensure_ascii=False, indent=1))


@app.command("browser-close")
def browser_close() -> None:
    """Shut down the persistent browser (if running) and remove its session file."""
    from cuaflow.platform.browser import BrowserError, PersistentBrowser

    try:
        br = PersistentBrowser.connect(browser_session_file(), launch_if_missing=False)
    except BrowserError:
        typer.echo("no live browser session")
        return
    br.shutdown()
    typer.echo("browser closed")


# ---------------------------------------------------------------------------
# human in the loop
# ---------------------------------------------------------------------------


@app.command("ask-user")
def ask_user(question: str = typer.Option(..., "--question", help="Question for the human operator.")) -> None:
    """Ask the human a question (the agent loop pauses and relays the answer)."""
    typer.echo(ask_user_marker(question))


def main() -> None:  # pragma: no cover
    app()


try:  # flow commands (try-step etc.) are registered by cuaflow.flow
    from cuaflow.flow.tool_cmds import register as _register_flow_cmds
    _register_flow_cmds(app)
except ImportError:
    pass
