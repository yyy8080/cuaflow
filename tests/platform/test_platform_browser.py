"""独立常驻浏览器（Playwright Chromium 无头模式）的端到端测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cuaflow.platform import BrowserBackend, BrowserNotRunning, PersistentBrowser
from cuaflow.platform._proc import pid_alive
from cuaflow.platform.browser import default_headless, read_session, session_alive, truncate_text

pytestmark = pytest.mark.browser

SRC = Path(__file__).resolve().parents[2] / "src"
PAGE_HTML = "<html><head><title>CF Test</title></head><body><h1>Persistent 中文</h1><button>Press me</button></body></html>"


@pytest.fixture
def session_file(tmp_path):
    sf = tmp_path / "browser" / "session.json"
    yield sf
    info = read_session(sf)
    if info and info.get("pid") and pid_alive(int(info["pid"])):
        try:
            PersistentBrowser.connect(sf, launch_if_missing=False).shutdown()
        except Exception:  # noqa: BLE001
            from cuaflow.platform._proc import kill_tree

            kill_tree(int(info["pid"]))


def test_launch_reconnect_shutdown(session_file, tmp_path):
    html = tmp_path / "page.html"
    html.write_text(PAGE_HTML, encoding="utf-8")

    with pytest.raises(BrowserNotRunning):
        PersistentBrowser.connect(session_file, headless=True, launch_if_missing=False)

    br = PersistentBrowser.connect(session_file, headless=True)
    assert isinstance(br, BrowserBackend)
    assert br.launched and br.is_connected()
    info = json.loads(session_file.read_text(encoding="utf-8"))
    for key in ("pid", "cdp_url", "user_data_dir", "started_at"):
        assert key in info
    assert Path(info["user_data_dir"]) == session_file.parent / "profile"
    assert (Path(info["user_data_dir"]) / "DevToolsActivePort").exists()
    pid = int(info["pid"])

    page = br.page()
    page.goto(html.as_uri())
    snap = br.aria_snapshot()
    assert "Persistent 中文" in snap and "Press me" in snap
    assert snap.startswith("URL: file://")
    shot = br.screenshot(tmp_path / "shot.png")
    assert shot.read_bytes().startswith(b"\x89PNG")

    # 新打开的第二个标签页会成为当前活动页面
    p2 = br.new_page("data:text/html;charset=utf-8,<p>second tab</p>")
    assert br.page() is p2
    p2.close()
    assert br.page().url == html.as_uri()

    br.disconnect()
    assert not br.is_connected()
    assert pid_alive(pid), "browser must survive disconnect()"
    assert session_alive(read_session(session_file))

    br2 = PersistentBrowser.connect(session_file, launch_if_missing=False)
    assert not br2.launched
    assert br2.page().url == html.as_uri()
    assert "Press me" in br2.aria_snapshot(max_chars=2000)

    br2.shutdown()
    assert not pid_alive(pid)
    assert not session_file.exists()
    assert PersistentBrowser.status(session_file)["alive"] is False


def test_survives_creating_process_and_restores_active_tab(session_file, tmp_path):
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(SRC)!r})
        from cuaflow.platform.browser import PersistentBrowser
        br = PersistentBrowser.connect({str(session_file)!r}, headless=True)
        br.page().goto("data:text/html;charset=utf-8,<h1>first</h1>")
        br.new_page("data:text/html;charset=utf-8,<h1>active one</h1>")
        br.new_page("data:text/html;charset=utf-8,<h1>third</h1>")
        br.set_active(br.pages()[1])
        br.disconnect()
        print("done")
        """
    )
    env = dict(os.environ, PYTHONPATH=str(SRC))
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120, env=env)
    assert res.returncode == 0, res.stderr
    info = read_session(session_file)
    assert info and pid_alive(int(info["pid"])), "browser died with its launching process"
    assert info.get("active_target_id")

    br = PersistentBrowser.connect(session_file, launch_if_missing=False)
    try:
        assert len(br.pages()) == 3
        assert "active one" in br.aria_snapshot()
    finally:
        br.shutdown()


def test_stale_session_file_relaunches(session_file):
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(json.dumps({"pid": 999999, "cdp_url": "ws://127.0.0.1:1/devtools/browser/x",
                                        "port": 1, "user_data_dir": "", "started_at": 0}))
    assert not session_alive(read_session(session_file))
    with pytest.raises(BrowserNotRunning):
        PersistentBrowser.connect(session_file, launch_if_missing=False)
    br = PersistentBrowser.connect(session_file, headless=True)
    try:
        assert br.launched and read_session(session_file)["pid"] != 999999
    finally:
        br.shutdown()


def test_headless_default_env(monkeypatch):
    monkeypatch.setenv("CUAFLOW_HEADLESS", "1")
    assert default_headless() is True
    monkeypatch.setenv("CUAFLOW_HEADLESS", "0")
    assert default_headless() is False
    monkeypatch.delenv("CUAFLOW_HEADLESS")
    if sys.platform.startswith("linux"):
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert default_headless() is True


def test_truncate_text():
    text = "\n".join(f"line {i}" for i in range(1000))
    out = truncate_text(text, 200)
    assert len(out) < 260 and "truncated" in out
    assert truncate_text("short", 200) == "short"
