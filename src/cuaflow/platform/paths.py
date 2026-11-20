"""Filesystem locations shared by the platform layer, the flow runtime and the CLI tools.

Environment variables:

* ``CUAFLOW_DATA_DIR``         – root data directory (default: ``platformdirs.user_data_dir("cuaflow")``)
* ``CUAFLOW_BROWSER_SESSION``  – persistent-browser session JSON (default: ``<data_dir>/browser/session.json``)
* ``CUAFLOW_BROWSER_PROFILE``  – browser user-data-dir (default: ``<session file dir>/profile``)
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_DATA_DIR = "CUAFLOW_DATA_DIR"
ENV_BROWSER_SESSION = "CUAFLOW_BROWSER_SESSION"
ENV_BROWSER_PROFILE = "CUAFLOW_BROWSER_PROFILE"


def data_dir(create: bool = True) -> Path:
    """Root data directory for cuaflow (honors ``CUAFLOW_DATA_DIR``)."""
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
    """Session JSON of the persistent browser (honors ``CUAFLOW_BROWSER_SESSION``)."""
    env = os.environ.get(ENV_BROWSER_SESSION, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return data_dir() / "browser" / "session.json"


def browser_profile_dir(session_file: str | Path | None = None) -> Path:
    """Browser user-data-dir (honors ``CUAFLOW_BROWSER_PROFILE``; defaults next to the session file)."""
    env = os.environ.get(ENV_BROWSER_PROFILE, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    sf = Path(session_file) if session_file else browser_session_file()
    return sf.resolve().parent / "profile"


def screenshots_dir() -> Path:
    path = data_dir() / "screenshots"
    path.mkdir(parents=True, exist_ok=True)
    return path
