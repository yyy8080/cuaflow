"""Tests for desktop launcher, direct API key masking, browser auto-fallback, and frozen --runner-proc dispatch."""

from __future__ import annotations

import socket
import threading
import time
import urllib.request
from pathlib import Path

from fastapi.testclient import TestClient

from cuaflow.config import MASK_PREFIX
from cuaflow.desktop import main as desktop_main
from cuaflow.flow.client import RunnerClient
from cuaflow.platform.browser import session as browser_session
from cuaflow.server import create_app
from cuaflow.services import Services


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_settings_direct_api_key_masked_and_preserved(tmp_path: Path):
    svc = Services(root=tmp_path / "lib", settings_file=tmp_path / "settings.json", start_worker=False)
    client = TestClient(create_app(svc, token="t1"))
    headers = {"Authorization": "Bearer t1"}

    # Save direct API key via PUT /api/settings
    r1 = client.put(
        "/api/settings",
        headers=headers,
        json={
            "models": {
                "default": {
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4.1",
                    "api_key": "sk-secret-1234567890abcd",
                    "api_key_env": "OPENAI_API_KEY",
                    "vision": True,
                },
                "roles": {},
            }
        },
    )
    assert r1.status_code == 200
    masked = r1.json()["models"]["default"]["api_key"]
    assert masked == f"{MASK_PREFIX}abcd"
    assert "sk-secret" not in r1.text

    # Underlying saved settings hold the real key for LLM calls
    assert svc.get_settings().models.default.resolved_api_key() == "sk-secret-1234567890abcd"

    # Submitting masked value back preserves real key on disk
    cur_payload = client.get("/api/settings", headers=headers).json()
    cur_payload["supervisor"]["unattended"] = True
    r2 = client.put("/api/settings", headers=headers, json=cur_payload)
    assert r2.status_code == 200
    assert svc.get_settings().models.default.api_key == "sk-secret-1234567890abcd"
    assert svc.get_settings().supervisor.unattended is True

    # Clearing api_key clears it on disk
    cur_payload["models"]["default"]["api_key"] = ""
    r3 = client.put("/api/settings", headers=headers, json=cur_payload)
    assert r3.status_code == 200
    assert svc.get_settings().models.default.api_key == ""


def test_resolve_browser_falls_back_to_edge_or_chrome_when_chromium_missing(monkeypatch, tmp_path: Path):
    fake_edge = tmp_path / "msedge.exe"
    fake_edge.write_text("stub", encoding="utf-8")

    def no_chromium(pw=None):
        raise browser_session.BrowserLaunchError("no bundled chromium")

    monkeypatch.delenv("CUAFLOW_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.delenv("CUAFLOW_BROWSER_CHANNEL", raising=False)
    monkeypatch.setattr(browser_session, "_bundled_chromium", no_chromium)
    monkeypatch.setattr(
        browser_session,
        "find_channel_executable",
        lambda ch: str(fake_edge) if ch == "msedge" else None,
    )

    exe, ch = browser_session.resolve_browser_executable()
    assert exe == str(fake_edge)
    assert ch == "msedge"


def test_frozen_runner_client_uses_runner_proc_flag(monkeypatch, tmp_path: Path):
    captured: dict[str, object] = {}

    class DummyStdout:
        def __iter__(self):
            yield '{"id": 0, "ok": true, "result": {"ready": true}}\n'

    class DummyProc:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            self.stdout = DummyStdout()
            self.stdin = None

        def poll(self):
            return None

    import sys

    fake_exe = tmp_path / "cuaflow-desktop.exe"
    fake_exe.write_text("exe", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.setattr("subprocess.Popen", DummyProc)

    rc = RunnerClient(python=str(fake_exe))
    rc.start()
    assert captured["cmd"] == [str(fake_exe), "--runner-proc"]


def test_desktop_launcher_headless_lifecycle_and_single_instance_reuse(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CUAFLOW_DATA_DIR", str(tmp_path / "data"))
    port = _free_port()
    opened_urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened_urls.append(u) or True)

    # Start headless desktop launcher in background thread, then verify health + single-instance reuse
    import uvicorn

    created_servers: list[uvicorn.Server] = []
    orig_server = uvicorn.Server

    def track_server(cfg):
        srv = orig_server(cfg)
        created_servers.append(srv)
        return srv

    monkeypatch.setattr(uvicorn, "Server", track_server)

    t = threading.Thread(
        target=lambda: desktop_main(["--port", str(port), "--token", "dtoken", "--no-gui", "--no-browser"]),
        daemon=True,
    )
    t.start()

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 10.0
    healthy = False
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1.0) as resp:
                if b'"ok":true' in resp.read().replace(b" ", b""):
                    healthy = True
                    break
        except Exception:
            time.sleep(0.1)
    assert healthy, "desktop server failed to start"

    # Second launch on the same port detects running server, opens browser with token, and returns 0 immediately!
    rc2 = desktop_main(["--port", str(port), "--token", "dtoken", "--no-gui"])
    assert rc2 == 0
    assert opened_urls == [f"http://127.0.0.1:{port}/?token=dtoken"]

    for srv in created_servers:
        srv.should_exit = True
    t.join(timeout=5.0)
