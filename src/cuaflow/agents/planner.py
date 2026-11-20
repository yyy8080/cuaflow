"""长程自主任务规划器（`MissionPlanner`）与批量技能编译器。

将复杂的浏览器或桌面任务拆解为动态阶段，优先复用流程库中已有的 ``Flow`` 技能；
在批量循环的首轮迭代中将可重复子任务编译为参数化 ``Flow``，使第 2..N 轮迭代在
``Supervisor`` 监督下以零探索 Token 运行；并在每个阶段及批次项完成后持久化检查点，
确保中断的长程任务能够无缝续跑。
"""

from __future__ import annotations

import json
import time
from typing import Any

from cuaflow.agents.author import Author
from cuaflow.agents.loop import Tool, ToolAgent, ToolResult
from cuaflow.agents.supervisor import Deps, Supervisor
from cuaflow.agents.tools import RunnerGuard, check, common_tools, load_prompt
from cuaflow.agents.tracking import CancelToken, RunTracker
from cuaflow.flow.client import RunnerClient, RunnerError
from cuaflow.flow.engine import now_iso
from cuaflow.flow.patching import render_flow_header
from cuaflow.llm import UsageMeter, usage_scope
from cuaflow.llm.base import tool_spec

PHASE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "unique snake_case phase id"},
        "title": {"type": "string"},
        "kind": {"type": "string", "enum": ["explore", "skill", "batch", "synthesize"]},
        "status": {"type": "string", "enum": ["pending", "running", "done", "skipped", "failed"]},
        "note": {"type": "string"},
    },
    "required": ["id", "title"],
}


class MissionSession:
    def __init__(
        self,
        deps: Deps,
        supervisor: Supervisor,
        author: Author,
        run_id: str,
        task: dict[str, Any],
        cancel: CancelToken,
        *,
        resume: bool = False,
        unattended: bool = False,
    ):
        self.d = deps
        self.supervisor = supervisor
        self.author = author
        self.run_id = run_id
        self.task = task
        self.task_id: str = task["id"]
        self.cancel = cancel
        self.resume = resume
        self.unattended = unattended
        self.store = deps.store
        self.run_dir = self.store.run_dir(run_id)
        self.task_dir = self.store.task_dir(self.task_id)
        self.scratch_flow = self.run_dir / "mission_scratch.py"
        self.tr = RunTracker(self.store, deps.bus, run_id, task_id=self.task_id)

        existing_plan = task.get("plan") if isinstance(task.get("plan"), dict) else None
        self.plan: dict[str, Any] = existing_plan or {"goal": task["instruction"], "phases": []}

        ckpt = task.get("checkpoint") if (resume and isinstance(task.get("checkpoint"), dict)) else {}
        self.mission_state: dict[str, Any] = dict(ckpt.get("mission_state") or task.get("params") or {})
        self.compiled_flows: dict[str, str] = dict(ckpt.get("compiled_flows") or {})
        self.batches: dict[str, dict[str, Any]] = {
            k: dict(v) for k, v in (ckpt.get("batches") or {}).items() if isinstance(v, dict)
        }
        self.last_flow_id: str | None = task.get("flow_id")
        self.meter = UsageMeter(
            tokens_per_script_step=deps.settings().planner.tokens_saved_per_step,
            initial=task.get("usage") if resume else None,
        )

    # ------------------------------------------------------------------ 状态持久化与运行器上下文
    def _save_checkpoint(self) -> None:
        safe_state = json.loads(json.dumps(self.mission_state, default=str, ensure_ascii=False))
        ckpt = {
            "updated_at": now_iso(),
            "mission_state": safe_state,
            "compiled_flows": self.compiled_flows,
            "batches": self.batches,
        }
        (self.task_dir / "checkpoint.json").write_text(
            json.dumps(ckpt, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        usage_snap = self.meter.snapshot()
        self.store.update_task(
            self.task_id,
            plan=self.plan,
            checkpoint=ckpt,
            usage=usage_snap,
            flow_id=self.last_flow_id,
        )
        self.store.update_run(self.run_id, usage=usage_snap)

    def _reload_scratch(self) -> None:
        """子运行结束后，在共享的热运行器上恢复规划器的临时上下文。"""
        if not self.scratch_flow.is_file():
            self.scratch_flow.write_text(render_flow_header("mission_scratch"), encoding="utf-8")
        safe_state = json.loads(json.dumps(self.mission_state, default=str, ensure_ascii=False))
        try:
            self.runner.load(self.scratch_flow, {}, self.run_dir, state=safe_state, **self.d.runner_opts)
        except RunnerError:
            self.guard.call(lambda: self.runner.restart(state=safe_state, flow_path=self.scratch_flow))

    def _sync_state_from_runner(self) -> None:
        st, err = self.guard.call(lambda: self.runner.call("get_state"))
        if not err and isinstance(st, dict):
            self.mission_state.update(st)
            self._save_checkpoint()

    def _sync_plan_to_tracker(self) -> None:
        phases = self.plan.get("phases") or []
        steps_spec = [
            {"id": p["id"], "title": f"[{p.get('kind', 'phase')}] {p['title']}", "expect": p.get("note", "")}
            for p in phases
        ]
        self.tr.init_steps(steps_spec, keep_existing=True)
        status_map = {
            "pending": "pending",
            "running": "running",
            "done": "passed",
            "skipped": "skipped",
            "failed": "failed",
        }
        for p in phases:
            mapped = status_map.get(p.get("status", "pending"), "pending")
            rec = self.tr.rec(p["id"])
            rec["title"] = f"[{p.get('kind', 'phase')}] {p['title']}"
            if p.get("note"):
                rec["expect"] = p["note"]
            if rec["status"] != mapped:
                self.tr.set_step_status(p["id"], mapped)
        self.tr.persist()
        self.tr.emit("plan_updated", self.plan)

    def _mark_phase(self, phase_id: str | None, status: str, detail: str = "") -> None:
        if not phase_id:
            return
        for p in self.plan.get("phases") or []:
            if p["id"] == phase_id:
                p["status"] = status
                if detail:
                    p["note"] = detail[:300]
                break
        if status == "running":
            self.tr.step_start(phase_id)
        elif status == "done":
            self.tr.rec(phase_id)["checks"].append(check("L0", True, detail or "phase completed"))
            self.tr.set_step_status(phase_id, "passed")
        elif status == "failed":
            self.tr.set_step_status(phase_id, "failed", error=detail)
        self._save_checkpoint()
        self.tr.emit("plan_updated", self.plan)

    # ------------------------------------------------------------------ 规划器工具实现
    def tool_update_plan(self, args: dict[str, Any]) -> ToolResult:
        phases = args.get("phases") or []
        ids = [p.get("id", "") for p in phases]
        if not phases or len(set(ids)) != len(ids) or not all(i.isidentifier() for i in ids):
            return ToolResult("Invalid plan: each phase needs a unique snake_case identifier `id`.")
        norm = []
        old_by_id = {p["id"]: p for p in (self.plan.get("phases") or [])}
        for p in phases:
            prev = old_by_id.get(p["id"], {})
            norm.append({
                "id": p["id"],
                "title": p["title"],
                "kind": p.get("kind") or prev.get("kind") or "explore",
                "status": p.get("status") or prev.get("status") or "pending",
                "note": p.get("note") if "note" in p else prev.get("note", ""),
            })
        self.plan = {"goal": args.get("goal") or self.task["instruction"], "phases": norm}
        self._sync_plan_to_tracker()
        self._save_checkpoint()
        summary = ", ".join(f"{p['id']}:{p['status']}" for p in norm)
        return ToolResult(f"Plan updated ({len(norm)} phases): {summary}")

    def tool_save_state(self, args: dict[str, Any]) -> ToolResult:
        key = str(args["key"])
        self.mission_state[key] = args.get("value")
        self.guard.call(lambda: self.runner.call("set_state", state=self.mission_state))
        self._save_checkpoint()
        return ToolResult(f"Saved state[{key!r}]. Keys: {sorted(self.mission_state)}")

    def tool_read_state(self, args: dict[str, Any]) -> ToolResult:
        key = args.get("key")
        if key:
            val = self.mission_state.get(key)
            text = json.dumps(val, ensure_ascii=False, default=str)
            return ToolResult(text[:8000] if len(text) > 8000 else text)
        overview = {
            k: (f"<list len={len(v)} sample={v[:2]}>" if isinstance(v, list) and len(v) > 5 else v)
            for k, v in self.mission_state.items()
        }
        return ToolResult(json.dumps(overview, ensure_ascii=False, default=str)[:8000])

    def _create_sub_flow_run(self, flow_id: str, params: dict[str, Any]) -> dict[str, Any]:
        flow = self.store.get_flow(flow_id)
        if flow is None:
            raise KeyError(f"unknown flow_id {flow_id!r}")
        ver = flow["current_version"]
        vrec = self.store.get_version(flow_id, ver, with_source=False) or {}
        steps = [
            {"step_id": s["id"], "index": i, "title": s.get("title", s["id"]), "expect": s.get("expect", ""),
             "status": "pending", "attempts": 0, "checks": [], "repairs": []}
            for i, s in enumerate(vrec.get("steps") or [])
        ]
        sub = self.store.create_run("flow", params, flow_id=flow_id, version=ver, task_id=self.task_id, steps=steps)
        self.store.append_task_run(self.task_id, sub["id"])
        # 将任务的当前 run_id 指针恢复指向父级长程任务运行记录，使前端界面持续追踪总览状态。
        self.store.update_task(self.task_id, run_id=self.run_id)
        return sub

    def tool_run_skill(self, args: dict[str, Any]) -> ToolResult:
        flow_id = str(args["flow_id"])
        params = dict(args.get("params") or {})
        phase_id = args.get("phase_id")
        if self.store.get_flow(flow_id) is None:
            return ToolResult(f"Unknown flow_id {flow_id!r}. Available: {[f['id'] for f in self.store.list_flows()]}")
        self._mark_phase(phase_id, "running", f"running flow {flow_id}")
        sub = self._create_sub_flow_run(flow_id, params)
        r = self.supervisor.execute(
            sub["id"], self.cancel, runner=self.runner, unattended=self.unattended or self.d.settings().supervisor.unattended
        )
        self.last_flow_id = flow_id
        self._reload_scratch()
        out_key = args.get("output_state_key") or "last_skill_output"
        self.mission_state[out_key] = r.get("output") or {}
        if r["status"] == "succeeded":
            self._mark_phase(phase_id, "done", f"flow {flow_id} succeeded")
            self._save_checkpoint()
            return ToolResult(
                f"Skill {flow_id} SUCCEEDED (run {sub['id']}, version v{r.get('new_version') or r.get('version')}). "
                f"Output saved to state[{out_key!r}]: {json.dumps(r.get('output') or {}, ensure_ascii=False)[:3000]}"
            )
        self._mark_phase(phase_id, "failed", r.get("error") or "skill failed")
        self._save_checkpoint()
        return ToolResult(f"Skill {flow_id} FAILED: {r.get('error')}. Inspect with `observe` and recover or re-plan.")

    def tool_record_skill(self, args: dict[str, Any]) -> ToolResult:
        instruction = str(args["instruction"]).strip()
        params = dict(args.get("params") or {})
        start_url = args.get("start_url") or self.task.get("start_url") or None
        base_flow_id = args.get("base_flow_id") or None
        phase_id = args.get("phase_id")
        skill_key = args.get("skill_key") or phase_id or "skill"
        self._mark_phase(phase_id, "running", f"compiling skill: {instruction[:80]}")

        sub_run = self.store.create_run("author", params, task_id=self.task_id)
        self.store.append_task_run(self.task_id, sub_run["id"])
        self.store.update_task(self.task_id, run_id=self.run_id)
        sub_task = {"id": self.task_id, "instruction": instruction, "start_url": start_url, "params": params}
        res = self.author.record(
            sub_run["id"],
            sub_task,
            self.cancel,
            base_flow_id=base_flow_id,
            base_params=params if base_flow_id else None,
            runner=self.runner,
            turn_limit=self.d.settings().planner.sub_author_turn_limit,
            unattended=self.unattended or self.d.settings().supervisor.unattended,
            extra_tags=["compiled_skill"],
        )
        self._reload_scratch()
        if res.get("status") != "succeeded" or not res.get("flow_id"):
            self._mark_phase(phase_id, "failed", res.get("error") or "record_skill failed")
            return ToolResult(f"record_skill FAILED: {res.get('error')}. Inspect state or handle directly.")
        fid = res["flow_id"]
        self.compiled_flows[str(skill_key)] = fid
        self.last_flow_id = fid
        out_key = args.get("output_state_key") or f"{skill_key}_output"
        self.mission_state[out_key] = res.get("output") or {}
        self._mark_phase(phase_id, "done", f"compiled {fid}")
        self._save_checkpoint()
        return ToolResult(
            f"Compiled reusable Flow `{fid}` and executed it on current params! "
            f"Output in state[{out_key!r}]: {json.dumps(res.get('output') or {}, ensure_ascii=False)[:2500]}"
        )

    def tool_run_batch(self, args: dict[str, Any]) -> ToolResult:
        s = self.d.settings()
        batch_id = str(args.get("batch_id") or "batch")
        item_param = str(args.get("item_param") or "page_index")
        static_params = dict(args.get("static_params") or {})
        phase_id = args.get("phase_id")
        on_item_error = args.get("on_item_error") or "continue"
        out_key = str(args.get("output_state_key") or f"{batch_id}_outputs")

        raw_items = args.get("items")
        if raw_items is None and args.get("items_state_key"):
            raw_items = self.mission_state.get(str(args["items_state_key"]))
        paginate_mode = False
        if raw_items is None and args.get("max_iterations"):
            paginate_mode = True
            raw_items = list(range(min(int(args["max_iterations"]), s.planner.max_batch_items)))
        if not isinstance(raw_items, list) or not raw_items:
            return ToolResult(
                "run_batch requires a non-empty list (`items` or `items_state_key`) or `max_iterations` for stateful pagination."
            )
        items = raw_items[: s.planner.max_batch_items]

        bstate = self.batches.setdefault(batch_id, {
            "flow_id": args.get("flow_id") or None,
            "completed_count": 0,
            "total": len(items),
            "results": [],
        })
        bstate["total"] = len(items)
        flow_id: str | None = args.get("flow_id") or bstate.get("flow_id")
        start_idx = int(bstate.get("completed_count") or 0)
        results: list[dict[str, Any]] = list(bstate.get("results") or [])

        self._mark_phase(phase_id, "running", f"batch {start_idx}/{len(items)}")
        eff_unattended = self.unattended or s.supervisor.unattended

        for idx in range(start_idx, len(items)):
            if self.cancel.cancelled:
                self._reload_scratch()
                self._save_checkpoint()
                return ToolResult(f"Batch cancelled at item {idx}/{len(items)}.")
            item = items[idx]
            if item_param == "*" and isinstance(item, dict):
                iter_params = {**static_params, **item}
            else:
                iter_params = {**static_params, item_param: item}

            if not flow_id:
                unit_inst = str(args.get("unit_instruction") or "").strip()
                if not unit_inst:
                    self._reload_scratch()
                    return ToolResult("run_batch needs either an existing `flow_id` or `unit_instruction` to compile one.")
                sub_run = self.store.create_run("author", iter_params, task_id=self.task_id)
                self.store.append_task_run(self.task_id, sub_run["id"])
                self.store.update_task(self.task_id, run_id=self.run_id)
                sub_task = {
                    "id": self.task_id,
                    "instruction": unit_inst,
                    "start_url": args.get("start_url") or None,
                    "params": iter_params,
                }
                rec = self.author.record(
                    sub_run["id"],
                    sub_task,
                    self.cancel,
                    runner=self.runner,
                    turn_limit=s.planner.sub_author_turn_limit,
                    unattended=eff_unattended,
                    extra_tags=["batch_skill", f"batch:{batch_id}"],
                )
                if rec.get("status") != "succeeded" or not rec.get("flow_id"):
                    self._reload_scratch()
                    err = rec.get("error") or "failed to compile batch unit flow on first item"
                    self._mark_phase(phase_id, "failed", err)
                    return ToolResult(f"Batch failed while compiling unit skill on item 0 ({item!r}): {err}")
                flow_id = rec["flow_id"]
                bstate["flow_id"] = flow_id
                self.compiled_flows[batch_id] = flow_id
                self.last_flow_id = flow_id
                entry = {"index": idx, "item": item, "status": "succeeded", "mode": "compiled_v1",
                         "output": rec.get("output") or {}, "run_id": sub_run["id"]}
            else:
                sub = self._create_sub_flow_run(flow_id, iter_params)
                r = self.supervisor.execute(sub["id"], self.cancel, runner=self.runner, unattended=eff_unattended)
                entry = {
                    "index": idx,
                    "item": item,
                    "status": r["status"],
                    "mode": "script",
                    "version": r.get("new_version") or r.get("version"),
                    "output": r.get("output") or {},
                    "error": r.get("error"),
                    "run_id": sub["id"],
                }

            # 在长循环过程中保持浏览器整洁（关闭泄漏的弹窗标签页）
            self.guard.call(lambda: self.runner.heal_browser(keep_tabs=s.planner.tab_limit))

            if entry["status"] != "succeeded" and on_item_error == "abort":
                bstate["last_failed"] = entry
                self._save_checkpoint()
                self._reload_scratch()
                self._mark_phase(phase_id, "failed", f"stopped at item {idx}: {entry.get('error')}")
                return ToolResult(
                    f"Batch `{batch_id}` stopped at item {idx}/{len(items)} ({item!r}): {entry.get('error')}. "
                    f"Completed outputs so far in state[{out_key!r}]."
                )

            results.append(entry)
            bstate["results"] = results
            bstate["completed_count"] = idx + 1
            self.mission_state[out_key] = [x["output"] for x in results if x["status"] == "succeeded"]
            self.tr.emit("batch_progress", {
                "batch_id": batch_id,
                "completed": idx + 1,
                "total": len(items),
                "flow_id": flow_id,
                "last_status": entry["status"],
            }, step_id=phase_id)
            self._save_checkpoint()

            out_dict = entry.get("output") or {}
            if paginate_mode and entry["status"] == "succeeded" and (
                out_dict.get("done") is True or out_dict.get("has_next") is False
            ):
                bstate["total"] = idx + 1
                break

        self._reload_scratch()
        ok_n = sum(1 for x in results if x["status"] == "succeeded")
        fail_items = [{"index": x["index"], "item": x["item"], "error": x.get("error")}
                      for x in results if x["status"] != "succeeded"]
        status = "done" if not fail_items else "running"
        self._mark_phase(phase_id, status, f"batch {ok_n}/{len(items)} succeeded via {flow_id}")
        self._save_checkpoint()
        summary = {
            "batch_id": batch_id,
            "flow_id": flow_id,
            "total": len(items),
            "succeeded": ok_n,
            "failed": fail_items[:10],
            "output_state_key": out_key,
            "sample_outputs": [x["output"] for x in results if x["status"] == "succeeded"][:5],
        }
        return ToolResult("Batch execution summary:\n" + json.dumps(summary, ensure_ascii=False, default=str)[:5000])

    def tool_finish(self, args: dict[str, Any]) -> ToolResult:
        pending = [p["id"] for p in (self.plan.get("phases") or []) if p.get("status") in ("pending", "running")]
        if pending and not args.get("allow_incomplete"):
            return ToolResult(
                f"Phases still marked pending/running: {pending}. Complete them, update_plan their status to "
                f"`done`/`skipped`, or pass allow_incomplete=true if genuinely blocked."
            )
        out = dict(args.get("output") or {})
        return ToolResult("Mission finished.", done=True,
                          final={"final_response": args.get("final_response", ""), "output": out})

    # ------------------------------------------------------------------ 主循环
    def run(self) -> dict[str, Any]:
        s = self.d.settings()
        self.tr.status("running")
        if self.plan.get("phases"):
            self._sync_plan_to_tracker()
        self.runner: RunnerClient = self.d.new_runner(self.run_dir)
        self.guard = RunnerGuard(self.runner, self.run_dir)
        with usage_scope(self.meter):
            try:
                self._reload_scratch()
                shell = self.d.shell_factory()
                base_tools = common_tools(self.runner, self.run_dir, shell)
                # 包装 `python` 工具，使任何对 `c.state` 的修改都能立即同步至 `mission_state` 与检查点！
                wrapped_tools: list[Tool] = []
                for t in base_tools:
                    if t.name == "python":
                        orig = t.handler

                        def py_and_sync(a: dict[str, Any], _orig=orig) -> ToolResult:
                            r = _orig(a)
                            self._sync_state_from_runner()
                            return r

                        wrapped_tools.append(Tool(t.spec, py_and_sync))
                    else:
                        wrapped_tools.append(t)

                tools = wrapped_tools + [
                    Tool(
                        tool_spec(
                            "update_plan",
                            "Create or revise the multi-phase execution plan and phase statuses.",
                            {
                                "goal": {"type": "string"},
                                "phases": {"type": "array", "items": PHASE_SCHEMA},
                            },
                            ["phases"],
                        ),
                        self.tool_update_plan,
                    ),
                    Tool(
                        tool_spec(
                            "save_state",
                            "Persist a key/value in mission_state (checkpointed to disk for crash recovery).",
                            {"key": {"type": "string"}, "value": {}},
                            ["key", "value"],
                        ),
                        self.tool_save_state,
                    ),
                    Tool(
                        tool_spec(
                            "read_state",
                            "Read a key (or overview of all keys) from mission_state.",
                            {"key": {"type": "string", "description": "optional key name"}},
                            [],
                        ),
                        self.tool_read_state,
                    ),
                    Tool(
                        tool_spec(
                            "run_skill",
                            "Execute an existing parameterized Flow from the library under Supervisor monitoring (0 exploration tokens).",
                            {
                                "flow_id": {"type": "string"},
                                "params": {"type": "object"},
                                "phase_id": {"type": "string"},
                                "output_state_key": {"type": "string"},
                            },
                            ["flow_id"],
                        ),
                        self.tool_run_skill,
                    ),
                    Tool(
                        tool_spec(
                            "record_skill",
                            "Execute a repeatable sub-task via Author AND save it as a parameterized Flow in the library.",
                            {
                                "instruction": {"type": "string"},
                                "params": {"type": "object"},
                                "start_url": {"type": "string"},
                                "base_flow_id": {"type": "string"},
                                "phase_id": {"type": "string"},
                                "skill_key": {"type": "string"},
                                "output_state_key": {"type": "string"},
                            },
                            ["instruction"],
                        ),
                        self.tool_record_skill,
                    ),
                    Tool(
                        tool_spec(
                            "run_batch",
                            "Process a list of items OR a stateful pagination loop (via max_iterations, stopping when output.done==true or output.has_next==false) using a compiled Flow skill.",
                            {
                                "batch_id": {"type": "string"},
                                "items": {"type": "array", "items": {}, "description": "explicit item list (or use items_state_key / max_iterations)"},
                                "items_state_key": {"type": "string", "description": "key in mission_state holding the item list"},
                                "max_iterations": {"type": "integer", "description": "for stateful JS pagination/queue loops without upfront item list"},
                                "item_param": {"type": "string", "description": "Flow parameter name receiving each item/page_index (default 'page_index')"},
                                "flow_id": {"type": "string", "description": "existing Flow ID, or omit to compile from unit_instruction on iteration 0"},
                                "unit_instruction": {"type": "string", "description": "instruction for processing ONE item/page"},
                                "static_params": {"type": "object", "description": "constant params passed on every iteration"},
                                "start_url": {"type": "string"},
                                "output_state_key": {"type": "string"},
                                "on_item_error": {"type": "string", "enum": ["continue", "abort"]},
                                "phase_id": {"type": "string"},
                            },
                            [],
                        ),
                        self.tool_run_batch,
                    ),
                    Tool(
                        tool_spec(
                            "finish",
                            "Complete the mission once all phases are done.",
                            {
                                "final_response": {"type": "string"},
                                "output": {"type": "object"},
                                "allow_incomplete": {"type": "boolean"},
                            },
                            ["final_response"],
                        ),
                        self.tool_finish,
                    ),
                ]

                catalog = [
                    {
                        "flow_id": f["id"],
                        "name": f["name"],
                        "intent_template": f.get("intent_template"),
                        "params": f.get("params_schema"),
                        "output": f.get("output_schema"),
                        "start_url": f.get("start_url"),
                    }
                    for f in self.store.list_flows()
                ]
                intro = [f"# Long-Horizon Mission\n{self.task['instruction']}"]
                if self.task.get("start_url"):
                    intro.append(f"Start URL: {self.task['start_url']}")
                if self.task.get("params"):
                    intro.append(f"Initial params: {json.dumps(self.task['params'], ensure_ascii=False)}")
                intro.append(f"Current local time: {time.strftime('%Y-%m-%d %H:%M:%S %A')}")
                intro.append(f"## Available Flow Library ({len(catalog)} skills)\n"
                             f"{json.dumps(catalog, ensure_ascii=False, indent=1) if catalog else '(empty)'}")
                if self.resume and (self.plan.get("phases") or self.batches or self.mission_state):
                    intro.append(
                        "## Resumed Checkpoint (DO NOT repeat already completed phases or batch items!)\n"
                        f"Plan: {json.dumps(self.plan, ensure_ascii=False)}\n"
                        f"Compiled flows: {json.dumps(self.compiled_flows, ensure_ascii=False)}\n"
                        f"Batches progress: {json.dumps({k: {'flow_id': v.get('flow_id'), 'completed_count': v.get('completed_count'), 'total': v.get('total')} for k, v in self.batches.items()}, ensure_ascii=False)}\n"
                        f"Mission state keys: {sorted(self.mission_state)}"
                    )

                agent = ToolAgent(
                    self.d.llms.get("planner"),
                    load_prompt("planner"),
                    tools,
                    role="planner",
                    on_log=lambda k, t: self.tr.log("planner", k, t),
                    should_stop=lambda: self.cancel.cancelled,
                )
                res = agent.run("\n\n".join(intro), turn_limit=s.planner.turn_limit)
                self._save_checkpoint()
                if self.cancel.cancelled:
                    return self.tr.status("aborted", error="aborted by user", usage=self.meter.snapshot())
                if not res.done:
                    return self.tr.status(
                        "failed",
                        error=f"mission planner did not finish: {res.reason}",
                        usage=self.meter.snapshot(),
                    )
                final = res.final or {}
                output = dict(final.get("output") or {})
                if not output and self.mission_state:
                    output = json.loads(json.dumps(self.mission_state, default=str, ensure_ascii=False))
                run = self.tr.status("succeeded", output=output, usage=self.meter.snapshot())
                return {
                    **run,
                    "final_response": final.get("final_response", ""),
                    "flow_id": self.last_flow_id,
                    "usage": self.meter.snapshot(),
                }
            finally:
                self.runner.close()


class MissionPlanner:
    def __init__(self, deps: Deps, supervisor: Supervisor, author: Author):
        self.d = deps
        self.supervisor = supervisor
        self.author = author

    def execute(
        self,
        run_id: str,
        task: dict[str, Any],
        cancel: CancelToken | None = None,
        *,
        resume: bool = False,
        unattended: bool = False,
    ) -> dict[str, Any]:
        sess = MissionSession(
            self.d,
            self.supervisor,
            self.author,
            run_id,
            task,
            cancel or CancelToken(),
            resume=resume,
            unattended=unattended,
        )
        try:
            return sess.run()
        except Exception as exc:  # noqa: BLE001
            sess.tr.log("planner", "observation", f"internal error: {type(exc).__name__}: {exc}")
            return sess.tr.status("failed", error=f"{type(exc).__name__}: {exc}", usage=sess.meter.snapshot())
