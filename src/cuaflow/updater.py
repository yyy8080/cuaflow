"""版本检测与在线自更新管理器 `UpdateManager`。

核心能力：
1. 向 GitHub Releases API 查询最新发布版本，支持语义化版本号比对与本地结果缓存。
2. 自动识别当前运行形态：
   - `windows_installer`：Inno Setup 安装版（下载 `cuaflow-setup-*-win64.exe`，校验 SHA-256，拉起独立 PowerShell 守护脚本静默覆盖安装并自动重启界面）；
   - `windows_portable`：Windows 绿色便携版（下载 `cuaflow-portable-*-win64.zip`，校验 SHA-256，退出后解压覆盖并自动重启）；
   - `git_source`：Git 源码开发目录（执行 `git pull --ff-only` 拉取最新代码）；
   - `pip_package`：Python 包安装环境（通过 `pip install --upgrade` 从发布标签更新）。
3. 下载过程实时通过 `EventBus` 推送字节进度与百分比，供单窗口桌面 WebUI 展示进度条。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from cuaflow import __version__
from cuaflow.library.events import EventBus
from cuaflow.platform._proc import spawn_detached
from cuaflow.platform.paths import data_dir

DEFAULT_GITHUB_REPO = "yyy8080/cuaflow"
_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+.]?(.*))?$", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_version_key(ver: str) -> tuple[int, int, int, int, str]:
    """将形如 `v0.2.2` 或 `0.2.2-rc1` 的版本号解析为可比较的元组（正式版高于同号预发布版）。"""
    cleaned = (ver or "").strip()
    m = _SEMVER_RE.match(cleaned)
    if not m:
        nums = [int(x) for x in re.findall(r"\d+", cleaned)[:3]]
        while len(nums) < 3:
            nums.append(0)
        return (nums[0], nums[1], nums[2], 1, "")
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    suffix = (m.group(4) or "").strip()
    is_stable = 1 if not suffix else 0
    return (major, minor, patch, is_stable, suffix)


def is_newer_version(candidate: str, current: str = __version__) -> bool:
    """判断 `candidate` 版本号是否严格高于 `current`。"""
    if not (candidate or "").strip():
        return False
    return parse_version_key(candidate) > parse_version_key(current)


def detect_install_mode(exe_path: Path | None = None, repo_hint: Path | None = None) -> str:
    """识别当前 cuaflow 实例的安装运行模式。"""
    if getattr(sys, "frozen", False):
        exe = (exe_path or Path(sys.executable)).resolve()
        app_dir = exe.parent
        if sys.platform == "win32":
            if (app_dir / "unins000.exe").is_file():
                return "windows_installer"
            return "windows_portable"
        return "frozen_binary"

    src_root = (repo_hint or Path(__file__).resolve().parents[2]).resolve()
    if (src_root / ".git").exists():
        return "git_source"
    return "pip_package"


def _select_release_asset(assets: list[dict[str, Any]], install_mode: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """根据安装模式挑选最匹配的更新包资产以及可选的 SHA256SUMS.txt 校验文件。"""
    sha_asset = next((a for a in assets if str(a.get("name") or "") == "SHA256SUMS.txt"), None)
    if install_mode == "windows_installer":
        for a in assets:
            name = str(a.get("name") or "").lower()
            if name.startswith("cuaflow-setup-") and name.endswith(".exe"):
                return a, sha_asset
    elif install_mode == "windows_portable":
        for a in assets:
            name = str(a.get("name") or "").lower()
            if name.startswith("cuaflow-portable-") and name.endswith(".zip"):
                return a, sha_asset
    # 兜底：若在 Windows 源码或 pip 环境也提供安装包信息供下载跳转
    if sys.platform == "win32":
        for a in assets:
            name = str(a.get("name") or "").lower()
            if name.startswith("cuaflow-setup-") and name.endswith(".exe"):
                return a, sha_asset
    return None, sha_asset


def _parse_sha256_manifest(text: str, target_filename: str) -> str | None:
    """从 `SHA256SUMS.txt` 内容中提取指定文件名的预期 SHA-256 哈希值。"""
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            digest, fname = parts[0].lower(), parts[-1].lstrip("*")
            if fname == target_filename and re.fullmatch(r"[0-9a-f]{64}", digest):
                return digest
    return None


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 256)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().lower()


class UpdateManager:
    """线程安全的版本检查与在线自更新协调器。"""

    def __init__(
        self,
        bus: EventBus | None = None,
        *,
        repo: str | None = None,
        current_version: str = __version__,
        updates_dir: Path | None = None,
        shutdown_callback: Callable[[], None] | None = None,
        http_transport: httpx.BaseTransport | None = None,
    ):
        self.bus = bus
        self.repo = (repo or os.environ.get("CUAFLOW_GITHUB_REPO") or DEFAULT_GITHUB_REPO).strip()
        self.current_version = current_version
        self.updates_dir = Path(updates_dir) if updates_dir else (data_dir() / "updates")
        self.shutdown_callback = shutdown_callback
        self._transport = http_transport
        self._lock = threading.RLock()
        self._last_check_mono: float = 0.0
        self._apply_thread: threading.Thread | None = None

        mode = detect_install_mode()
        self._state: dict[str, Any] = {
            "current_version": self.current_version,
            "latest_version": self.current_version,
            "tag_name": f"v{self.current_version}",
            "has_update": False,
            "status": "idle",
            "install_mode": mode,
            "can_self_update": mode in ("windows_installer", "windows_portable", "git_source", "pip_package"),
            "repo": self.repo,
            "release_url": f"https://github.com/{self.repo}/releases",
            "release_notes": "",
            "published_at": None,
            "checked_at": None,
            "asset_name": None,
            "asset_url": None,
            "asset_size": 0,
            "sha256_url": None,
            "downloaded_bytes": 0,
            "progress_pct": 0,
            "message": "",
            "error": None,
        }

    def set_shutdown_callback(self, fn: Callable[[], None] | None) -> None:
        with self._lock:
            self.shutdown_callback = fn

    def status_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _update_state(self, publish: bool = True, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._state.update(fields)
            snap = dict(self._state)
        if publish and self.bus is not None:
            self.bus.publish("update_status", snap)
        return snap

    def _client(self, timeout: float = 15.0) -> httpx.Client:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"cuaflow/{self.current_version}",
        }
        return httpx.Client(
            headers=headers,
            follow_redirects=True,
            timeout=timeout,
            transport=self._transport,
        )

    # ------------------------------------------------------------------ 检测远端最新版本
    def check_for_updates(self, *, force: bool = True, min_interval: float = 1800.0) -> dict[str, Any]:
        """请求 GitHub Releases `/releases/latest` 检测是否存在高于当前版本的发行包。"""
        with self._lock:
            if self._state["status"] in ("downloading", "verifying", "installing", "restarting"):
                return dict(self._state)
            if (
                not force
                and self._last_check_mono > 0
                and (time.monotonic() - self._last_check_mono) < min_interval
            ):
                return dict(self._state)
            self._update_state(status="checking", error=None, message="正在检查 GitHub 最新版本…")

        api_url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        try:
            with self._client(timeout=12.0) as client:
                resp = client.get(api_url)
                if resp.status_code == 404:
                    self._last_check_mono = time.monotonic()
                    return self._update_state(
                        status="up_to_date",
                        has_update=False,
                        checked_at=now_iso(),
                        message=f"当前已是最新版本（v{self.current_version}）",
                    )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return self._update_state(
                status="error",
                checked_at=now_iso(),
                error=f"检查更新失败：{exc}",
                message="无法连接更新服务器，请检查网络或稍后重试。",
            )

        tag_name = str(data.get("tag_name") or "").strip()
        latest_ver = tag_name.lstrip("vV") or self.current_version
        newer = is_newer_version(latest_ver, self.current_version)
        assets = data.get("assets") if isinstance(data.get("assets"), list) else []
        mode = self._state["install_mode"]
        asset, sha_asset = _select_release_asset(assets, mode)

        can_auto = mode in ("git_source", "pip_package") or (
            mode in ("windows_installer", "windows_portable") and asset is not None
        )
        msg = (
            f"发现新版本 v{latest_ver}（当前 v{self.current_version}），可立即一键更新！"
            if newer
            else f"当前已是最新版本（v{self.current_version}）"
        )
        self._last_check_mono = time.monotonic()
        return self._update_state(
            latest_version=latest_ver,
            tag_name=tag_name or f"v{latest_ver}",
            has_update=newer,
            status="available" if newer else "up_to_date",
            can_self_update=can_auto,
            release_url=str(data.get("html_url") or f"https://github.com/{self.repo}/releases"),
            release_notes=str(data.get("body") or "").strip(),
            published_at=data.get("published_at"),
            checked_at=now_iso(),
            asset_name=asset.get("name") if asset else None,
            asset_url=asset.get("browser_download_url") if asset else None,
            asset_size=int(asset.get("size") or 0) if asset else 0,
            sha256_url=sha_asset.get("browser_download_url") if sha_asset else None,
            downloaded_bytes=0,
            progress_pct=0,
            message=msg,
            error=None,
        )

    # ------------------------------------------------------------------ 执行下载与自更新
    def start_apply_update(self, *, async_run: bool = True) -> dict[str, Any]:
        """启动在线下载与自动更新流程（默认后台异步执行并实时推送进度）。"""
        with self._lock:
            if self._state["status"] in ("downloading", "verifying", "installing", "restarting"):
                return dict(self._state)
        snap = self.status_dict()
        if not snap.get("checked_at"):
            snap = self.check_for_updates(force=True)
        if not snap.get("has_update"):
            return self._update_state(status="up_to_date", message=f"当前已是最新版本（v{self.current_version}），无需更新。")

        if not async_run:
            return self._perform_update()

        t = threading.Thread(target=self._perform_update, name="cuaflow-self-updater", daemon=True)
        self._apply_thread = t
        t.start()
        return self._update_state(status="downloading", progress_pct=1, message="正在准备下载最新版本更新包…")

    def _perform_update(self) -> dict[str, Any]:
        snap = self.status_dict()
        mode = snap["install_mode"]
        latest_ver = snap["latest_version"]
        tag_name = snap["tag_name"]

        try:
            if mode in ("windows_installer", "windows_portable"):
                return self._apply_windows_bundle_update(snap)
            if mode == "git_source":
                return self._apply_git_source_update(tag_name, latest_ver)
            if mode == "pip_package":
                return self._apply_pip_update(tag_name, latest_ver)
            return self._update_state(
                status="error",
                error=f"当前运行模式（{mode}）不支持自动替换，请前往发布页手动下载。",
                message="请点击「前往下载页」获取最新安装包。",
            )
        except Exception as exc:  # noqa: BLE001
            return self._update_state(
                status="error",
                error=f"自动更新失败：{exc}",
                message=f"更新过程中遇到错误：{exc}",
            )

    def _download_with_progress(self, url: str, dest: Path, expected_size: int = 0) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        downloaded = 0
        last_emit = 0.0
        with self._client(timeout=120.0) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or expected_size or 0)
                with part.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 128):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last_emit >= 0.18 or (total and downloaded >= total):
                            last_emit = now
                            pct = min(99, int(downloaded * 100 / total)) if total > 0 else 50
                            mb_done = downloaded / (1024 * 1024)
                            mb_total = total / (1024 * 1024) if total else 0
                            size_txt = f"{mb_done:.1f} / {mb_total:.1f} MB" if total else f"{mb_done:.1f} MB"
                            self._update_state(
                                status="downloading",
                                downloaded_bytes=downloaded,
                                asset_size=total or expected_size,
                                progress_pct=pct,
                                message=f"正在下载更新包 {dest.name}（{size_txt}，{pct}%）…",
                            )
        part.replace(dest)
        return dest

    def _verify_checksum_if_available(self, asset_path: Path, sha256_url: str | None) -> None:
        if not sha256_url:
            return
        self._update_state(status="verifying", progress_pct=99, message="正在校验安装包 SHA-256 完整性…")
        with self._client(timeout=20.0) as client:
            r = client.get(sha256_url)
            if r.status_code != 200:
                return
            expected = _parse_sha256_manifest(r.text, asset_path.name)
        if expected:
            actual = _file_sha256(asset_path)
            if actual != expected:
                asset_path.unlink(missing_ok=True)
                raise RuntimeError(f"安装包 SHA-256 校验不匹配（预期 {expected[:12]}…，实际 {actual[:12]}…）")

    def _apply_windows_bundle_update(self, snap: dict[str, Any]) -> dict[str, Any]:
        asset_url = snap.get("asset_url")
        asset_name = snap.get("asset_name")
        if not asset_url or not asset_name:
            raise RuntimeError("未在最新 Release 中找到对应的 Windows 安装包资产")

        target_file = self.updates_dir / str(asset_name)
        self._download_with_progress(str(asset_url), target_file, int(snap.get("asset_size") or 0))
        self._verify_checksum_if_available(target_file, snap.get("sha256_url"))

        self._update_state(status="installing", progress_pct=100, message="下载校验完成，正在启动无缝覆盖更新并重启应用…")

        exe_path = Path(sys.executable).resolve()
        install_dir = exe_path.parent
        desktop_exe = install_dir / "cuaflow-desktop.exe"
        relaunch_exe = desktop_exe if desktop_exe.is_file() else exe_path
        ps1_path = self.updates_dir / "apply_update.ps1"

        if snap["install_mode"] == "windows_installer":
            ps1_lines = [
                "$ErrorActionPreference = 'SilentlyContinue'",
                f"$OldPid = {os.getpid()}",
                "Wait-Process -Id $OldPid -Timeout 15 -ErrorAction SilentlyContinue",
                "Stop-Process -Id $OldPid -Force -ErrorAction SilentlyContinue",
                "Start-Sleep -Milliseconds 600",
                f'$Setup = "{target_file}"',
                f'$Dir = "{install_dir}"',
                'Start-Process -FilePath $Setup -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/DIR=`"$Dir`"") -Wait',
                f'Start-Process -FilePath "{relaunch_exe}"',
            ]
        else:
            ps1_lines = [
                "$ErrorActionPreference = 'SilentlyContinue'",
                f"$OldPid = {os.getpid()}",
                "Wait-Process -Id $OldPid -Timeout 15 -ErrorAction SilentlyContinue",
                "Stop-Process -Id $OldPid -Force -ErrorAction SilentlyContinue",
                "Start-Sleep -Milliseconds 600",
                f'Expand-Archive -Path "{target_file}" -DestinationPath "{install_dir}" -Force',
                f'Start-Process -FilePath "{relaunch_exe}"',
            ]

        ps1_path.write_text("\n".join(ps1_lines) + "\n", encoding="utf-8-sig")
        spawn_detached(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(ps1_path),
            ]
        )

        final_state = self._update_state(
            status="restarting",
            progress_pct=100,
            message=f"v{snap['latest_version']} 安装程序已接管，窗口即将自动关闭并在几秒内重新启动新版本…",
        )
        cb = self.shutdown_callback
        if cb is not None:
            threading.Timer(0.8, cb).start()
        return final_state

    def _apply_git_source_update(self, tag_name: str, latest_ver: str) -> dict[str, Any]:
        repo_dir = Path(__file__).resolve().parents[2]
        self._update_state(status="installing", progress_pct=55, message="正在通过 git 拉取最新源码…")
        res = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout or "git pull failed").strip()[:300])
        self._update_state(status="installing", progress_pct=85, message="正在同步本地 Python 包环境…")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".", "--no-deps"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=90,
        )
        return self._update_state(
            status="done",
            has_update=False,
            current_version=latest_ver,
            progress_pct=100,
            message=f"已成功拉取最新源码 v{latest_ver}，重启 cuaflow 服务后全面生效。",
        )

    def _apply_pip_update(self, tag_name: str, latest_ver: str) -> dict[str, Any]:
        self._update_state(status="installing", progress_pct=60, message=f"正在通过 pip 升级至 {tag_name}…")
        pkg_url = f"git+https://github.com/{self.repo}.git@{tag_name}"
        res = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", pkg_url],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout or "pip upgrade failed").strip()[:300])
        return self._update_state(
            status="done",
            has_update=False,
            current_version=latest_ver,
            progress_pct=100,
            message=f"已成功升级至 v{latest_ver}，重启 cuaflow 后生效。",
        )
