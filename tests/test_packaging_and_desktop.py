"""针对桌面启动器、API Key 脱敏保存、系统浏览器自动回退以及打包态 --runner-proc 子进程分发的测试。"""

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

    # 通过 PUT /api/settings 直接保存明文 API Key
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

    # 底层落盘配置中保留真实密钥以供大模型调用
    assert svc.get_settings().models.default.resolved_api_key() == "sk-secret-1234567890abcd"

    # 回传脱敏后的掩码值时，磁盘上的真实密钥保持不变
    cur_payload = client.get("/api/settings", headers=headers).json()
    cur_payload["supervisor"]["unattended"] = True
    r2 = client.put("/api/settings", headers=headers, json=cur_payload)
    assert r2.status_code == 200
    assert svc.get_settings().models.default.api_key == "sk-secret-1234567890abcd"
    assert svc.get_settings().supervisor.unattended is True

    # 清空 api_key 字段时会同步清空磁盘上的密钥
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

    # 在后台线程启动无界面桌面启动器，验证健康检查与单实例复用逻辑
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

    # 在同一端口二次启动时会检测到已运行的服务端，直接携带令牌打开浏览器并立即返回 0！
    rc2 = desktop_main(["--port", str(port), "--token", "dtoken", "--no-gui"])
    assert rc2 == 0
    assert opened_urls == [f"http://127.0.0.1:{port}/?token=dtoken"]

    for srv in created_servers:
        srv.should_exit = True
    t.join(timeout=5.0)


def test_semver_comparison_and_update_manager_windows_installer_flow(monkeypatch, tmp_path: Path):
    """验证版本号语义比对、GitHub Releases 检测、SHA-256 完整性校验以及 Windows 安装包静默自替换脚本生成。"""
    import hashlib
    import httpx
    from cuaflow.updater import UpdateManager, is_newer_version

    assert is_newer_version("v0.2.2", "0.2.1") is True
    assert is_newer_version("0.10.0", "0.2.9") is True
    assert is_newer_version("v0.2.2", "0.2.2") is False
    assert is_newer_version("0.2.2-rc1", "0.2.2") is False

    installer_bytes = b"MZ-fake-inno-setup-payload-0.3.0"
    good_sha = hashlib.sha256(installer_bytes).hexdigest()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/releases/latest"):
            return httpx.Response(
                200,
                json={
                    "tag_name": "v0.3.0",
                    "html_url": "https://github.com/yyy8080/cuaflow/releases/tag/v0.3.0",
                    "body": "### 更新说明\n- 新增版本自动检测与一键更新",
                    "published_at": "2026-11-20T08:00:00Z",
                    "assets": [
                        {
                            "name": "cuaflow-setup-0.3.0-win64.exe",
                            "size": len(installer_bytes),
                            "browser_download_url": "https://downloads.example.com/cuaflow-setup-0.3.0-win64.exe",
                        },
                        {
                            "name": "SHA256SUMS.txt",
                            "size": 90,
                            "browser_download_url": "https://downloads.example.com/SHA256SUMS.txt",
                        },
                    ],
                },
            )
        if req.url.path.endswith("cuaflow-setup-0.3.0-win64.exe"):
            return httpx.Response(200, content=installer_bytes, headers={"Content-Length": str(len(installer_bytes))})
        if req.url.path.endswith("SHA256SUMS.txt"):
            return httpx.Response(200, text=f"{good_sha}  cuaflow-setup-0.3.0-win64.exe\n")
        return httpx.Response(404)

    detached_calls: list[list[str]] = []
    shutdown_called = threading.Event()

    fake_install_dir = tmp_path / "installed_app"
    fake_install_dir.mkdir(parents=True)
    fake_exe = fake_install_dir / "cuaflow-desktop.exe"
    fake_exe.write_text("exe", encoding="utf-8")
    (fake_install_dir / "unins000.exe").write_text("unins", encoding="utf-8")

    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.setattr("cuaflow.updater.spawn_detached", lambda cmd, **kw: detached_calls.append(list(cmd)))

    mgr = UpdateManager(
        current_version="0.2.2",
        updates_dir=tmp_path / "updates",
        shutdown_callback=shutdown_called.set,
        http_transport=httpx.MockTransport(handler),
    )
    assert mgr.status_dict()["install_mode"] == "windows_installer"

    checked = mgr.check_for_updates(force=True)
    assert checked["has_update"] is True
    assert checked["latest_version"] == "0.3.0"
    assert checked["asset_name"] == "cuaflow-setup-0.3.0-win64.exe"
    assert checked["can_self_update"] is True

    applied = mgr.start_apply_update(async_run=False)
    assert applied["status"] == "restarting"
    assert (tmp_path / "updates" / "cuaflow-setup-0.3.0-win64.exe").read_bytes() == installer_bytes
    ps1 = (tmp_path / "updates" / "apply_update.ps1").read_text(encoding="utf-8-sig")
    assert "/VERYSILENT" in ps1
    assert str(fake_exe) in ps1
    assert len(detached_calls) == 1
    assert detached_calls[0][0] == "powershell.exe"
    assert shutdown_called.wait(timeout=3.0)


def test_update_api_endpoints(tmp_path: Path):
    """验证服务端 /api/update/status 与 /api/update/check 接口鉴权与返回结构。"""
    import httpx
    from cuaflow.updater import UpdateManager

    svc = Services(root=tmp_path / "lib", settings_file=tmp_path / "settings.json", start_worker=False)
    svc.updater = UpdateManager(
        svc.bus,
        current_version="0.2.2",
        updates_dir=tmp_path / "updates",
        http_transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"tag_name": "v0.2.2", "assets": [], "body": "up to date"})
        ),
    )
    try:
        client = TestClient(create_app(svc, token="utok"))
        headers = {"Authorization": "Bearer utok"}

        r0 = client.get("/api/update/status", headers=headers)
        assert r0.status_code == 200
        assert r0.json()["current_version"] == "0.2.2"

        r1 = client.post("/api/update/check", headers=headers)
        assert r1.status_code == 200
        assert r1.json()["status"] == "up_to_date"
        assert r1.json()["has_update"] is False
    finally:
        svc.close()
