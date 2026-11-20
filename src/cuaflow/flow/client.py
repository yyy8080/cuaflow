"""Parent-side client for the Runner child process."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from cuaflow.platform.base import IS_WINDOWS


class RunnerError(RuntimeError):
    def __init__(self, message: str, tb: str | None = None):
        super().__init__(message)
        self.tb = tb


class RunnerTimeout(RunnerError):
    pass


class RunnerDied(RunnerError):
    pass


class RunnerClient:
    """Start/drive/restart a ``cuaflow.flow.proc`` child.

    ``load_args`` (flow_path, params, run_dir, desktop, browser_session, headless) are remembered
    so that :meth:`restart` can bring a crashed / timed-out runner back with the last checkpointed
    state. Browser and desktop state survive restarts (detached browser, real OS windows).
    """

    def __init__(self, log_path: str | Path | None = None, env: dict[str, str] | None = None,
                 python: str | None = None):
        self.log_path = Path(log_path) if log_path else None
        self.env = env or {}
        self.python = python or sys.executable
        self.proc: subprocess.Popen | None = None
        self._responses: queue.Queue = queue.Queue()
        self._next_id = 0
        self._lock = threading.Lock()
        self.load_args: dict[str, Any] | None = None
        self.restarts = 0

    # ------------------------------------------------------------------
    def start(self) -> None:
        env = {**os.environ, **self.env, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        src_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = src_root + os.pathsep + env.get("PYTHONPATH", "")
        # UTF-8 mode: flow code using open()/read_text() without encoding must not depend on the
        # Windows ANSI code page (cp936/cp1252); also makes the JSON-RPC pipe encoding deterministic.
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        kwargs: dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        log = open(self.log_path, "a", encoding="utf-8") if self.log_path else subprocess.DEVNULL  # noqa: SIM115
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [self.python, "-u", "-m", "cuaflow.flow.proc"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            env=env, text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs)
        self._responses = queue.Queue()
        threading.Thread(target=self._reader, args=(self.proc, self._responses), daemon=True).start()
        ready = self._wait(0, timeout=60)
        if not ready.get("ok"):
            raise RunnerError("runner failed to start")

    def _reader(self, proc: subprocess.Popen, q: queue.Queue) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                q.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        q.put({"__eof__": True})

    def _wait(self, rid: int, timeout: float) -> dict[str, Any]:
        while True:
            try:
                msg = self._responses.get(timeout=timeout)
            except queue.Empty:
                raise RunnerTimeout(f"runner did not answer within {timeout:.0f}s") from None
            if msg.get("__eof__"):
                raise RunnerDied(f"runner process exited (code {self.proc.poll() if self.proc else None})")
            if msg.get("id") == rid:
                return msg

    def call(self, cmd: str, timeout: float = 120, **args: Any) -> Any:
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RunnerDied("runner is not running")
            self._next_id += 1
            rid = self._next_id
            assert self.proc.stdin is not None
            try:
                self.proc.stdin.write(json.dumps({"id": rid, "cmd": cmd, "args": args}, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RunnerDied(f"runner pipe closed: {exc}") from None
            msg = self._wait(rid, timeout)
        if not msg.get("ok"):
            raise RunnerError(msg.get("error", "runner error"), msg.get("traceback"))
        return msg.get("result")

    # ------------------------------------------------------------------
    def load(self, flow_path: str | Path, params: dict[str, Any], run_dir: str | Path,
             state: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        self.load_args = {"flow_path": str(flow_path), "params": params, "run_dir": str(run_dir), **extra}
        return self.call("load", timeout=120, state=state, **self.load_args)

    def run_step(self, step_id: str, timeout: float = 120) -> dict[str, Any]:
        return self.call("run_step", timeout=timeout + 30, step_id=step_id)

    def replace_step(self, source: str, expected_id: str | None = None) -> dict[str, Any]:
        return self.call("replace_step", timeout=30, source=source, expected_id=expected_id)

    def exec(self, code: str, timeout: float = 120) -> dict[str, Any]:
        return self.call("exec", timeout=timeout, code=code)

    def observe(self, detail: bool = True, tag: str = "obs", surface: str | None = None) -> dict[str, Any]:
        return self.call("observe", timeout=60, detail=detail, tag=tag, surface=surface)

    def check_pre(self, step_id: str) -> dict[str, Any]:
        return self.call("check_pre", timeout=60, step_id=step_id)

    def heal_browser(self, keep_tabs: int = 6) -> dict[str, Any]:
        return self.call("heal_browser", timeout=30, keep_tabs=keep_tabs)

    def update_output(self, updates: dict[str, Any]) -> dict[str, Any]:
        return self.call("update_output", timeout=15, updates=updates)

    def finish(self) -> dict[str, Any]:
        return self.call("finish", timeout=30)

    def restart(self, state: dict[str, Any] | None = None, flow_path: str | Path | None = None) -> None:
        """Kill and relaunch the runner, reloading the flow with ``state``."""
        self.kill()
        self.restarts += 1
        self.start()
        if self.load_args is not None:
            args = dict(self.load_args)
            if flow_path is not None:
                args["flow_path"] = str(flow_path)
                self.load_args["flow_path"] = str(flow_path)
            self.call("load", timeout=120, state=state, **args)

    def kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.call("shutdown", timeout=15)
                self.proc.wait(timeout=10)
            except (RunnerError, subprocess.TimeoutExpired):
                pass
        self.kill()

    def __enter__(self) -> "RunnerClient":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
