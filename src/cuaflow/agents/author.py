"""Author agent: accomplishes a new task while recording it as a step-based flow ("record while doing")."""

from __future__ import annotations

import time
from typing import Any

from cuaflow.agents.loop import Tool, ToolAgent, ToolResult
from cuaflow.agents.supervisor import Deps
from cuaflow.agents.tools import RunnerGuard, StepVerifier, common_tools, format_step_result, load_prompt, shot_path
from cuaflow.agents.tracking import CancelToken, RunTracker
from cuaflow.flow.client import RunnerError
from cuaflow.flow.flow import load_flow
from cuaflow.flow.patching import (extract_steps, remove_step, render_flow_header,
                                   reorder_steps, replace_step, validate_step_source)
from cuaflow.llm.base import tool_spec

PLAN_SCHEMA = {
    "name": {"type": "string", "description": "short flow name (user's language)"},
    "description": {"type": "string"},
    "intent_template": {"type": "string", "description": "task template with {param} placeholders"},
    "side_effects": {"type": "boolean", "description": "true if the flow performs irreversible actions"},
    "start_url": {"type": "string", "description": "initial URL if browser based, else empty"},
    "params": {"type": "object", "description": "name -> {type: str|int|float|bool, description, example}",
               "additionalProperties": {"type": "object"}},
    "output": {"type": "object", "description": "name -> {type, description}", "additionalProperties": {"type": "object"}},
    "steps": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "title": {"type": "string"}, "expect": {"type": "string"}},
        "required": ["id", "title", "expect"]}},
}


class AuthorSession:
    def __init__(self, deps: Deps, run_id: str, task: dict[str, Any], cancel: CancelToken,
                 base_flow_id: str | None = None, base_params: dict[str, Any] | None = None):
        self.d, self.run_id, self.task, self.cancel = deps, run_id, task, cancel
        self.store = deps.store
        self.run_dir = self.store.run_dir(run_id)
        self.draft = self.run_dir / "draft_flow.py"
        self.tr = RunTracker(self.store, deps.bus, run_id, task_id=task.get("id"))
        self.plan: dict[str, Any] | None = None
        self.committed: set[str] = set()
        self.params: dict[str, Any] = dict(base_params or task.get("params") or {})
        self.base_flow_id = base_flow_id

    # ------------------------------------------------------------------ draft helpers
    def _source(self) -> str:
        return self.draft.read_text(encoding="utf-8")

    def _write(self, src: str) -> None:
        self.draft.write_text(src, encoding="utf-8")

    def _plan_ids(self) -> list[str]:
        return [s["id"] for s in (self.plan or {}).get("steps", [])]

    def _ordered(self, src: str) -> str:
        present = [s.step_id for s in extract_steps(src)]
        order = [i for i in self._plan_ids() if i in present] + [i for i in present if i not in self._plan_ids()]
        return reorder_steps(src, order) if present and order != present else src

    # ------------------------------------------------------------------ tools
    def plan_flow(self, a: dict[str, Any]) -> ToolResult:
        steps = a.get("steps") or []
        ids = [s.get("id", "") for s in steps]
        if not steps or len(set(ids)) != len(ids) or not all(i.isidentifier() for i in ids):
            return ToolResult("Plan rejected: steps need unique snake_case python identifiers as ids.")
        params_schema, examples = {}, {}
        for name, spec in (a.get("params") or {}).items():
            spec = dict(spec) if isinstance(spec, dict) else {"type": str(spec)}
            if "example" in spec:
                examples[name] = spec["example"]
            spec.setdefault("type", "str")
            params_schema[name] = spec
        start_url = a.get("start_url") or None
        header = render_flow_header(a.get("name") or "flow", a.get("description", ""), a.get("intent_template", ""),
                                    params_schema, a.get("output") or {}, bool(a.get("side_effects")), start_url)
        old = self._source() if self.draft.exists() else ""
        kept = {s.step_id: s.text for s in extract_steps(old)} if old else {}
        src = header
        for sid in ids:
            if sid in kept:
                src = replace_step(src, sid, kept[sid])
        self.plan = {**a, "params": params_schema, "steps": steps}
        self.committed &= set(ids) & set(kept)
        self.params = {**self.params, **examples}
        self._write(self._ordered(src))
        try:
            state = self.d_runner.call("get_state")
            self.d_runner.load(self.draft, self.params, self.run_dir, state=state, **self.d.runner_opts)
        except RunnerError as exc:
            return ToolResult(f"Plan saved but the flow failed to load: {exc}. Fix the plan (params?).")
        self.tr.init_steps(steps, keep_existing=True)
        return ToolResult(f"Plan saved with {len(steps)} steps: {ids}. Run params for this session: {self.params}. "
                          f"Already committed: {sorted(self.committed)}. Now implement each step with try_step.")

    def try_step(self, a: dict[str, Any]) -> ToolResult:
        if self.plan is None:
            return ToolResult("Call plan_flow first.")
        src_step = a["source"]
        try:
            sid = validate_step_source(src_step)
        except ValueError as exc:
            return ToolResult(f"Rejected: {exc}")
        if sid not in self._plan_ids():
            return ToolResult(f"Step id {sid!r} is not in the plan {self._plan_ids()}; update plan_flow first.")
        new_src = self._ordered(replace_step(self._source(), sid, src_step))
        try:
            self.d_runner.replace_step(src_step, expected_id=sid)
        except RunnerError as exc:
            return ToolResult(f"Rejected by runner: {exc}")
        self._write(new_src)
        step = next(s for s in self.plan["steps"] if s["id"] == sid)
        spec = {**step, **next((x for x in self.d_runner.call("steps") if x["id"] == sid), {})}
        self.tr.step_start(sid)
        result, err = self.guard.call(lambda: self.d_runner.run_step(sid, timeout=float(spec.get("timeout") or 120)))
        if err:
            self.committed.discard(sid)
            self.tr.set_step_status(sid, "failed", error=err)
            return ToolResult(f"Step run aborted: {err}")
        passed, checks = self.verifier.verify(spec, result, use_l2=self.verifier.judge_llm is not None)
        self.tr.step_result(sid, result, checks, "passed" if passed else "failed")
        (self.committed.add if passed else self.committed.discard)(sid)
        remaining = [i for i in self._plan_ids() if i not in self.committed]
        text = format_step_result(result, checks, passed)
        text += ("\nCommitted to the flow." if passed else "\nNot committed; fix and retry.")
        text += f"\nRemaining steps: {remaining or 'none — call finish when the task is complete'}"
        return ToolResult(text, images=shot_path(self.run_dir, result.get("screenshot")))

    def remove(self, a: dict[str, Any]) -> ToolResult:
        sid = a["step_id"]
        self._write(remove_step(self._source(), sid))
        if self.plan:
            self.plan["steps"] = [s for s in self.plan["steps"] if s["id"] != sid]
            self.tr.init_steps(self.plan["steps"], keep_existing=True)
        self.committed.discard(sid)
        return ToolResult(f"Removed step {sid}. Plan: {self._plan_ids()}")

    def finish(self, a: dict[str, Any]) -> ToolResult:
        if self.plan is None:
            return ToolResult("Cannot finish: no plan. Call plan_flow and record the steps first.")
        missing = [i for i in self._plan_ids() if i not in self.committed]
        if missing:
            return ToolResult(f"Cannot finish: steps not yet passed via try_step: {missing}. Implement them, or "
                              f"remove steps that are not needed (remove_step).")
        try:
            load_flow(self.draft)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(f"Cannot finish: draft flow does not import: {exc}")
        return ToolResult("Finished.", done=True, final={"final_response": a.get("final_response", "")})

    def ask_user(self, question: str) -> str:
        self.tr.status("waiting_human")
        t = self.d.tickets.open(self.run_id, f"Author 提问: {question}")
        res = self.d.tickets.wait(t["id"], self.cancel)
        self.tr.status("running")
        if res.get("action") == "abort":
            from cuaflow.agents.loop import Cancelled

            self.cancel.cancel()
            raise Cancelled()
        return res.get("hint") or "(no answer — proceed with your best judgement)"

    # ------------------------------------------------------------------ main
    def run(self) -> dict[str, Any]:
        s = self.d.settings()
        tr = self.tr
        tr.status("running")
        if self.base_flow_id:
            base = self.store.get_flow(self.base_flow_id) or {}
            self._write(self.store.read_source(self.base_flow_id))
        else:
            self._write(render_flow_header("exploration"))
        self.d_runner = self.d.new_runner(self.run_dir)
        self.guard = RunnerGuard(self.d_runner, self.run_dir)
        judge = self.d.llms.get("vision_judge") if s.supervisor.l2_policy != "off" else None
        self.verifier = StepVerifier(judge, self.run_dir, self.task.get("instruction", ""))
        try:
            try:
                self.d_runner.load(self.draft, self.params if self.base_flow_id else {}, self.run_dir,
                                   **self.d.runner_opts)
            except RunnerError:
                self._write(render_flow_header("exploration"))
                self.d_runner.load(self.draft, {}, self.run_dir, **self.d.runner_opts)
            shell = self.d.shell_factory()
            tools = common_tools(self.d_runner, self.run_dir, shell, ask_user=self.ask_user) + [
                Tool(tool_spec("plan_flow", "Declare/revise the flow: metadata, parameters (with example values "
                                            "from this task), output schema and ordered steps.", PLAN_SCHEMA,
                               ["name", "params", "steps"]), self.plan_flow),
                Tool(tool_spec("try_step", "Add/replace a planned step (full @flow.step-decorated function source) "
                                           "and run it now in the live session; commits it if it passes.",
                               {"source": {"type": "string"}}), self.try_step),
                Tool(tool_spec("remove_step", "Remove a step from the plan and the flow.",
                               {"step_id": {"type": "string"}}), self.remove),
                Tool(tool_spec("finish", "Finish when all planned steps passed and the task is accomplished.",
                               {"final_response": {"type": "string"}}), self.finish),
            ]
            agent = ToolAgent(self.d.llms.get("author"), load_prompt("author"), tools, role="author",
                              on_log=lambda k, t: tr.log("author", k, t), should_stop=lambda: self.cancel.cancelled)
            intro = [f"# Task\n{self.task['instruction']}"]
            if self.task.get("start_url"):
                intro.append(f"Start URL: {self.task['start_url']}")
            if self.params:
                intro.append(f"Given parameters: {self.params}")
            intro.append(f"Current local time: {time.strftime('%Y-%m-%d %H:%M:%S %A')}")
            if self.base_flow_id:
                base = self.store.get_flow(self.base_flow_id) or {}
                intro.append(f"An existing similar flow was loaded as the starting draft (adapt it; keep steps that "
                             f"still fit). Its metadata: name={base.get('name')!r}, intent="
                             f"{base.get('intent_template')!r}, params={base.get('params_schema')}.\n"
                             f"```python\n{self._source()}\n```\nYou still must call plan_flow and pass every step "
                             f"with try_step in this session.")
            res = agent.run("\n\n".join(intro), turn_limit=s.author.turn_limit)
            if self.cancel.cancelled:
                return tr.status("aborted", error="aborted by user")
            if not res.done:
                return tr.status("failed", error=f"author did not finish: {res.reason}")
            out = self.d_runner.finish()
            flow_obj, _ = load_flow(self.draft)
            meta = flow_obj.meta()
            tags = [f"derived_from:{self.base_flow_id}"] if self.base_flow_id else []
            flow = self.store.create_flow(meta, self._source(), author="author",
                                          reason="Author 录制: " + self.task["instruction"][:200], tags=tags)
            self.store.update_run(self.run_id, flow_id=flow["id"], version=1, new_version=1,
                                  params=self.params)
            tr.flow_id = flow["id"]
            tr.emit("version_created", self.store.get_version(flow["id"], 1, with_source=False) or {})
            final = (res.final or {}).get("final_response", "")
            run = tr.status("succeeded", output=out.get("output") or {})
            return {**run, "final_response": final}
        finally:
            self.d_runner.close()


class Author:
    def __init__(self, deps: Deps):
        self.d = deps

    def record(self, run_id: str, task: dict[str, Any], cancel: CancelToken | None = None,
               base_flow_id: str | None = None, base_params: dict[str, Any] | None = None) -> dict[str, Any]:
        sess = AuthorSession(self.d, run_id, task, cancel or CancelToken(), base_flow_id, base_params)
        try:
            return sess.run()
        except Exception as exc:  # noqa: BLE001
            sess.tr.log("author", "observation", f"internal error: {type(exc).__name__}: {exc}")
            return sess.tr.status("failed", error=f"{type(exc).__name__}: {exc}")
