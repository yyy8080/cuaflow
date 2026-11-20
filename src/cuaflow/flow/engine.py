"""In-process flow engine: executes single steps against a live Ctx and records checkpoints.

The engine runs inside the Runner child process (see :mod:`cuaflow.flow.proc`) and is driven
step-by-step by a controller (Supervisor / Author) in the parent process. Because browser
pages, windows and ``c.state`` stay alive in the child between commands, a failing step can be
patched (``replace_step``) and retried without restarting the flow.
"""

from __future__ import annotations

import contextlib
import io
import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cuaflow.flow import expect as expect_mod
from cuaflow.flow.ctx import Ctx
from cuaflow.flow.expect import ExpectationFailed
from cuaflow.flow.flow import Flow, load_flow
from cuaflow.flow.patching import validate_step_source


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class _Tee(io.TextIOBase):
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):  # type: ignore[override]
        for st in self.streams:
            st.write(s)
        return len(s)

    def flush(self):  # type: ignore[override]
        for st in self.streams:
            st.flush()


def _short_tb(exc: BaseException, limit: int = 6000) -> str:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return tb[-limit:]


class FlowEngine:
    def __init__(self, flow_path: str | Path, params: dict[str, Any], run_dir: str | Path,
                 state: dict[str, Any] | None = None, ctx_factory_kwargs: dict[str, Any] | None = None):
        self.flow_path = Path(flow_path)
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.flow, self.ns = load_flow(self.flow_path) if self.flow_path.exists() else (None, {})
        if self.flow is None:
            raise FileNotFoundError(self.flow_path)
        self.params = self.flow.coerce_params(params)
        self.ctx = Ctx(self.params, self.run_dir, state=state, **(ctx_factory_kwargs or {}))
        self.attempts: dict[str, int] = {}
        self._checkpoints = self.run_dir / "steps.jsonl"

    # ------------------------------------------------------------------
    @property
    def steps(self) -> list[dict[str, Any]]:
        return [s.spec() for s in self.flow.steps]

    def describe(self) -> dict[str, Any]:
        return {**self.flow.meta(), "params": self.params}

    # ------------------------------------------------------------------
    def check_pre(self, step_id: str) -> dict[str, Any]:
        step = self.flow.get(step_id)
        if step.pre is None:
            return {"ok": True, "detail": "no precondition"}
        try:
            ok = bool(step.pre(self.ctx))
            return {"ok": ok, "detail": "precondition satisfied" if ok else "precondition returned False"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"precondition raised {type(exc).__name__}: {exc}"}

    def run_step(self, step_id: str, observe: bool = True) -> dict[str, Any]:
        step = self.flow.get(step_id)
        self.attempts[step_id] = self.attempts.get(step_id, 0) + 1
        c = self.ctx
        c.current_step = step_id
        c.surfaces_used = set()
        before_modals = self._modal_titles()
        buf = io.StringIO()
        started = now_iso()
        t0 = time.monotonic()
        status, error_kind, error, tb = "passed", None, None, None
        log_path = self.run_dir / "flow_log.txt"
        with log_path.open("a", encoding="utf-8") as logf:
            logf.write(f"[{time.strftime('%H:%M:%S')}] ==== step {step_id} (attempt {self.attempts[step_id]}) ====\n")
            logf.flush()
            tee = _Tee(buf, logf)
            try:
                with contextlib.redirect_stdout(tee):
                    step.func(c)
            except ExpectationFailed as exc:
                status, error_kind, error, tb = "failed", "expectation", str(exc), _short_tb(exc)
            except Exception as exc:  # noqa: BLE001
                status, error_kind, error, tb = "failed", "exception", f"{type(exc).__name__}: {exc}", _short_tb(exc)
        duration_ms = int((time.monotonic() - t0) * 1000)
        result: dict[str, Any] = {
            "step_id": step_id,
            "index": self.flow.index(step_id),
            "title": step.title,
            "expect": step.expect,
            "status": status,
            "attempt": self.attempts[step_id],
            "started_at": started,
            "ended_at": now_iso(),
            "duration_ms": duration_ms,
            "error_kind": error_kind,
            "error": error,
            "traceback": tb,
            "stdout": buf.getvalue()[-8000:],
            "surfaces": sorted(c.surfaces_used),
            "output": json.loads(json.dumps(c.output, default=str, ensure_ascii=False)),
        }
        if observe:
            obs = self.observation(surface=self._surface_for(step), tag=f"{status}")
            after_modals = obs.get("modal_dialogs", [])
            obs["new_modal_dialogs"] = [t for t in after_modals if t not in before_modals]
            result["observation"] = obs
            result["screenshot"] = obs.get("screenshot")
        result["state"] = c.state_json()
        with self._checkpoints.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({k: v for k, v in result.items() if k != "traceback"}, ensure_ascii=False) + "\n")
        c.current_step = None
        return result

    def _surface_for(self, step) -> str:
        if step.surface in ("browser", "desktop"):
            return step.surface
        if "browser" in self.ctx.surfaces_used:
            return "browser"
        if "desktop" in self.ctx.surfaces_used:
            return "desktop"
        return "browser" if self.ctx.has_browser() else "desktop"

    def _modal_titles(self) -> list[str]:
        try:
            return [w.title for w in self.ctx.peek_desktop().modal_dialogs()]
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------
    def observation(self, surface: str | None = None, tag: str = "obs", detail: bool = False,
                    max_chars: int = 12000) -> dict[str, Any]:
        """Screenshot + lightweight context; with ``detail`` also ARIA / UIA tree text."""
        c = self.ctx
        obs: dict[str, Any] = {"ts": now_iso()}
        surface = surface or ("browser" if c.has_browser() else "desktop")
        obs["surface"] = surface
        try:
            path = c.snap(tag, surface=surface)
            obs["screenshot"] = str(Path(path).relative_to(self.run_dir)) if Path(path).is_relative_to(
                self.run_dir) else str(path)
        except Exception as exc:  # noqa: BLE001
            obs["screenshot_error"] = f"{type(exc).__name__}: {exc}"
        browser = c.peek_browser()
        if browser is not None:
            try:
                page = browser.page()
                obs["url"], obs["title"] = page.url, page.title()
                if detail:
                    obs["aria"] = browser.aria_snapshot(page, max_chars=max_chars)
            except Exception as exc:  # noqa: BLE001
                obs["browser_error"] = f"{type(exc).__name__}: {exc}"
        try:
            desk = c.peek_desktop()
            fg = desk.foreground()
            obs["foreground"] = fg.to_dict() if fg else None
            obs["modal_dialogs"] = [w.title for w in desk.modal_dialogs()]
            if detail and (surface == "desktop" or browser is None):
                obs["ui_tree"] = desk.ui_tree()[:max_chars]
        except Exception as exc:  # noqa: BLE001
            obs["desktop_error"] = f"{type(exc).__name__}: {exc}"
        return obs

    # ------------------------------------------------------------------
    def replace_step(self, step_source: str, expected_id: str | None = None) -> dict[str, Any]:
        sid = validate_step_source(step_source, expected_id)
        exec(compile(step_source, f"<patch:{sid}>", "exec"), self.ns)  # re-registers via @flow.step
        return {"step_id": sid, "steps": self.steps}

    def exec_code(self, code: str) -> dict[str, Any]:
        """Run an exploratory snippet with ``c``, ``expect`` and ``flow`` in scope."""
        buf = io.StringIO()
        scope = {**self.ns, "c": self.ctx, "expect": expect_mod, "flow": self.flow}
        try:
            with contextlib.redirect_stdout(buf):
                exec(compile(code, "<exec>", "exec"), scope)
            return {"ok": True, "stdout": buf.getvalue()[-12000:]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "stdout": buf.getvalue()[-8000:], "error": f"{type(exc).__name__}: {exc}",
                    "traceback": _short_tb(exc, 4000)}

    def set_state(self, state: dict[str, Any]) -> None:
        self.ctx.state = dict(state)

    def finish(self) -> dict[str, Any]:
        return {"output": json.loads(json.dumps(self.ctx.output, default=str, ensure_ascii=False)),
                "state": self.ctx.state_json()}

    def close(self) -> None:
        self.ctx.close()


def latest_checkpoint_state(run_dir: str | Path) -> tuple[dict[str, Any], list[str]]:
    """Return (state after the last passed step, ids of passed steps) from ``steps.jsonl``."""
    path = Path(run_dir) / "steps.jsonl"
    state: dict[str, Any] = {}
    passed: list[str] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "passed":
                state = rec.get("state") or {}
                if rec["step_id"] not in passed:
                    passed.append(rec["step_id"])
    return state, passed


__all__ = ["FlowEngine", "Flow", "latest_checkpoint_state", "now_iso"]
