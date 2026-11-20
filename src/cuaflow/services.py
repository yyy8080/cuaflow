"""Service container + orchestrator (task queue, matching, authoring, supervised runs)."""

from __future__ import annotations

import json
import queue
import threading
import traceback
from pathlib import Path
from typing import Any

from cuaflow.agents.author import Author
from cuaflow.agents.supervisor import Deps, Supervisor
from cuaflow.agents.tracking import CancelToken, TicketBroker
from cuaflow.config import Settings, load_settings, save_settings, settings_path
from cuaflow.library.events import EventBus
from cuaflow.library.match import match_task
from cuaflow.library.store import Store
from cuaflow.llm import LLM, LLMProvider
from cuaflow.platform.paths import data_dir
from cuaflow.platform.shell import LocalShell


class Services:
    def __init__(self, root: str | Path | None = None, llm_override: dict[str, LLM] | LLM | None = None,
                 runner_opts: dict[str, Any] | None = None, settings_file: str | Path | None = None,
                 start_worker: bool = True):
        self.root = Path(root) if root else data_dir() / "library"
        self.store = Store(self.root)
        self.bus = EventBus(self.store)
        self.settings_file = Path(settings_file) if settings_file else settings_path()
        self._settings: Settings = load_settings(self.settings_file)
        self.llms = LLMProvider(self.get_settings, llm_override)
        self.tickets = TicketBroker(self.store, self.bus)
        self.deps = Deps(self.store, self.bus, self.llms, self.get_settings, self.tickets,
                         shell_factory=lambda: LocalShell(blocked_patterns=self._settings.safety.blocked_patterns),
                         runner_opts=dict(runner_opts or {}))
        self.supervisor = Supervisor(self.deps)
        self.author = Author(self.deps)
        self._recover_stale()
        self.orchestrator = Orchestrator(self, start_worker=start_worker)

    def get_settings(self) -> Settings:
        return self._settings

    def update_settings(self, s: Settings) -> Settings:
        self._settings = save_settings(s, self.settings_file)
        return self._settings

    def _recover_stale(self) -> None:
        for r in self.store.list_runs(limit=1000):
            if r["status"] in ("queued", "running", "repairing", "waiting_human"):
                self.store.update_run(r["id"], status="failed", error="interrupted (server restarted)")
        for t in self.store.list_tasks(limit=1000):
            if t["status"] in ("queued", "matching", "authoring", "running"):
                self.store.update_task(t["id"], status="failed", final_response="interrupted (server restarted)")
        for t in self.store.list_tickets("open"):
            self.store.resolve_ticket(t["id"], "abort", "server restarted")


class Orchestrator:
    """Serial worker: one GUI job at a time (the desktop/browser is a shared physical resource)."""

    def __init__(self, svc: Services, start_worker: bool = True):
        self.svc = svc
        self.jobs: queue.Queue = queue.Queue()
        self.tokens: dict[str, CancelToken] = {}
        self._thread: threading.Thread | None = None
        if start_worker:
            self._thread = threading.Thread(target=self._loop, name="cuaflow-worker", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ submit
    def submit_task(self, instruction: str, start_url: str | None = None, params: dict[str, Any] | None = None,
                    force_new: bool = False) -> dict[str, Any]:
        task = self.svc.store.create_task(instruction, start_url, params or {}, force_new)
        self.tokens[task["id"]] = CancelToken()
        self.jobs.put(("task", task["id"], None))
        self.svc.bus.publish("task_status", {"status": "queued"}, task_id=task["id"])
        return task

    def submit_run(self, flow_id: str, params: dict[str, Any], version: int | None = None,
                   from_step: str | None = None, task_id: str | None = None, enqueue: bool = True) -> dict[str, Any]:
        store = self.svc.store
        flow = store.get_flow(flow_id)
        if flow is None:
            raise KeyError(flow_id)
        version = version or flow["current_version"]
        vrec = store.get_version(flow_id, version, with_source=False)
        if vrec is None:
            raise KeyError(f"{flow_id} v{version}")
        steps = [{"step_id": s["id"], "index": i, "title": s.get("title", s["id"]), "expect": s.get("expect", ""),
                  "status": "pending", "attempts": 0, "checks": [], "repairs": []}
                 for i, s in enumerate(vrec.get("steps") or [])]
        run = store.create_run("flow", params, flow_id=flow_id, version=version, task_id=task_id, steps=steps)
        self.tokens[run["id"]] = CancelToken()
        self.svc.bus.publish("run_status", {"status": "queued"}, run_id=run["id"], flow_id=flow_id)
        if enqueue:
            self.jobs.put(("run", run["id"], from_step))
        return run

    def abort(self, run_or_task_id: str) -> None:
        tok = self.tokens.setdefault(run_or_task_id, CancelToken())
        tok.cancel()
        run = self.svc.store.get_run(run_or_task_id)
        if run and run["status"] == "queued":
            self.svc.store.update_run(run["id"], status="aborted", error="aborted before start")
            self.svc.bus.publish("run_status", {"status": "aborted"}, run_id=run["id"])
        if run and run.get("task_id"):
            self.tokens.setdefault(run["task_id"], CancelToken()).cancel()

    # ------------------------------------------------------------------ worker
    def _loop(self) -> None:
        while True:
            kind, ident, extra = self.jobs.get()
            try:
                if kind == "task":
                    self.process_task(ident)
                elif kind == "run":
                    self.process_run(ident, extra)
            except Exception:  # noqa: BLE001
                self.svc.bus.publish("log", {"level": "error", "text": traceback.format_exc()[-3000:]})
            finally:
                self.jobs.task_done()

    def process_run(self, run_id: str, from_step: str | None = None) -> dict[str, Any]:
        run = self.svc.store.get_run(run_id)
        if run is None or run["status"] == "aborted":
            return run or {}
        return self.svc.supervisor.execute(run_id, self.tokens.setdefault(run_id, CancelToken()), from_step)

    def process_task(self, task_id: str) -> dict[str, Any]:
        store, bus = self.svc.store, self.svc.bus
        task = store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        cancel = self.tokens.setdefault(task_id, CancelToken())

        def set_status(status: str, **fields: Any) -> dict[str, Any]:
            t = store.update_task(task_id, status=status, **fields)
            bus.publish("task_status", {"status": status, **({"match": t.get("match")} if t.get("match") else {})},
                        task_id=task_id)
            return t

        if cancel.cancelled:
            return set_status("aborted")
        set_status("matching")
        if task.get("force_new"):
            m = {"verdict": "new", "flow_id": None, "params": task.get("params") or {}, "reason": "forced new"}
        else:
            m = match_task(self.svc.llms.get("matcher"), store.list_flows(), task["instruction"],
                           task.get("start_url"), task.get("params"))
        set_status("matching", match=m)

        if m["verdict"] == "run":
            run = self.submit_run(m["flow_id"], m.get("params") or {}, task_id=task_id, enqueue=False)
            self.tokens[run["id"]] = cancel
            set_status("running", run_id=run["id"], flow_id=m["flow_id"])
            run = self.svc.supervisor.execute(run["id"], cancel)
            return self._finish_task(task_id, run, set_status)

        set_status("authoring")
        run = store.create_run("author", task.get("params") or {}, task_id=task_id)
        self.tokens[run["id"]] = cancel
        set_status("authoring", run_id=run["id"])
        base = m["flow_id"] if m["verdict"] == "adapt" else None
        result = self.svc.author.record(run["id"], task, cancel, base_flow_id=base,
                                        base_params=m.get("params") if base else None)
        final = result.get("final_response", "")
        if result.get("status") != "succeeded":
            return set_status("aborted" if result.get("status") == "aborted" else "failed",
                              final_response=result.get("error") or final)
        flow_id = result.get("flow_id")
        set_status("authoring", flow_id=flow_id, final_response=final)
        s = self.svc.get_settings()
        flow = store.get_flow(flow_id) if flow_id else None
        if flow and s.author.verify_replay and not flow["side_effects"] and not cancel.cancelled:
            replay = self.submit_run(flow_id, result.get("params") or {}, task_id=task_id, enqueue=False)
            self.tokens[replay["id"]] = cancel
            set_status("running", run_id=replay["id"])
            replay = self.svc.supervisor.execute(replay["id"], cancel)
            if replay.get("status") != "succeeded":
                return set_status("failed", final_response=f"{final}\n\n[回放验证失败] {replay.get('error')}")
        return set_status("succeeded", final_response=final)

    def _finish_task(self, task_id: str, run: dict[str, Any], set_status) -> dict[str, Any]:
        status = run.get("status")
        out = json.dumps(run.get("output") or {}, ensure_ascii=False)
        if status == "succeeded":
            return set_status("succeeded", final_response=f"已复用脚本执行完成。输出: {out}")
        return set_status("aborted" if status == "aborted" else "failed",
                          final_response=run.get("error") or "run failed")
