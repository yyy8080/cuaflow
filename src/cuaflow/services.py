"""服务容器与编排器（任务队列、工作流匹配、长程任务规划器、受监督运行与持续定时调度）。"""

from __future__ import annotations

import json
import queue
import threading
import traceback
from pathlib import Path
from typing import Any

from cuaflow.agents.author import Author
from cuaflow.agents.planner import MissionPlanner
from cuaflow.agents.supervisor import Deps, Supervisor
from cuaflow.agents.tracking import CancelToken, TicketBroker
from cuaflow.config import Settings, load_settings, save_settings, settings_path
from cuaflow.library.events import EventBus
from cuaflow.library.match import looks_long_horizon, match_task
from cuaflow.library.store import Store
from cuaflow.llm import LLM, LLMProvider, UsageMeter, usage_scope
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
        self.planner = MissionPlanner(self.deps, self.supervisor, self.author)
        resumable = self._recover_stale()
        self.orchestrator = Orchestrator(self, start_worker=start_worker)
        if start_worker and self._settings.planner.auto_resume:
            for tid in resumable:
                self.orchestrator.resume_task(tid)

    def get_settings(self) -> Settings:
        return self._settings

    def update_settings(self, s: Settings) -> Settings:
        self._settings = save_settings(s, self.settings_file)
        return self._settings

    def close(self) -> None:
        self.orchestrator._stop.set()
        for tok in list(self.orchestrator.tokens.values()):
            tok.cancel()

    def _recover_stale(self) -> list[str]:
        resumable: list[str] = []
        for r in self.store.list_runs(limit=1000):
            if r["status"] in ("queued", "running", "repairing", "waiting_human"):
                self.store.update_run(r["id"], status="failed", error="interrupted (server restarted)")
        for t in self.store.list_tasks(limit=1000):
            if t["status"] in ("queued", "matching", "authoring", "planning", "running"):
                if t.get("checkpoint"):
                    resumable.append(t["id"])
                self.store.update_task(t["id"], status="failed",
                                       final_response="interrupted (server restarted; resumable from checkpoint)")
        for t in self.store.list_tickets("open"):
            self.store.resolve_ticket(t["id"], "abort", "server restarted")
        return resumable


class Orchestrator:
    """串行工作线程与持续定时调度监控器（桌面与浏览器属于共享物理资源，需串行互斥使用）。"""

    def __init__(self, svc: Services, start_worker: bool = True):
        self.svc = svc
        self.jobs: queue.Queue = queue.Queue()
        self.tokens: dict[str, CancelToken] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sched_thread: threading.Thread | None = None
        if start_worker:
            self._thread = threading.Thread(target=self._loop, name="cuaflow-worker", daemon=True)
            self._thread.start()
            self._sched_thread = threading.Thread(target=self._scheduler_loop, name="cuaflow-scheduler", daemon=True)
            self._sched_thread.start()

    # ------------------------------------------------------------------ 任务提交与恢复
    def submit_task(
        self,
        instruction: str,
        start_url: str | None = None,
        params: dict[str, Any] | None = None,
        force_new: bool = False,
        mode: str = "auto",
        enqueue: bool = True,
    ) -> dict[str, Any]:
        task = self.svc.store.create_task(instruction, start_url, params or {}, force_new, mode=mode)
        self.tokens[task["id"]] = CancelToken()
        self.svc.bus.publish("task_status", {"status": "queued", "mode": mode}, task_id=task["id"])
        if enqueue:
            self.jobs.put(("task", task["id"], {"resume": False}))
        return task

    def resume_task(self, task_id: str, enqueue: bool = True) -> dict[str, Any]:
        task = self.svc.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        self.tokens[task_id] = CancelToken()
        task = self.svc.store.update_task(task_id, status="queued")
        self.svc.bus.publish("task_status", {"status": "queued", "resumed": True}, task_id=task_id)
        if enqueue:
            self.jobs.put(("task", task_id, {"resume": True}))
        return task

    def submit_run(
        self,
        flow_id: str,
        params: dict[str, Any],
        version: int | None = None,
        from_step: str | None = None,
        task_id: str | None = None,
        enqueue: bool = True,
        resume_checkpoint: bool = False,
        unattended: bool | None = None,
    ) -> dict[str, Any]:
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
        if task_id:
            store.append_task_run(task_id, run["id"])
        self.tokens[run["id"]] = CancelToken()
        self.svc.bus.publish("run_status", {"status": "queued"}, run_id=run["id"], flow_id=flow_id)
        if enqueue:
            self.jobs.put(("run", run["id"], {"from_step": from_step, "resume_checkpoint": resume_checkpoint,
                                              "unattended": unattended}))
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
        task = self.svc.store.get_task(run_or_task_id)
        if task:
            if task["status"] == "queued":
                self.svc.store.update_task(task["id"], status="aborted", final_response="aborted before start")
                self.svc.bus.publish("task_status", {"status": "aborted"}, task_id=task["id"])
            if task.get("run_id"):
                self.tokens.setdefault(task["run_id"], CancelToken()).cancel()

    # ------------------------------------------------------------------ 工作线程与定时调度循环
    def _loop(self) -> None:
        while not self._stop.is_set():
            kind, ident, extra = self.jobs.get()
            try:
                if kind == "task":
                    opts = extra if isinstance(extra, dict) else {}
                    self.process_task(ident, resume=bool(opts.get("resume")), unattended=bool(opts.get("unattended")))
                elif kind == "run":
                    if isinstance(extra, dict):
                        self.process_run(
                            ident,
                            extra.get("from_step"),
                            resume_checkpoint=bool(extra.get("resume_checkpoint")),
                            unattended=extra.get("unattended"),
                        )
                    else:
                        self.process_run(ident, extra)
                elif kind == "schedule":
                    self.process_schedule(ident)
            except Exception:  # noqa: BLE001
                self.svc.bus.publish("log", {"level": "error", "text": traceback.format_exc()[-3000:]})
            finally:
                self.jobs.task_done()

    def _scheduler_loop(self) -> None:
        while not self._stop.wait(2.0):
            try:
                for sc in self.svc.store.due_schedules():
                    # 推入串行工作队列；临时顺延 next_run_at 以避免重复入队
                    from cuaflow.library.store import future_iso

                    self.svc.store.update_schedule(sc["id"], next_run_at=future_iso(sc["interval_seconds"]))
                    self.jobs.put(("schedule", sc["id"], None))
            except Exception:  # noqa: BLE001
                pass

    def tick_schedules(self) -> list[dict[str, Any]]:
        """同步执行当前所有已到期的定时计划（供测试与手动触发使用）。"""
        results = []
        for sc in self.svc.store.due_schedules():
            results.append(self.process_schedule(sc["id"]))
        return results

    def process_schedule(self, schedule_id: str) -> dict[str, Any]:
        store, bus = self.svc.store, self.svc.bus
        sc = store.get_schedule(schedule_id)
        if sc is None:
            raise KeyError(schedule_id)
        if sc["kind"] == "flow" and sc.get("flow_id"):
            run = self.submit_run(sc["flow_id"], sc.get("params") or {}, enqueue=False, unattended=True)
            res = self.process_run(run["id"], unattended=True)
            ok = res.get("status") == "succeeded"
            updated = store.record_schedule_run(schedule_id, run["id"], ok)
        else:
            task = self.submit_task(
                sc.get("instruction") or sc["name"],
                start_url=sc.get("start_url"),
                params=sc.get("params") or {},
                enqueue=False,
            )
            res = self.process_task(task["id"], unattended=True)
            ok = res.get("status") == "succeeded"
            updated = store.record_schedule_run(schedule_id, task["id"], ok)

        bus.publish("schedule_run", {"schedule_id": schedule_id, "succeeded": ok, "schedule": updated},
                    flow_id=sc.get("flow_id"))
        if updated.get("status") == "tripped":
            bus.publish(
                "schedule_tripped",
                {"schedule_id": schedule_id, "name": sc["name"],
                 "consecutive_failures": updated.get("consecutive_failures")},
                flow_id=sc.get("flow_id"),
            )
        return updated

    def process_run(
        self,
        run_id: str,
        from_step: str | None = None,
        *,
        resume_checkpoint: bool = False,
        unattended: bool | None = None,
    ) -> dict[str, Any]:
        run = self.svc.store.get_run(run_id)
        if run is None or run["status"] == "aborted":
            return run or {}
        return self.svc.supervisor.execute(
            run_id,
            self.tokens.setdefault(run_id, CancelToken()),
            from_step,
            resume_checkpoint=resume_checkpoint,
            unattended=unattended,
        )

    def _planner_available(self) -> bool:
        return self.svc.llms._override is None or self.svc.llms.has_explicit_role("planner") or (
            not isinstance(self.svc.llms._override, dict)
        )

    def _should_use_mission(self, task: dict[str, Any], match: dict[str, Any], resume: bool) -> bool:
        mode = task.get("mode") or "auto"
        if mode == "mission":
            return True
        if mode == "single":
            return False
        if resume and task.get("checkpoint"):
            return True
        s = self.svc.get_settings()
        if not s.planner.enabled or not self._planner_available():
            return False
        if match.get("verdict") == "mission":
            return True
        return looks_long_horizon(task.get("instruction", ""))

    def process_task(self, task_id: str, *, resume: bool = False, unattended: bool = False) -> dict[str, Any]:
        store, bus = self.svc.store, self.svc.bus
        s = self.svc.get_settings()
        task = store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        cancel = self.tokens.setdefault(task_id, CancelToken())
        task_meter = UsageMeter(
            tokens_per_script_step=s.planner.tokens_saved_per_step,
            initial=task.get("usage") if resume else None,
        )

        def set_status(status: str, **fields: Any) -> dict[str, Any]:
            fields.setdefault("usage", task_meter.snapshot())
            t = store.update_task(task_id, status=status, **fields)
            bus.publish("task_status", {"status": status, **({"match": t.get("match")} if t.get("match") else {})},
                        task_id=task_id)
            return t

        if cancel.cancelled:
            return set_status("aborted")

        with usage_scope(task_meter):
            # 1. 恢复带有检查点的长程任务时，直接进入 MissionPlanner
            if resume and task.get("checkpoint"):
                return self._run_mission(task, cancel, set_status, resume=True, unattended=unattended)

            # 2. 恢复中断的单工作流运行时，从 steps.jsonl 检查点还原执行
            if resume and task.get("flow_id") and task.get("run_id"):
                prev_run = store.get_run(task["run_id"])
                if prev_run and prev_run.get("kind") == "flow":
                    set_status("running")
                    store.update_run(prev_run["id"], status="queued", error=None)
                    self.tokens[prev_run["id"]] = cancel
                    run = self.svc.supervisor.execute(prev_run["id"], cancel, resume_checkpoint=True,
                                                      unattended=unattended)
                    return self._finish_task(task_id, run, set_status)

            # 3. 显式指定 mission 模式时，直接交由 MissionPlanner 执行
            if task.get("mode") == "mission":
                m = {"verdict": "mission", "flow_id": None, "params": task.get("params") or {},
                     "reason": "explicit mission mode"}
                set_status("matching", match=m)
                return self._run_mission(task, cancel, set_status, resume=False, unattended=unattended)

            set_status("matching")
            if task.get("force_new"):
                m = {"verdict": "new", "flow_id": None, "params": task.get("params") or {}, "reason": "forced new"}
            else:
                m = match_task(self.svc.llms.get("matcher"), store.list_flows(), task["instruction"],
                               task.get("start_url"), task.get("params"))
            set_status("matching", match=m)

            # 4. 快速路径：命中已有工作流 -> 在 Supervisor 监督下确定性执行（零探索 Token 消耗）
            if m["verdict"] == "run":
                run = self.submit_run(m["flow_id"], m.get("params") or {}, task_id=task_id, enqueue=False,
                                      unattended=unattended)
                self.tokens[run["id"]] = cancel
                set_status("running", run_id=run["id"], flow_id=m["flow_id"])
                run = self.svc.supervisor.execute(run["id"], cancel, unattended=unattended)
                # 若脚本发生结构性失败（非手动中止且非遇到 CAPTCHA）且 Planner 可用，则切换为自主规划恢复执行
                if (
                    run.get("status") == "failed"
                    and not cancel.cancelled
                    and "CAPTCHA" not in str(run.get("error") or "")
                    and task.get("mode") != "single"
                    and s.planner.enabled
                    and self._planner_available()
                ):
                    bus.publish("log", {"level": "warning",
                                        "text": f"脚本 {m['flow_id']} 执行未通过，切换至 MissionPlanner 自主规划完成"},
                                task_id=task_id)
                    return self._run_mission(task, cancel, set_status, resume=False, unattended=unattended)
                return self._finish_task(task_id, run, set_status)

            # 5. 长程 / 多阶段 / 批量任务 -> 交由 MissionPlanner 规划执行
            if self._should_use_mission(task, m, resume=resume):
                return self._run_mission(task, cancel, set_status, resume=resume, unattended=unattended)

            # 6. 单一线性任务 -> 由 Author 边执行边编译生成 v1 工作流
            set_status("authoring")
            run = store.create_run("author", task.get("params") or {}, task_id=task_id)
            store.append_task_run(task_id, run["id"])
            self.tokens[run["id"]] = cancel
            set_status("authoring", run_id=run["id"])
            base = m["flow_id"] if m["verdict"] == "adapt" else None
            result = self.svc.author.record(run["id"], task, cancel, base_flow_id=base,
                                            base_params=m.get("params") if base else None,
                                            unattended=unattended)
            final = result.get("final_response", "")
            if result.get("status") != "succeeded":
                return set_status("aborted" if result.get("status") == "aborted" else "failed",
                                  final_response=result.get("error") or final)
            flow_id = result.get("flow_id")
            set_status("authoring", flow_id=flow_id, final_response=final)
            flow = store.get_flow(flow_id) if flow_id else None
            if flow and s.author.verify_replay and not flow["side_effects"] and not cancel.cancelled:
                replay = self.submit_run(flow_id, result.get("params") or {}, task_id=task_id, enqueue=False,
                                         unattended=unattended)
                self.tokens[replay["id"]] = cancel
                set_status("running", run_id=replay["id"])
                replay = self.svc.supervisor.execute(replay["id"], cancel, unattended=unattended)
                if replay.get("status") != "succeeded":
                    return set_status("failed", final_response=f"{final}\n\n[回放验证失败] {replay.get('error')}")
            return set_status("succeeded", final_response=final)

    def _run_mission(self, task: dict[str, Any], cancel: CancelToken, set_status,
                     *, resume: bool, unattended: bool) -> dict[str, Any]:
        store = self.svc.store
        mrun = store.create_run("mission", task.get("params") or {}, task_id=task["id"])
        store.append_task_run(task["id"], mrun["id"])
        self.tokens[mrun["id"]] = cancel
        set_status("planning", run_id=mrun["id"])
        fresh_task = store.get_task(task["id"]) or task
        res = self.svc.planner.execute(mrun["id"], fresh_task, cancel, resume=resume, unattended=unattended)
        status = res.get("status")
        if status == "succeeded":
            return set_status("succeeded", flow_id=res.get("flow_id") or fresh_task.get("flow_id"),
                              final_response=res.get("final_response") or "长任务已完成")
        return set_status("aborted" if status == "aborted" else "failed",
                          flow_id=res.get("flow_id") or fresh_task.get("flow_id"),
                          final_response=res.get("error") or res.get("final_response") or "mission failed")

    def _finish_task(self, task_id: str, run: dict[str, Any], set_status) -> dict[str, Any]:
        status = run.get("status")
        out = json.dumps(run.get("output") or {}, ensure_ascii=False)
        if status == "succeeded":
            return set_status("succeeded", final_response=f"已复用脚本执行完成。输出: {out}")
        return set_status("aborted" if status == "aborted" else "failed",
                          final_response=run.get("error") or "run failed")
