"""平台层、工作流运行时与 CLI 工具共享的文件系统路径约定。

支持的环境变量：

* ``CUAFLOW_DATA_DIR``         —— 根数据目录（默认：``platformdirs.user_data_dir("cuaflow")``）
* ``CUAFLOW_BROWSER_SESSION``  —— 持久化浏览器会话 JSON 路径（默认：``<data_dir>/browser/session.json``）
* ``CUAFLOW_BROWSER_PROFILE``  —— 浏览器用户数据目录 user-data-dir（默认：``<会话文件所在目录>/profile``）
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_DATA_DIR = "CUAFLOW_DATA_DIR"
ENV_BROWSER_SESSION = "CUAFLOW_BROWSER_SESSION"
ENV_BROWSER_PROFILE = "CUAFLOW_BROWSER_PROFILE"


def data_dir(create: bool = True) -> Path:
    """返回 cuaflow 的根数据目录（优先读取环境变量 ``CUAFLOW_DATA_DIR``）。"""
    env = os.environ.get(ENV_DATA_DIR, "").strip()
    if env:
        path = Path(env).expanduser()
    else:
        from platformdirs import user_data_dir

        path = Path(user_data_dir("cuaflow", appauthor=False))
    path = path.resolve()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def browser_session_file() -> Path:
    """返回持久化浏览器的会话 JSON 文件路径（优先读取 ``CUAFLOW_BROWSER_SESSION``）。"""
    env = os.environ.get(ENV_BROWSER_SESSION, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return data_dir() / "browser" / "session.json"


def browser_profile_dir(session_file: str | Path | None = None) -> Path:
    """返回浏览器的用户数据目录路径（优先读取 ``CUAFLOW_BROWSER_PROFILE``；默认位于会话文件同级的 profile 目录）。"""
    env = os.environ.get(ENV_BROWSER_PROFILE, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    sf = Path(session_file) if session_file else browser_session_file()
    return sf.resolve().parent / "profile"


def screenshots_dir() -> Path:
    path = data_dir() / "screenshots"
    path.mkdir(parents=True, exist_ok=True)
    return path
