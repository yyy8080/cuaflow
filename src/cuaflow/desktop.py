"""Standalone desktop launcher for installed cuaflow builds (starts local server, opens browser, shows control window)."""

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


def _open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _run_tk_window(console_url: str, display_addr: str, data_root: Path, browser_desc: str, stop_fn) -> bool:
    try:
        import tkinter as tk
    except Exception:  # noqa: BLE001
        return False

    try:
        win = tk.Tk()
    except Exception:  # noqa: BLE001
        return False

    win.title("cuaflow 桌面控制器")
    win.geometry("430x255")
    win.resizable(False, False)
    win.configure(bg="#0f172a")

    ico_candidates = [
        Path(sys.executable).resolve().parent / "cuaflow.ico",
        Path(__file__).resolve().parents[2] / "packaging" / "cuaflow.ico",
    ]
    for ico in ico_candidates:
        if ico.is_file() and sys.platform == "win32":
            try:
                win.iconbitmap(default=str(ico))
            except Exception:  # noqa: BLE001
                pass
            break

    header = tk.Frame(win, bg="#0f172a", padx=20, pady=16)
    header.pack(fill="x")
    tk.Label(
        header,
        text="cuaflow 智能浏览器与桌面代理",
        fg="#f8fafc",
        bg="#0f172a",
        font=("Microsoft YaHei UI", 13, "bold"),
        anchor="w",
    ).pack(fill="x")
    tk.Label(
        header,
        text=f"● 服务运行中：{display_addr}  |  浏览器内核：{browser_desc}",
        fg="#4ade80",
        bg="#0f172a",
        font=("Microsoft YaHei UI", 9),
        anchor="w",
         pady=4,
    ).pack(fill="x")
    tk.Label(
        header,
        text="关闭浏览器网页不会中断后台长任务与定时巡检；点击下方「停止并退出」可彻底关闭。",
        fg="#94a3b8",
        bg="#0f172a",
        font=("Microsoft YaHei UI", 8),
        wraplength=390,
        justify="left",
        anchor="w",
    ).pack(fill="x")

    status_var = tk.StringVar(value="")
    btns = tk.Frame(win, bg="#0f172a", padx=20, pady=6)
    btns.pack(fill="x")

    def open_console() -> None:
        webbrowser.open(console_url)
        status_var.set("已在默认浏览器打开控制台")

    def copy_url() -> None:
        win.clipboard_clear()
        win.clipboard_append(console_url)
        status_var.set("控制台完整链接已复制到剪贴板")

    def open_dir() -> None:
        _open_folder(data_root)

    def quit_app() -> None:
        status_var.set("正在安全停止后台服务...")
        win.update_idletasks()
        stop_fn()
        win.destroy()

    btn_style = {"font": ("Microsoft YaHei UI", 9, "bold"), "bd": 0, "padx": 12, "pady": 8, "cursor": "hand2"}
    tk.Button(btns, text="打开 Web 控制台", bg="#2563eb", fg="#ffffff", activebackground="#1d4ed8",
              activeforeground="#ffffff", command=open_console, **btn_style).pack(side="left", padx=(0, 8))
    tk.Button(btns, text="复制链接", bg="#1e293b", fg="#e2e8f0", activebackground="#334155",
              activeforeground="#ffffff", command=copy_url, **btn_style).pack(side="left", padx=(0, 8))
    tk.Button(btns, text="数据目录", bg="#1e293b", fg="#e2e8f0", activebackground="#334155",
              activeforeground="#ffffff", command=open_dir, **btn_style).pack(side="left", padx=(0, 8))
    tk.Button(btns, text="停止并退出", bg="#dc2626", fg="#ffffff", activebackground="#b91c1c",
              activeforeground="#ffffff", command=quit_app, **btn_style).pack(side="right")

    tk.Label(win, textvariable=status_var, fg="#38bdf8", bg="#0f172a",
             font=("Microsoft YaHei UI", 8), padx=20, anchor="w").pack(fill="x", pady=(6, 0))

    win.protocol("WM_DELETE_WINDOW", quit_app)
    win.mainloop()
    return True


def main(argv: list[str] | None = None) -> int:
    raw_args = sys.argv[1:] if argv is None else list(argv)
    if len(raw_args) >= 1 and raw_args[0] == "--runner-proc":
        from cuaflow.flow.proc import main as runner_main

        return int(runner_main())

    parser = argparse.ArgumentParser(prog="cuaflow-desktop", description="cuaflow desktop launcher & server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.environ.get("CUAFLOW_TOKEN") or None)
    parser.add_argument("--no-browser", action="store_true", help="do not open web browser automatically")
    parser.add_argument("--no-gui", action="store_true", help="run server in background without controller window")
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

    # Reuse already running local cuaflow instance if alive
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
                webbrowser.open(url)
            return 0

    port = _pick_port(args.host, args.port)
    console_url = f"http://{args.host}:{port}/?token={token}"
    display_addr = f"http://{args.host}:{port}"

    import uvicorn

    from cuaflow.platform.browser.session import resolve_browser_executable
    from cuaflow.server import create_app
    from cuaflow.services import Services

    try:
        _bexe, browser_desc = resolve_browser_executable()
    except Exception:  # noqa: BLE001
        browser_desc = "未检测到浏览器"

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

    def stop_server() -> None:
        server.should_exit = True
        try:
            svc.close()
        except Exception:  # noqa: BLE001
            pass

    if not args.no_browser:
        webbrowser.open(console_url)

    try:
        if not args.no_gui and _run_tk_window(console_url, display_addr, root, browser_desc, stop_server):
            stop_server()
            srv_thread.join(timeout=5.0)
            return 0

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
