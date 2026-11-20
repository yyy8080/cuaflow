"""Persistent, detached Chromium/Edge browser driven over CDP (``BrowserBackend``).

The browser process is launched *detached* (own session / process group, or
``DETACHED_PROCESS`` on Windows) with ``--remote-debugging-port=0`` and a persistent
``--user-data-dir`` (login state survives). Its endpoint is persisted in a session JSON
file so any later process (flow runner, ``cuaflow-tool``, repairer) can re-attach::

    from cuaflow.platform.browser import PersistentBrowser

    br = PersistentBrowser.connect()          # reuse live session or launch a new one
    page = br.page()
    page.goto("https://example.com")
    print(br.aria_snapshot())
    br.disconnect()                           # browser keeps running
    ...
    PersistentBrowser.connect(launch_if_missing=False).shutdown()   # kill it

.. warning::
   This uses the Playwright **sync** API. It cannot be used from a thread that is running
   an asyncio event loop (Playwright raises "It looks like you are using Playwright Sync API
   inside the asyncio loop"). From async code, run it in a worker thread
   (``await asyncio.to_thread(...)``) or a subprocess, and keep every call on the thread that
   created the ``PersistentBrowser`` (Playwright sync objects are thread-affine).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cuaflow.platform._proc import IS_WINDOWS, kill_tree, pid_alive, process_cmdline, spawn_detached
from cuaflow.platform.paths import browser_profile_dir, browser_session_file

ENV_HEADLESS = "CUAFLOW_HEADLESS"
ENV_BROWSER_CHANNEL = "CUAFLOW_BROWSER_CHANNEL"  # chromium (default) | msedge | chrome
ENV_BROWSER_EXECUTABLE = "CUAFLOW_BROWSER_EXECUTABLE"
ENV_NO_SANDBOX = "CUAFLOW_NO_SANDBOX"

DEVTOOLS_ACTIVE_PORT = "DevToolsActivePort"
STARTUP_BLANK_URL = "data:text/html,"
_BLANK_URLS = ("", "about:blank", STARTUP_BLANK_URL)


class BrowserError(RuntimeError):
    pass


class BrowserNotRunning(BrowserError):
    """No live browser session and ``launch_if_missing=False``."""


class BrowserLaunchError(BrowserError):
    pass


# ---------------------------------------------------------------------------
# configuration helpers
# ---------------------------------------------------------------------------


def default_headless() -> bool:
    """``CUAFLOW_HEADLESS`` (1/0) wins; else headed on Windows/macOS, headless on Linux without DISPLAY."""
    env = os.environ.get(ENV_HEADLESS, "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    if IS_WINDOWS or sys.platform == "darwin":
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _windows_candidates(channel: str) -> list[Path]:
    pf = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432", "LOCALAPPDATA")]
    roots = [Path(p) for p in pf if p]
    rel = {
        "msedge": Path("Microsoft/Edge/Application/msedge.exe"),
        "chrome": Path("Google/Chrome/Application/chrome.exe"),
    }.get(channel)
    return [r / rel for r in roots] if rel else []


def find_channel_executable(channel: str) -> str | None:
    """Locate an installed Edge/Chrome executable for ``channel`` (``msedge`` / ``chrome``)."""
    channel = (channel or "").lower()
    if channel in ("", "chromium"):
        return None
    if IS_WINDOWS:
        for cand in _windows_candidates(channel):
            if cand.exists():
                return str(cand)
        return shutil.which(f"{channel}.exe")
    if sys.platform == "darwin":
        app = {"msedge": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
               "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}.get(channel)
        return app if app and Path(app).exists() else None
    names = {"msedge": ["microsoft-edge", "microsoft-edge-stable"],
             "chrome": ["google-chrome", "google-chrome-stable"]}.get(channel, [channel])
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def _configure_frozen_browsers_env() -> None:
    if getattr(sys, "frozen", False) and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        meipass = getattr(sys, "_MEIPASS", "")
        for cand in (
            Path(meipass) / "ms-playwright" if meipass else None,
            Path(sys.executable).resolve().parent / "ms-playwright",
        ):
            if cand and cand.is_dir():
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cand)
                break


def _bundled_chromium(pw: Any | None = None) -> str:
    _configure_frozen_browsers_env()
    if pw is not None:
        path = pw.chromium.executable_path
    else:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
    if not path or not Path(path).exists():
        raise BrowserLaunchError("Playwright chromium not found. Run `cuaflow install-browser`.")
    return path


def resolve_browser_executable(
    channel: str | None = None,
    executable: str | None = None,
    pw: Any | None = None,
) -> tuple[str, str]:
    """Locate a usable Chromium/Edge/Chrome binary, falling back to system Edge/Chrome when Chromium isn't installed."""
    exe_override = executable or os.environ.get(ENV_BROWSER_EXECUTABLE)
    if exe_override:
        if not Path(exe_override).exists():
            raise BrowserLaunchError(f"browser executable {exe_override!r} does not exist")
        return exe_override, "custom"
    ch = (channel or os.environ.get(ENV_BROWSER_CHANNEL) or "chromium").lower()
    if ch not in ("", "auto", "chromium"):
        exe = find_channel_executable(ch)
        if exe:
            return exe, ch
        raise BrowserLaunchError(f"browser channel {ch!r} not found on this machine")
    try:
        return _bundled_chromium(pw), "chromium"
    except BrowserLaunchError:
        for fallback_ch in ("msedge", "chrome"):
            exe = find_channel_executable(fallback_ch)
            if exe:
                return exe, fallback_ch
        raise BrowserLaunchError(
            "No Chromium, Microsoft Edge, or Google Chrome browser found. "
            "Install Microsoft Edge / Chrome or run `cuaflow install-browser`."
        ) from None


def install_playwright_chromium() -> int:
    """Download standalone Playwright Chromium via Playwright's bundled Node driver (works in frozen builds)."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    driver_exe, driver_cli = compute_driver_executable()
    proc = subprocess.run([str(driver_exe), str(driver_cli), "install", "chromium"], env=get_driver_env())
    return int(proc.returncode)


# ---------------------------------------------------------------------------
# session file / CDP probing
# ---------------------------------------------------------------------------


def read_session(session_file: str | Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(session_file).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_session(session_file: str | Path, info: dict[str, Any]) -> None:
    path = Path(session_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _local_opener() -> urllib.request.OpenerDirector:
    # The CDP endpoint is our own browser on 127.0.0.1: never send it through HTTP(S)_PROXY.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def cdp_version(port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    """``GET http://127.0.0.1:<port>/json/version`` or None if unreachable."""
    try:
        with _local_opener().open(f"http://127.0.0.1:{int(port)}/json/version", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def cdp_page_targets(port: int, timeout: float = 2.0) -> list[dict[str, Any]]:
    """``GET http://127.0.0.1:<port>/json/list`` filtered to ``type == 'page'`` targets."""
    try:
        with _local_opener().open(f"http://127.0.0.1:{int(port)}/json/list", timeout=timeout) as resp:
            items = json.loads(resp.read().decode("utf-8"))
            if isinstance(items, list):
                return [t for t in items if isinstance(t, dict) and t.get("type") == "page"]
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return []


def read_devtools_active_port(user_data_dir: str | Path) -> tuple[int, str] | None:
    """Parse ``<profile>/DevToolsActivePort`` → (port, browser ws path)."""
    try:
        lines = (Path(user_data_dir) / DEVTOOLS_ACTIVE_PORT).read_text(encoding="utf-8").splitlines()
        port = int(lines[0].strip())
        ws_path = lines[1].strip() if len(lines) > 1 else ""
        return (port, ws_path) if port > 0 else None
    except (OSError, ValueError, IndexError):
        return None


def session_alive(info: dict[str, Any] | None) -> bool:
    """A session is alive when its CDP endpoint answers (and its pid, if known, is running)."""
    if not info:
        return False
    port = int(info.get("port") or 0)
    if not port:
        return False
    pid = int(info.get("pid") or 0)
    if pid and not pid_alive(pid):
        return False
    ver = cdp_version(port)
    if not ver:
        return False
    ws = str(ver.get("webSocketDebuggerUrl") or "")
    cdp_url = str(info.get("cdp_url") or "")
    if ws and cdp_url.startswith("ws") and ws.rsplit("/", 1)[-1] != cdp_url.rsplit("/", 1)[-1]:
        return False  # a different browser now owns that port
    return True


class _FileLock:
    """Tiny cross-process lock (O_EXCL lock file with stale detection)."""

    def __init__(self, path: Path, timeout: float = 60.0, stale_after: float = 120.0) -> None:
        self.path, self.timeout, self.stale_after = path, timeout, stale_after
        self._fd: int | None = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    owner = int(self.path.read_text(encoding="utf-8") or 0)
                except (OSError, ValueError):
                    age, owner = 0.0, 0
                if age > self.stale_after or (owner and owner != os.getpid() and not pid_alive(owner)):
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() > deadline:
                    raise BrowserError(f"timed out waiting for browser lock {self.path}") from None
                time.sleep(0.1)

    def __exit__(self, *exc: Any) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# launching
# ---------------------------------------------------------------------------


def launch_browser(
    executable: str,
    user_data_dir: str | Path,
    *,
    headless: bool,
    window_size: tuple[int, int] = (1280, 900),
    extra_args: list[str] | None = None,
    no_sandbox: bool | None = None,
    startup_timeout: float = 30.0,
    log_path: str | Path | None = None,
) -> tuple[dict[str, Any], subprocess.Popen]:
    """Start a detached Chromium-based browser and wait for its DevTools endpoint."""
    udd = Path(user_data_dir).resolve()
    udd.mkdir(parents=True, exist_ok=True)
    try:
        (udd / DEVTOOLS_ACTIVE_PORT).unlink()
    except OSError:
        pass

    if no_sandbox is None:
        env = os.environ.get(ENV_NO_SANDBOX, "").strip().lower()
        # Playwright itself launches Chromium with --no-sandbox by default on Linux.
        no_sandbox = env in ("1", "true", "yes") or (env == "" and sys.platform.startswith("linux"))

    args = [
        executable,
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={udd}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=TranslateUI,MediaRouter",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        f"--window-size={window_size[0]},{window_size[1]}",
    ]
    if headless:
        args.append("--headless=new")
    if no_sandbox:
        args.append("--no-sandbox")
    args.extend(extra_args or [])
    args.append(STARTUP_BLANK_URL)

    log_file = Path(log_path) if log_path else udd.parent / "browser.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "ab") as log:
        proc = spawn_detached(args, stdout=log, stderr=log)

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        found = read_devtools_active_port(udd)
        targets = cdp_page_targets(found[0], timeout=1.0) if found else []
        if found and cdp_version(found[0], timeout=1.0) and any(
            str(t.get("url") or "") not in ("", "about:blank") for t in targets
        ):
            port, ws_path = found
            ver = cdp_version(port) or {}
            cdp_url = str(ver.get("webSocketDebuggerUrl") or f"ws://127.0.0.1:{port}{ws_path}")
            info = {
                "pid": proc.pid,
                "cdp_url": cdp_url,
                "port": port,
                "http_url": f"http://127.0.0.1:{port}",
                "user_data_dir": str(udd),
                "executable": executable,
                "headless": bool(headless),
                "started_at": time.time(),
                "browser_version": ver.get("Browser", ""),
            }
            return info, proc
        if proc.poll() is not None:
            tail = ""
            try:
                tail = log_file.read_text(encoding="utf-8", errors="replace")[-2000:]
            except OSError:
                pass
            hint = (" (the profile may already be in use by another browser instance)"
                    if proc.returncode == 0 else "")
            raise BrowserLaunchError(
                f"browser exited early with code {proc.returncode}{hint}; log tail:\n{tail}"
            )
        time.sleep(0.1)
    kill_tree(proc.pid)
    raise BrowserLaunchError(f"timed out after {startup_timeout:.0f}s waiting for DevToolsActivePort in {udd}")


# ---------------------------------------------------------------------------
# PersistentBrowser
# ---------------------------------------------------------------------------


class PersistentBrowser:
    """``BrowserBackend`` over a detached, persistent Chromium/Edge process.

    Prefer :meth:`connect` to construct. Usable as a context manager (``__exit__`` →
    :meth:`disconnect`, the browser keeps running).
    """

    def __init__(
        self,
        session_file: str | Path | None = None,
        *,
        headless: bool | None = None,
        user_data_dir: str | Path | None = None,
        channel: str | None = None,
        executable: str | None = None,
        window_size: tuple[int, int] = (1280, 900),
        extra_args: list[str] | None = None,
        startup_timeout: float = 30.0,
        connect_timeout: float = 30.0,
    ) -> None:
        self.session_file = Path(session_file).resolve() if session_file else browser_session_file()
        self.headless = default_headless() if headless is None else bool(headless)
        self.user_data_dir = Path(user_data_dir).resolve() if user_data_dir else browser_profile_dir(self.session_file)
        self.channel = (channel or os.environ.get(ENV_BROWSER_CHANNEL) or "chromium").lower()
        self.executable = executable or os.environ.get(ENV_BROWSER_EXECUTABLE) or None
        self.window_size = window_size
        self.extra_args = list(extra_args or [])
        self.startup_timeout = startup_timeout
        self.connect_timeout = connect_timeout

        self.session: dict[str, Any] | None = None
        self.launched = False  # True if this object launched the browser process
        self._proc: subprocess.Popen | None = None
        self._pw: Any = None
        self._browser: Any = None
        self._active: Any = None
        self._monitored_pages: set[int] = set()
        self._auto_dialog: str | None = "accept"
        self._telemetry: dict[str, Any] = self._empty_telemetry()

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    def connect(
        cls,
        session_file: str | Path | None = None,
        headless: bool | None = None,
        launch_if_missing: bool = True,
        **kwargs: Any,
    ) -> "PersistentBrowser":
        """Attach to the live session in ``session_file`` or launch a new browser.

        Raises :class:`BrowserNotRunning` if there is no live session and
        ``launch_if_missing`` is False. ``kwargs`` go to ``__init__`` (``user_data_dir``,
        ``channel``, ``executable``, ``window_size``, ``extra_args``, ...).
        """
        br = cls(session_file, headless=headless, **kwargs)
        br.start(launch_if_missing=launch_if_missing)
        return br

    @staticmethod
    def status(session_file: str | Path | None = None) -> dict[str, Any]:
        """Session JSON plus ``alive`` flag (does not connect Playwright)."""
        sf = Path(session_file) if session_file else browser_session_file()
        info = read_session(sf) or {}
        return {**info, "session_file": str(sf), "alive": session_alive(info) if info else False}

    def _resolve_executable(self) -> str:
        exe, _ch = resolve_browser_executable(channel=self.channel, executable=self.executable, pw=self._pw)
        return exe

    def _kill_stale(self, info: dict[str, Any] | None) -> None:
        """Kill a leftover browser of a dead session that still holds our profile."""
        if not info:
            return
        pid = int(info.get("pid") or 0)
        if pid and pid_alive(pid):
            cmd = process_cmdline(pid)
            if cmd and str(info.get("user_data_dir") or self.user_data_dir) in cmd:
                kill_tree(pid, graceful_timeout=3)

    def start(self, launch_if_missing: bool = True) -> "PersistentBrowser":
        if self._browser is not None:
            return self
        _configure_frozen_browsers_env()
        from playwright.sync_api import sync_playwright

        try:
            self._pw = sync_playwright().start()
        except Exception as exc:  # noqa: BLE001
            if "asyncio loop" in str(exc):
                raise BrowserError(
                    "PersistentBrowser uses the Playwright sync API and cannot run inside an asyncio "
                    "event loop thread; call it from a worker thread or subprocess"
                ) from exc
            raise
        try:
            with _FileLock(self.session_file.with_name(self.session_file.name + ".lock")):
                info = read_session(self.session_file)
                if not session_alive(info):
                    # Profile might still be served by a browser whose session file was lost.
                    adopted = self._adopt_from_profile()
                    if adopted:
                        info = adopted
                    elif not launch_if_missing:
                        raise BrowserNotRunning(f"no live browser session in {self.session_file}")
                    else:
                        self._kill_stale(info)
                        info, self._proc = launch_browser(
                            self._resolve_executable(),
                            self.user_data_dir,
                            headless=self.headless,
                            window_size=self.window_size,
                            extra_args=self.extra_args,
                            startup_timeout=self.startup_timeout,
                            log_path=self.session_file.parent / "browser.log",
                        )
                        self.launched = True
                    write_session(self.session_file, info)
                self.session = info
            last_cdp_err: Exception | None = None
            for attempt in range(3):
                try:
                    self._browser = self._pw.chromium.connect_over_cdp(
                        str(self.session["cdp_url"]), timeout=int(self.connect_timeout * 1000)
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    last_cdp_err = exc
                    time.sleep(0.3 * (attempt + 1))
            if self._browser is None:
                raise last_cdp_err or BrowserError("connect_over_cdp failed")
        except BaseException:
            self._stop_playwright()
            raise
        # Wait for Chromium's default persistent context and open page targets BEFORE calling context()
        # so we never accidentally create a transient incognito context via browser.new_context().
        port = int((self.session or {}).get("port") or 0)
        expected_pages = max(1, len(cdp_page_targets(port, timeout=2.0)) if port else 1)
        sync_deadline = time.monotonic() + (5.0 if self.launched else 3.0)
        while time.monotonic() < sync_deadline:
            ctxs = self._browser.contexts
            if ctxs and len(ctxs[0].pages) >= expected_pages:
                break
            self._pump_playwright(0.05)
        ctx = self.context()
        existing_pages = self.pages()
        if self.launched and existing_pages:
            p0 = existing_pages[-1]
            ready_deadline = time.monotonic() + 3.0
            while time.monotonic() < ready_deadline:
                try:
                    if str(p0.url or "") not in ("", "about:blank") and p0.evaluate("document.readyState") == "complete":
                        break
                except Exception:  # noqa: BLE001
                    pass
                self._pump_playwright(0.05)
        for existing in existing_pages:
            self._attach_page_monitors(existing)
        if existing_pages and self._active is None:
            self.page()
        ctx.on("page", self._on_new_page)
        return self

    def _pump_playwright(self, seconds: float = 0.05) -> None:
        """Yield to Playwright's asyncio dispatcher fiber so incoming CDP target/context events are processed."""
        if self._browser is not None and hasattr(self._browser, "_sync"):
            import asyncio

            try:
                self._browser._sync(asyncio.sleep(seconds))
                return
            except Exception:  # noqa: BLE001
                pass
        time.sleep(seconds)

    def _adopt_from_profile(self) -> dict[str, Any] | None:
        found = read_devtools_active_port(self.user_data_dir)
        if not found:
            return None
        ver = cdp_version(found[0], timeout=1.0)
        if not ver:
            return None
        port = found[0]
        return {
            "pid": 0,
            "cdp_url": str(ver.get("webSocketDebuggerUrl") or f"ws://127.0.0.1:{port}{found[1]}"),
            "port": port,
            "http_url": f"http://127.0.0.1:{port}",
            "user_data_dir": str(self.user_data_dir),
            "executable": "",
            "headless": None,
            "started_at": time.time(),
            "browser_version": ver.get("Browser", ""),
            "adopted": True,
        }

    def _stop_playwright(self) -> None:
        pw, self._pw, self._browser, self._active = self._pw, None, None, None
        self._monitored_pages.clear()
        if pw is not None:
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass

    def is_connected(self) -> bool:
        try:
            return self._browser is not None and bool(self._browser.is_connected())
        except Exception:  # noqa: BLE001
            return False

    def disconnect(self) -> None:
        """Drop the CDP connection (stop Playwright) but keep the browser process alive."""
        if self._browser is not None:
            self._remember_active_page()
        self._stop_playwright()

    def shutdown(self) -> None:
        """Close the browser gracefully (CDP ``Browser.close``), kill leftovers, delete the session file."""
        info = self.session or read_session(self.session_file) or {}
        if self._browser is not None:
            try:
                cdp = self._browser.new_browser_cdp_session()
                cdp.send("Browser.close")
            except Exception:  # noqa: BLE001
                pass
        self._stop_playwright()
        pid = int(info.get("pid") or 0)
        if pid:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and pid_alive(pid):
                if self._proc is not None and self._proc.poll() is not None:
                    break
                time.sleep(0.1)
            if pid_alive(pid):
                kill_tree(pid, graceful_timeout=2)
        if self._proc is not None:
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self._proc = None
        current = read_session(self.session_file)
        if current is not None and (not info or current.get("cdp_url") == info.get("cdp_url")):
            try:
                self.session_file.unlink()
            except OSError:
                pass
        self.session = None

    def __enter__(self) -> "PersistentBrowser":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.disconnect()

    # ------------------------------------------------------------------ pages
    def _require(self) -> Any:
        if self._browser is None:
            raise BrowserError("PersistentBrowser is not connected; call start()/connect() first")
        return self._browser

    @property
    def playwright_browser(self) -> Any:
        """The underlying Playwright ``Browser`` (CDP connection)."""
        return self._require()

    def context(self) -> Any:
        browser = self._require()
        if browser.contexts:
            return browser.contexts[0]
        return browser.new_context()

    def pages(self) -> list[Any]:
        return [p for p in self.context().pages if not p.is_closed()]

    def _on_new_page(self, page: Any) -> None:
        self._attach_page_monitors(page)
        try:
            cur = self._active
            cur_has_real_url = (
                cur is not None
                and not cur.is_closed()
                and str(cur.url or "") not in _BLANK_URLS
            )
            new_url = str(page.url or "")
        except Exception:  # noqa: BLE001
            cur_has_real_url, new_url = False, ""

        if not cur_has_real_url or new_url not in _BLANK_URLS:
            self._active = page
            return

        # Delayed startup blank tab or popup still at blank URL: only promote if it navigates to a real URL.
        def _promote_on_nav(frame: Any) -> None:
            try:
                if not page.is_closed() and frame == page.main_frame and str(frame.url or "") not in _BLANK_URLS:
                    self._active = page
            except Exception:  # noqa: BLE001
                pass

        try:
            page.on("framenavigated", _promote_on_nav)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _empty_telemetry() -> dict[str, Any]:
        return {
            "doc_status": None,
            "doc_url": None,
            "http_errors": [],
            "network_failures": [],
            "page_errors": [],
            "console_errors": [],
            "dialogs": [],
        }

    def reset_step_telemetry(self) -> None:
        self._telemetry = self._empty_telemetry()

    def drain_step_telemetry(self) -> dict[str, Any]:
        snap = dict(self._telemetry)
        try:
            snap["open_tabs"] = len(self.pages()) if self.is_connected() else 0
        except Exception:  # noqa: BLE001
            snap["open_tabs"] = 0
        self._telemetry = self._empty_telemetry()
        return snap

    def set_dialog_policy(self, policy: str | None) -> None:
        """``"accept"`` (default), ``"dismiss"``, or ``None`` (leave dialogs to step code)."""
        if policy not in ("accept", "dismiss", None):
            raise ValueError(f"invalid dialog policy {policy!r}")
        self._auto_dialog = policy

    def _attach_page_monitors(self, page: Any) -> None:
        pid = id(page)
        if pid in self._monitored_pages:
            return
        self._monitored_pages.add(pid)

        def on_console(msg: Any) -> None:
            try:
                if msg.type == "error":
                    buf = self._telemetry["console_errors"]
                    if len(buf) < 25:
                        buf.append(str(msg.text)[:400])
            except Exception:  # noqa: BLE001
                pass

        def on_pageerror(err: Any) -> None:
            buf = self._telemetry["page_errors"]
            if len(buf) < 25:
                buf.append(str(err)[:500])

        def on_requestfailed(req: Any) -> None:
            try:
                failure = req.failure or ""
                if isinstance(failure, dict):
                    failure = failure.get("errorText") or str(failure)
                failure_s = str(failure)
                if "ERR_ABORTED" in failure_s or "NS_BINDING_ABORTED" in failure_s:
                    return
                buf = self._telemetry["network_failures"]
                if len(buf) < 25:
                    buf.append({"url": str(req.url)[:300], "resource_type": str(req.resource_type), "failure": failure_s[:200]})
            except Exception:  # noqa: BLE001
                pass

        def on_response(resp: Any) -> None:
            try:
                status = int(resp.status)
                rtype = str(resp.request.resource_type)
                is_main_doc = rtype == "document" and resp.frame == page.main_frame
                if is_main_doc:
                    self._telemetry["doc_status"] = status
                    self._telemetry["doc_url"] = str(resp.url)[:400]
                if status >= 400 and rtype in ("document", "xhr", "fetch"):
                    buf = self._telemetry["http_errors"]
                    if len(buf) < 25:
                        buf.append({"status": status, "url": str(resp.url)[:300], "resource_type": rtype, "main_frame": is_main_doc})
            except Exception:  # noqa: BLE001
                pass

        def on_dialog(dialog: Any) -> None:
            entry = {"type": "", "message": "", "handled": False}
            try:
                entry["type"] = str(dialog.type)
                entry["message"] = str(dialog.message)[:400]
                if self._auto_dialog == "accept":
                    dialog.accept()
                    entry["handled"] = True
                elif self._auto_dialog == "dismiss":
                    dialog.dismiss()
                    entry["handled"] = True
            except Exception:  # noqa: BLE001 - already handled by step code
                pass
            buf = self._telemetry["dialogs"]
            if len(buf) < 20:
                buf.append(entry)

        try:
            page.on("console", on_console)
            page.on("pageerror", on_pageerror)
            page.on("requestfailed", on_requestfailed)
            page.on("response", on_response)
            page.on("dialog", on_dialog)
        except Exception:  # noqa: BLE001
            pass

    def set_active(self, page: Any) -> Any:
        """Mark ``page`` as the one :meth:`page` returns (also brings it to front)."""
        self._attach_page_monitors(page)
        self._active = page
        try:
            page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        return page

    def page(self) -> Any:
        """Most recently active non-closed page of the default context (creates one if none)."""
        if self._active is not None and not self._active.is_closed():
            self._attach_page_monitors(self._active)
            return self._active
        pages = self.pages()
        if not pages:
            return self.set_active(self.context().new_page())
        chosen = None
        target = str((self.session or {}).get("active_target_id") or "")
        if target:
            for p in pages:
                if self._target_id(p) == target:
                    chosen = p
                    break
        if chosen is None:
            for p in reversed(pages):
                try:
                    if p.evaluate("document.visibilityState === 'visible' && document.hasFocus()"):
                        chosen = p
                        break
                except Exception:  # noqa: BLE001
                    continue
        self._active = chosen or pages[-1]
        self._attach_page_monitors(self._active)
        return self._active

    def new_page(self, url: str | None = None) -> Any:
        page = self.context().new_page()
        self.set_active(page)
        if url:
            page.goto(url, wait_until="domcontentloaded")
        return page

    def _target_id(self, page: Any) -> str:
        try:
            cdp = self.context().new_cdp_session(page)
            try:
                return str(cdp.send("Target.getTargetInfo").get("targetInfo", {}).get("targetId") or "")
            finally:
                cdp.detach()
        except Exception:  # noqa: BLE001
            return ""

    def _remember_active_page(self) -> None:
        if self.session is None:
            return
        try:
            page = self._active if self._active is not None and not self._active.is_closed() else None
            if page is None:
                return
            tid = self._target_id(page)
            if not tid:
                return
            self.session["active_target_id"] = tid
            self.session["last_url"] = page.url
            current = read_session(self.session_file)
            if current and current.get("cdp_url") == self.session.get("cdp_url"):
                current.update(active_target_id=tid, last_url=page.url)
                write_session(self.session_file, current)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ long-run hygiene & helpers
    def prune_tabs(self, keep_max: int = 6) -> int:
        """Close excess background/blank tabs so long-horizon runs do not leak memory."""
        if not self.is_connected():
            return 0
        pages = self.pages()
        if len(pages) <= max(1, keep_max):
            return 0
        active = self.page()
        closed = 0
        # First close background blank tabs, then oldest non-active tabs.
        candidates = [p for p in pages if p is not active and p.url in _BLANK_URLS] + [
            p for p in pages if p is not active and p.url not in _BLANK_URLS
        ]
        for p in candidates:
            if len(pages) - closed <= max(1, keep_max):
                break
            try:
                p.close()
                closed += 1
            except Exception:  # noqa: BLE001
                pass
        return closed

    def wait_stable(self, page: Any | None = None, timeout: float = 10.0, idle_ms: int = 250) -> None:
        """Wait for DOM readiness + brief network/layout settle without hanging on long-polling."""
        p = page or self.page()
        try:
            p.wait_for_load_state("domcontentloaded", timeout=int(timeout * 1000))
        except Exception:  # noqa: BLE001
            pass
        try:
            p.wait_for_load_state("networkidle", timeout=int(min(timeout, 2.0) * 1000))
        except Exception:  # noqa: BLE001
            pass
        if idle_ms > 0:
            try:
                p.wait_for_timeout(idle_ms)
            except Exception:  # noqa: BLE001
                time.sleep(idle_ms / 1000.0)

    def dismiss_overlays(self, page: Any | None = None) -> list[str]:
        """Safely dismiss common cookie banners or closeable modal overlays (never touches CAPTCHAs)."""
        p = page or self.page()
        health = self.page_health(p)
        if health.get("captcha_detected"):
            return []
        dismissed: list[str] = []
        script = """() => {
            const clicked = [];
            const bannerRe = /cookie|consent|gdpr|privacy|banner|modal|overlay|popup|toast/i;
            const acceptLabels = ['接受', '全部接受', '同意', '我知道了', '知道了', '关闭', 'Accept', 'Accept all', 'Accept All', 'I agree', 'Got it'];
            const candidates = Array.from(document.querySelectorAll('button, [role="button"]'));
            for (const btn of candidates) {
                const rect = btn.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                const style = window.getComputedStyle(btn);
                if (style.visibility === 'hidden' || style.display === 'none') continue;
                const label = (btn.innerText || btn.getAttribute('aria-label') || '').trim();
                if (!acceptLabels.includes(label)) continue;
                let anc = btn.parentElement;
                let inOverlay = false;
                for (let d = 0; anc && d < 6; d++, anc = anc.parentElement) {
                    const cls = `${anc.id || ''} ${anc.className || ''} ${anc.getAttribute('role') || ''} ${anc.getAttribute('aria-modal') || ''}`;
                    const pos = window.getComputedStyle(anc).position;
                    if (bannerRe.test(cls) || pos === 'fixed' || anc.getAttribute('role') === 'dialog') {
                        inOverlay = true;
                        break;
                    }
                }
                if (inOverlay) {
                    btn.click();
                    clicked.push(label);
                    if (clicked.length >= 2) break;
                }
            }
            return clicked;
        }"""
        try:
            res = p.evaluate(script)
            if isinstance(res, list):
                dismissed.extend(str(x) for x in res)
        except Exception:  # noqa: BLE001
            pass
        return dismissed

    def extract_table(self, selector: str = "table", page: Any | None = None, max_rows: int = 500) -> list[dict[str, str]]:
        """Extract an HTML table into a list of row dicts keyed by header names."""
        p = page or self.page()
        js = """([sel, limit]) => {
            const tbl = document.querySelector(sel);
            if (!tbl) return [];
            const ths = Array.from(tbl.querySelectorAll('thead th, tr:first-child th')).map(
                (el, i) => (el.innerText || '').trim() || `col_${i}`
            );
            const bodyRows = tbl.querySelectorAll('tbody tr').length
                ? Array.from(tbl.querySelectorAll('tbody tr'))
                : Array.from(tbl.querySelectorAll('tr')).slice(ths.length ? 1 : 0);
            const out = [];
            for (const tr of bodyRows.slice(0, limit)) {
                const cells = Array.from(tr.querySelectorAll('td, th')).map(c => (c.innerText || '').trim());
                if (!cells.length) continue;
                const row = {};
                cells.forEach((v, i) => { row[ths[i] || `col_${i}`] = v; });
                out.push(row);
            }
            return out;
        }"""
        res = p.evaluate(js, [selector, int(max_rows)])
        return res if isinstance(res, list) else []

    def extract_links(self, selector: str = "a[href]", page: Any | None = None, limit: int = 200) -> list[dict[str, str]]:
        """Extract visible/matching anchor links with resolved absolute URLs."""
        p = page or self.page()
        js = """([sel, lim]) => Array.from(document.querySelectorAll(sel)).slice(0, lim).map(a => ({
            text: (a.innerText || a.getAttribute('aria-label') || '').trim(),
            href: a.href || a.getAttribute('href') || ''
        })).filter(x => x.href)"""
        res = p.evaluate(js, [selector, int(limit)])
        return res if isinstance(res, list) else []

    def page_health(self, page: Any | None = None) -> dict[str, Any]:
        """Zero-token DOM health & safety probe (error pages, alerts, login walls, passive CAPTCHA check)."""
        p = page or self.page()
        url = ""
        try:
            url = str(p.url or "")
        except Exception:  # noqa: BLE001
            pass
        if url.startswith("chrome-error://") or url.startswith("edge-error://"):
            return {
                "url": url,
                "is_error_page": True,
                "error_reason": "browser_network_error_page",
                "captcha_detected": False,
                "login_wall_detected": False,
                "body_text_len": 0,
                "alerts": [],
                "dom_fingerprint": f"err:{url}",
            }
        js = """() => {
            const visible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width <= 2 || r.height <= 2) return false;
                const s = window.getComputedStyle(el);
                return s.visibility !== 'hidden' && s.display !== 'none';
            };
            const title = document.title || '';
            const bodyText = (document.body ? (document.body.innerText || '') : '').trim();
            // Passive CAPTCHA / anti-bot gate detection ONLY so the supervisor can stop & escalate safely.
            const iframes = Array.from(document.querySelectorAll('iframe')).filter(visible);
            const captchaFrame = iframes.some(f => {
                const src = (f.src || '').toLowerCase();
                return src.includes('recaptcha/api2/bframe') || src.includes('recaptcha/enterprise/bframe')
                    || src.includes('hcaptcha.com') || src.includes('challenges.cloudflare.com');
            });
            const cfChallenge = Boolean(
                document.querySelector('#challenge-running, #cf-challenge-running, form#challenge-form')
                || (title === 'Just a moment...' && document.querySelector('[name="cf-turnstile-response"]'))
            );
            const hasVisibleCaptchaBox = Array.from(
                document.querySelectorAll('.cf-turnstile, .h-captcha, #captcha-box, [data-sitekey]')
            ).some(el => visible(el) && /verify you are human|完成人机验证|请完成安全验证/i.test(bodyText));

            const pwVisible = Array.from(document.querySelectorAll('input[type="password"]')).some(visible);
            const path = (location.pathname + location.search).toLowerCase();
            const loginUrl = /(^|\\/|\\b)(login|signin|sign-in|sso|auth)(\\/|\\b|\\?|$)/.test(path);

            const alerts = Array.from(document.querySelectorAll('[role="alert"], .alert-danger, .toast-error, .error-banner'))
                .filter(visible)
                .map(el => (el.innerText || '').trim().slice(0, 200))
                .filter(Boolean)
                .slice(0, 5);

            const headings = Array.from(document.querySelectorAll('h1, h2, h3, button, [role="alert"]'))
                .slice(0, 20)
                .map(e => (e.innerText || '').trim().slice(0, 40))
                .join('|');
            let hash = 5381;
            const sig = `${location.href}::${title}::${bodyText.length}::${headings}`;
            for (let i = 0; i < sig.length; i++) {
                hash = ((hash << 5) + hash + sig.charCodeAt(i)) >>> 0;
            }
            const netErr = Boolean(document.querySelector('#main-frame-error, .neterror'));
            return {
                url: location.href,
                title,
                ready_state: document.readyState,
                body_text_len: bodyText.length,
                is_error_page: netErr,
                captcha_detected: Boolean(captchaFrame || cfChallenge || hasVisibleCaptchaBox),
                login_wall_detected: Boolean(pwVisible && loginUrl),
                alerts,
                dom_fingerprint: hash.toString(16),
            };
        }"""
        try:
            res = p.evaluate(js)
            if isinstance(res, dict):
                return res
        except Exception as exc:  # noqa: BLE001
            return {"url": url, "probe_error": str(exc)[:200], "is_error_page": False,
                    "captcha_detected": False, "login_wall_detected": False, "alerts": []}
        return {"url": url, "is_error_page": False, "captcha_detected": False, "login_wall_detected": False, "alerts": []}

    # ------------------------------------------------------------------ observation
    def aria_snapshot(self, page: Any | None = None, max_chars: int = 12000) -> str:
        p = page or self.page()
        try:
            title = p.title()
        except Exception:  # noqa: BLE001
            title = ""
        header = f"URL: {p.url}\nTitle: {title}\n"
        try:
            body = p.locator("body").aria_snapshot(timeout=10000)
        except Exception as exc:  # noqa: BLE001
            acc = getattr(p, "accessibility", None)
            if acc is not None:
                try:
                    body = json.dumps(acc.snapshot(), ensure_ascii=False, indent=1)
                except Exception as exc2:  # noqa: BLE001
                    body = f"[aria snapshot failed: {exc}; accessibility snapshot failed: {exc2}]"
            else:
                body = f"[aria snapshot failed: {exc}]"
        return truncate_text(header + (body or ""), max_chars)

    def screenshot(self, path: str | Path, page: Any | None = None, full_page: bool = False) -> Path:
        p = page or self.page()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        p.screenshot(path=str(path), full_page=full_page)
        return path.resolve()


def truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text.rfind("\n", 0, max_chars)
    if cut < max_chars * 0.6:
        cut = max_chars
    return text[:cut] + f"\n[... truncated {len(text) - cut} chars ...]"
