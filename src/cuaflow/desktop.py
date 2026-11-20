"""面向已安装 cuaflow 发行版的单窗口桌面端启动器。

启动后在进程内静默拉起本地后端服务，并直接以独立桌面窗口形式打开内置多智能体对话式 WebUI
（优先使用原生 pywebview WebView2 窗口，自动回退至独立无边框 Edge/Chrome App 壳窗口，不再额外弹出独立的控制器小窗或浏览器标签页）。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any


def _local_get_json(url: str, timeout: float = 1.5) -> dict[str, Any] | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _cuaflow_alive(host: str, port: int) -> bool:
    h = _local_get_json(f"http://{host}:{port}/api/health", timeout=1.2)
    return bool(h and h.get("ok") is True and "version" in h)


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _pick_port(host: str, preferred: int) -> int:
    if _port_free(host, preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _ensure_std_streams(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / "desktop.log"
    if sys.stdout is None or sys.stderr is None:
        fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115
        if sys.stdout is None:
            sys.stdout = fh
        if sys.stderr is None:
            sys.stderr = fh
    return log_path


def _find_app_shell_browser() -> str | None:
    """查找可用于承载独立无地址栏 App 窗口的系统 Edge 或 Chrome 可执行文件。"""
    from cuaflow.platform.browser.session import find_channel_executable

    for ch in ("msedge", "chrome", "chromium"):
        exe = find_channel_executable(ch)
        if exe:
            return exe
    return None


def _open_standalone_webui(
    console_url: str,
    data_root: Path,
    *,
    wait: bool = True,
    close_hooks: list[Any] | None = None,
) -> str:
    """以单一独立桌面应用窗口打开内置 WebUI，返回实际采用的外壳模式名称。

    优先级：
    1. `pywebview` 原生系统 WebView2 桌面窗口（阻塞至窗口关闭）。
    2. 系统 Edge / Chrome 的 `--app=...` 独立应用窗口模式（使用专属 `webui_shell` 配置目录，无浏览器标签栏与地址栏）。
    3. 兜底调用系统默认浏览器。
    """
    if wait:
        try:
            import webview  # type: ignore[import-not-found]

            win = webview.create_window(
                "cuaflow — 多智能体对话工作台",
                console_url,
                width=1420,
                height=900,
                min_size=(980, 640),
            )
            if close_hooks is not None:
                close_hooks.append(win.destroy)
            webview.start()
            return "pywebview"
        except Exception:  # noqa: BLE001
            pass

    exe = _find_app_shell_browser()
    if exe:
        shell_profile = (data_root / "webui_shell").resolve()
        shell_profile.mkdir(parents=True, exist_ok=True)
        cmd = [
            exe,
            f"--app={console_url}",
            f"--user-data-dir={shell_profile}",
            "--window-size=1420,900",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=TranslateUI",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            if close_hooks is not None:
                close_hooks.append(proc.terminate)
            if not wait:
                return "app_window_detached"
            started = time.monotonic()
            proc.wait()
            # 若进程在 2 秒内正常驻留后由用户关闭窗口，则视为桌面主窗口已退出
            if time.monotonic() - started >= 2.0:
                return "app_window_closed"
            return "app_window_delegated"
        except OSError:
            pass

    webbrowser.open(console_url)
    return "browser_fallback"


def main(argv: list[str] | None = None) -> int:
    raw_args = sys.argv[1:] if argv is None else list(argv)
    if len(raw_args) >= 1 and raw_args[0] == "--runner-proc":
        from cuaflow.flow.proc import main as runner_main

        return int(runner_main())

    parser = argparse.ArgumentParser(prog="cuaflow-desktop", description="cuaflow 独立单窗口 WebUI 桌面端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.environ.get("CUAFLOW_TOKEN") or None)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开界面窗口")
    parser.add_argument("--no-gui", action="store_true", help="仅作为后台无界面守护服务运行")
    args = parser.parse_args(raw_args)

    from cuaflow.platform.paths import data_dir

    root = data_dir()
    _ensure_std_streams(root)

    tok_file = root / "token"
    info_file = root / "server_info.json"
    token = args.token
    if not token:
        token = tok_file.read_text(encoding="utf-8").strip() if tok_file.is_file() else secrets.token_urlsafe(24)
        tok_file.write_text(token, encoding="utf-8")

    # 若本地已有正在运行的 cuaflow 实例，则直接复用已有服务唤醒界面，不再重复启动第二个后端
    existing_port = args.port
    if info_file.is_file():
        try:
            saved = json.loads(info_file.read_text(encoding="utf-8"))
            if int(saved.get("port") or 0) > 0:
                existing_port = int(saved["port"])
        except (OSError, ValueError):
            pass
    for probe_port in dict.fromkeys([args.port, existing_port]):
        if _cuaflow_alive(args.host, probe_port):
            url = f"http://{args.host}:{probe_port}/?token={token}"
            if not args.no_browser:
                if args.no_gui:
                    webbrowser.open(url)
                else:
                    _open_standalone_webui(url, root, wait=False)
            return 0

    port = _pick_port(args.host, args.port)
    console_url = f"http://{args.host}:{port}/?token={token}"

    import uvicorn

    from cuaflow.server import create_app
    from cuaflow.services import Services

    svc = Services(start_worker=True)
    app = create_app(svc, token)
    cfg = uvicorn.Config(app, host=args.host, port=port, log_level="warning", log_config=None)
    server = uvicorn.Server(cfg)

    srv_thread = threading.Thread(target=server.run, name="cuaflow-uvicorn", daemon=True)
    srv_thread.start()

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if _cuaflow_alive(args.host, port):
            break
        if not srv_thread.is_alive():
            return 1
        time.sleep(0.1)

    info_file.write_text(
        json.dumps({"host": args.host, "port": port, "pid": os.getpid(), "started_at": time.time()}, indent=2),
        encoding="utf-8",
    )

    close_hooks: list[Any] = []

    def stop_server() -> None:
        server.should_exit = True
        for hook in list(close_hooks):
            try:
                hook()
            except Exception:  # noqa: BLE001
                pass
        try:
            svc.close()
        except Exception:  # noqa: BLE001
            pass

    svc.updater.set_shutdown_callback(stop_server)

    try:
        # 标准桌面模式：直接打开单一独立 WebUI 窗口，关闭窗口时自动安全停止内置服务
        if not args.no_gui and not args.no_browser:
            mode = _open_standalone_webui(console_url, root, wait=True, close_hooks=close_hooks)
            if mode in ("pywebview", "app_window_closed"):
                stop_server()
                srv_thread.join(timeout=5.0)
                return 0
        elif not args.no_browser:
            webbrowser.open(console_url)

        def _sig_handler(*_: Any) -> None:
            stop_server()

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _sig_handler)
                except ValueError:
                    pass

        while srv_thread.is_alive() and not server.should_exit:
            time.sleep(0.25)
        srv_thread.join(timeout=5.0)
        return 0
    finally:
        try:
            if info_file.is_file():
                cur = json.loads(info_file.read_text(encoding="utf-8"))
                if cur.get("pid") == os.getpid():
                    info_file.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
