"""Supervisor: executes a flow step by step, verifies every step (L0–L3), drives repairs and escalation."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cuaflow.agents.judge import judge_task
from cuaflow.agents.repairer import Repairer
from cuaflow.agents.tools import RunnerGuard, StepVerifier, check
from cuaflow.agents.tracking import CancelToken, RunTracker, TicketBroker
from cuaflow.config import Settings
from cuaflow.flow.client import RunnerClient, RunnerError
from cuaflow.flow.engine import now_iso
from cuaflow.flow.patching import get_step_source, replace_step, unified_diff
from cuaflow.library.events import EventBus
from cuaflow.library.store import Store
from cuaflow.llm import LLMProvider
from cuaflow.platform.base import ShellBackend


@dataclass
class Deps:
    store: Store
    bus: EventBus
    llms: LLMProvider
    settings: Callable[[], Settings]
    tickets: TicketBroker
    shell_factory: Callable[[], ShellBackend]
    runner_opts: dict[str, Any] = field(default_factory=dict)  # e.g. {"desktop": "fake", "headless": True}

    def new_runner(self, run_dir: Path) -> RunnerClient:
        rc = RunnerClient(log_path=run_dir / "runner.log")
        rc.start()
        return rc


class Supervisor:
    def __init__(self, deps: Deps):
        self.d = deps

    # ------------------------------------------------------------------
    def _use_l2(self, flow_id: str, step: dict[str, Any], s: Settings) -> bool:
        pol = s.supervisor.l2_policy
        if pol == "off":
            return False
        if pol == "always":
            return True
        if pol == "sample":
            return step.get("critical") or random.random() < s.supervisor.sample_rate
        stats = self.d.store.step_stats(flow_id, step["id"])
        return bool(step.get("critical") or stats.get("needs_verify", 1) or stats.get("runs", 0) == 0)

    def execute(self, run_id: str, cancel: CancelToken | None = None, from_step: str | None = None) -> dict[str, Any]:
        cancel = cancel or CancelToken()
        store, s = self.d.store, self.d.settings()
        run = store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        flow_id = run["flow_id"]
        flow = store.get_flow(flow_id)
        if flow is None:
            raise KeyError(flow_id)
        version = run.get("version") or flow["current_version"]
        tr = RunTracker(store, self.d.bus, run_id, flow_id=flow_id, task_id=run.get("task_id"))
        run_dir = store.run_dir(run_id)
        source = store.read_source(flow_id, version)
        work_file = run_dir / "flow.py"
        work_file.write_text(source, encoding="utf-8")
        version_rec = store.get_version(flow_id, version, with_source=False) or {}
        tr.init_steps(version_rec.get("steps") or [])
        tr.status("running", version=version)
        intent = flow.get("intent_template") or flow.get("name", "")
        params = dict(run.get("params") or {})

        runner: RunnerClient | None = None
        try:
            runner = self.d.new_runner(run_dir)
            try:
                meta = runner.load(work_file, params, run_dir, **self.d.runner_opts)
            except RunnerError as exc:
                return tr.status("failed", error=f"cannot load flow: {exc}")
            steps = meta["steps"]
            tr.init_steps(steps, keep_existing=True)
            start = 0
            if from_step:
                start = [st["id"] for st in steps].index(from_step)
                for st in steps[:start]:
                    tr.set_step_status(st["id"], "skipped")
            judge_llm = self.d.llms.get("vision_judge") if s.supervisor.l2_policy != "off" else None
            verifier = StepVerifier(judge_llm, run_dir, intent)
            guard = RunnerGuard(runner, run_dir)
            shell = self.d.shell_factory()
            patched: list[tuple[str, str]] = []
            working = source

            for step in steps[start:]:
                sid = step["id"]
                if cancel.cancelled:
                    return tr.status("aborted", error="aborted by user")
                tr.step_start(sid)
                result, passed, checks = self._run_with_retries(runner, guard, verifier, tr, flow_id, step, s)
                if passed:
                    continue
                # ---------------- failure: repair loop ----------------
                tr.set_step_status(sid, "repairing")
                tr.status("repairing")
                repairer = Repairer(self.d.llms.get("repairer"), judge_llm, runner, run_dir, shell,
                                    on_log=lambda k, t, _sid=sid: tr.log("repairer", k, t, step_id=_sid),
                                    should_stop=lambda: cancel.cancelled, turn_limit=s.supervisor.repair_turn_limit)
                history: list[str] = []
                hint: str | None = None
                resolved = False
                while not resolved:
                    for attempt in range(1, s.supervisor.max_repair_attempts + 1):
                        if cancel.cancelled:
                            return tr.status("aborted", error="aborted by user")
                        rep = {"attempt": len(tr.rec(sid)["repairs"]) + 1, "started_at": now_iso(),
                               "status": "running"}
                        tr.rec(sid)["repairs"].append(rep)
                        tr.persist()
                        tr.emit("repair_start", {"attempt": rep["attempt"]}, step_id=sid)
                        outcome = repairer.repair(working, step, result, checks, intent=intent, hint=hint,
                                                  history=history)
                        rep.update(ended_at=now_iso(), summary=outcome.summary,
                                   status="succeeded" if outcome.success else "failed")
                        if outcome.success and outcome.step_source:
                            old_step = get_step_source(working, sid)
                            working = replace_step(working, sid, outcome.step_source)
                            work_file.write_text(working, encoding="utf-8")
                            rep["diff"] = unified_diff(old_step, get_step_source(working, sid), f"{sid}.py")
                            patched.append((sid, outcome.summary))
                            tr.step_result(sid, outcome.result or {}, outcome.checks, "repaired")
                            tr.emit("repair_end", rep, step_id=sid)
                            resolved = True
                            break
                        tr.persist()
                        tr.emit("repair_end", rep, step_id=sid)
                        history.append(outcome.summary)
                        if outcome.result:
                            result, checks = outcome.result, outcome.checks or checks
                        if outcome.gave_up or outcome.summary.startswith("repair stopped: cancelled"):
                            break
                    if resolved:
                        break
                    # ---------------- escalate to a human ----------------
                    tr.status("waiting_human")
                    ticket = self.d.tickets.open(run_id, f"步骤 {step.get('title')!r} 自动修复失败: "
                                                         f"{history[-1] if history else result.get('error')}",
                                                 step_id=sid, flow_id=flow_id, screenshot=result.get("screenshot"))
                    res = self.d.tickets.wait(ticket["id"], cancel)
                    action = res.get("action")
                    if action == "abort":
                        tr.set_step_status(sid, "failed")
                        return tr.status("aborted", error="aborted by operator")
                    if action == "skip":
                        tr.set_step_status(sid, "skipped")
                        resolved = True
                    elif action == "continue":
                        tr.rec(sid)["checks"].append(check("L0", True, "completed manually by operator"))
                        tr.set_step_status(sid, "passed")
                        store.mark_needs_verify(flow_id, sid)
                        resolved = True
                    else:  # hint -> another round of repair attempts
                        hint = res.get("hint") or ""
                    tr.status("repairing" if not resolved else "running")
                tr.status("running")

            out = runner.finish()
            output = out.get("output") or {}
            fields: dict[str, Any] = {"output": output}
            if patched:
                ids = ",".join(dict.fromkeys(p[0] for p in patched))
                reason = "; ".join(f"[{sid}] {summary}" for sid, summary in patched)
                v = store.add_version(flow_id, working, steps, author="repairer", reason=f"自动修复: {reason}",
                                      repaired_step=ids, parent_version=version)
                fields["new_version"] = v["version"]
                tr.emit("version_created", v)
            status = "succeeded"
            if s.supervisor.l3_enabled:
                verdict = judge_task(self.d.llms.get("supervisor"), intent, tr.records, output, run_dir)
                if verdict["passed"] is not None:
                    fields["final_verdict"] = verdict
                    tr.emit("check", check("L3", bool(verdict["passed"]), verdict["reason"]))
                    if not verdict["passed"]:
                        status, fields["error"] = "failed", f"task-level verification failed: {verdict['reason']}"
            if any(r["status"] == "skipped" for r in tr.records[start:]):
                fields.setdefault("error", "some steps were skipped by the operator")
            return tr.status(status, **fields)
        except Exception as exc:  # noqa: BLE001
            tr.log("supervisor", "observation", f"internal error: {type(exc).__name__}: {exc}")
            return tr.status("failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            if runner is not None:
                runner.close()
            self._after_run(run_id)

    # ------------------------------------------------------------------
    def _run_with_retries(self, runner, guard, verifier, tr, flow_id, step, s):
        sid = step["id"]
        retries = 0 if step.get("side_effect") else int(step.get("retries") or 0)
        timeout = float(step.get("timeout") or s.supervisor.step_timeout_default)
        result: dict[str, Any] = {}
        checks: list[dict[str, Any]] = []
        for i in range(retries + 1):
            result, err = guard.call(lambda: runner.run_step(sid, timeout=timeout))
            if err:
                result = {"step_id": sid, "status": "failed", "error_kind": "timeout", "error": err,
                          "ended_at": now_iso(), "duration_ms": int(timeout * 1000)}
            use_l2 = self._use_l2(flow_id, step, s)
            passed, checks = verifier.verify(step, result, use_l2)
            self.d.store.record_step(flow_id, sid, passed, int(result.get("duration_ms") or 0))
            if passed:
                if use_l2 and any(c["level"] == "L2" and c["passed"] and "skipped" not in c["detail"]
                                  for c in checks):
                    self.d.store.mark_needs_verify(flow_id, sid, False)
                tr.step_result(sid, result, checks, "passed")
                return result, True, checks
            last = i == retries
            tr.step_result(sid, result, checks, "failed" if last else "running")
        return result, False, checks

    def _after_run(self, run_id: str) -> None:
        """Auto-rollback: if the current version keeps failing and the parent was healthy, revert."""
        store, s = self.d.store, self.d.settings()
        run = store.get_run(run_id) or {}
        if not s.supervisor.auto_rollback or run.get("status") != "failed" or not run.get("flow_id"):
            return
        flow = store.get_flow(run["flow_id"]) or {}
        cur = flow.get("current_version")
        n = s.supervisor.rollback_after_failures
        recent = store.recent_version_results(run["flow_id"], cur, n)
        if len(recent) < n or any(r != "failed" for r in recent):
            return
        ver = store.get_version(run["flow_id"], cur, with_source=False) or {}
        parent = ver.get("parent_version")
        if not parent:
            return
        ph = (store.get_version(run["flow_id"], parent, with_source=False) or {}).get("health", {})
        if ph.get("runs") and ph.get("successes", 0) / ph["runs"] >= 0.5:
            store.set_current_version(run["flow_id"], parent)
            self.d.bus.publish("log", {"level": "warning", "text": f"自动回滚 {run['flow_id']} v{cur} -> v{parent}"},
                               run_id=run_id, flow_id=run["flow_id"])

