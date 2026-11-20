"""Small cross-platform process helpers (pid liveness, process-tree kill, detached spawn)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"

# Windows creation flags
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def pid_alive(pid: int) -> bool:
    """True if ``pid`` refers to a running (non-zombie) process."""
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            # Access denied still means the process exists.
            return ctypes.GetLastError() == 5  # ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Zombies answer kill(0) but are dead for our purposes.
    stat = Path(f"/proc/{pid}/stat")
    try:
        if stat.exists():
            state = stat.read_text().rsplit(")", 1)[1].split()[0]
            if state in ("Z", "X"):
                try:  # reap if it is our child
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    pass
                return False
    except (OSError, IndexError):
        pass
    return True


def process_cmdline(pid: int) -> str:
    """Best-effort command line of ``pid`` ("" if unknown)."""
    try:
        import psutil  # type: ignore

        return " ".join(psutil.Process(pid).cmdline())
    except Exception:  # noqa: BLE001
        pass
    p = Path(f"/proc/{pid}/cmdline")
    try:
        return p.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def kill_tree(pid: int, *, graceful_timeout: float = 0.0) -> None:
    """Kill ``pid`` and all of its descendants.

    On Windows uses ``taskkill /T [/F] /PID``; on POSIX signals the process group when ``pid``
    leads its own group (``start_new_session=True``), otherwise just the pid.
    If ``graceful_timeout`` > 0 a polite termination is attempted first.
    """
    if not pid or pid <= 0:
        return
    if IS_WINDOWS:
        flags = CREATE_NO_WINDOW
        if graceful_timeout > 0:
            subprocess.run(["taskkill", "/T", "/PID", str(pid)], capture_output=True, creationflags=flags)
            if _wait_dead(pid, graceful_timeout):
                return
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=flags)
        return

    def _signal(sig: int) -> None:
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, OSError):
            return
        try:
            if pgid == pid:
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    if graceful_timeout > 0:
        _signal(signal.SIGTERM)
        if _wait_dead(pid, graceful_timeout):
            return
    _signal(signal.SIGKILL)
    _wait_dead(pid, 2.0)


def _wait_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def detached_popen_kwargs() -> dict[str, Any]:
    """Popen kwargs that detach the child so it survives the parent process."""
    if IS_WINDOWS:
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB}
    return {"start_new_session": True}


def spawn_detached(args: list[str], *, stdout: Any = subprocess.DEVNULL, stderr: Any = subprocess.DEVNULL,
                   cwd: str | None = None, env: dict[str, str] | None = None) -> subprocess.Popen:
    kwargs: dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, cwd=cwd, env=env,
                                  close_fds=True)
    kwargs.update(detached_popen_kwargs())
    try:
        return subprocess.Popen(args, **kwargs)  # noqa: S603
    except OSError:
        if IS_WINDOWS:  # breakaway may be denied by the parent job object
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            return subprocess.Popen(args, **kwargs)  # noqa: S603
        raise
