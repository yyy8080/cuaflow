"""轻量级跨平台进程辅助函数（PID 存活检测、进程树终止、分离式子进程启动）。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"

# Windows 进程创建标志
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def pid_alive(pid: int) -> bool:
    """若 ``pid`` 指向正在运行的非僵尸进程则返回 True。"""
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
            # 拒绝访问（Access Denied）同样说明该进程仍然存在。
            return ctypes.GetLastError() == 5  # Win32 错误码 ERROR_ACCESS_DENIED（拒绝访问）
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
    # 僵尸进程虽然会响应 kill(0)，但对当前业务而言已视为死亡。
    stat = Path(f"/proc/{pid}/stat")
    try:
        if stat.exists():
            state = stat.read_text().rsplit(")", 1)[1].split()[0]
            if state in ("Z", "X"):
                try:  # 若为当前进程的子进程则尝试回收
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    pass
                return False
    except (OSError, IndexError):
        pass
    return True


def process_cmdline(pid: int) -> str:
    """尽力获取 ``pid`` 对应的命令行字符串（未知时返回空字符串 ""）。"""
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
    """终止 ``pid`` 及其所有子孙进程。

    在 Windows 上使用 ``taskkill /T [/F] /PID``；在 POSIX 系统上，当 ``pid`` 为进程组组长
    （即 ``start_new_session=True``）时向整个进程组发送信号，否则仅向该 ``pid`` 发送信号。
    若 ``graceful_timeout`` > 0，会先尝试温和终止并在超时后强制杀死。
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
    """构造使子进程与父进程分离（父进程退出后仍可存活）的 Popen 关键字参数。"""
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
        if IS_WINDOWS:  # 父进程所在的作业对象（Job Object）可能拒绝脱离权限，此时降级重试
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            return subprocess.Popen(args, **kwargs)  # noqa: S603
        raise
