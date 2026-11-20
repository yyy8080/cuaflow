"""Repairer agent: patches ONE failing step in the live, paused runner session."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cuaflow.agents.loop import Tool, ToolAgent, ToolResult
from cuaflow.agents.tools import (RunnerGuard, StepVerifier, common_tools, format_step_result, load_prompt,
                                  shot_path)
from cuaflow.flow.client import RunnerClient, RunnerError
from cuaflow.flow.patching import get_step_source, validate_step_source
from cuaflow.llm.base import LLM, image_part, text_part, tool_spec
from cuaflow.platform.base import ShellBackend


@dataclass
class RepairOutcome:
    success: bool
    step_source: str | None = None
    summary: str = ""
    result: dict[str, Any] | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    gave_up: bool = False
    turns: int = 0


class Repairer:
    def __init__(self, llm: LLM, judge_llm: LLM | None, runner: RunnerClient, run_dir: Path, shell: ShellBackend,
                 on_log: Callable[[str, str], None] | None = None, should_stop: Callable[[], bool] | None = None,
                 turn_limit: int = 30):
        self.llm, self.judge_llm, self.runner = llm, judge_llm, runner
        self.run_dir, self.shell = Path(run_dir), shell
        self.on_log = on_log or (lambda k, t: None)
        self.should_stop = should_stop
        self.turn_limit = turn_limit

    def repair(self, flow_source: str, step: dict[str, Any], failure: dict[str, Any], checks: list[dict[str, Any]],
               intent: str = "", hint: str | None = None, history: list[str] | None = None) -> RepairOutcome:
        step_id = step["id"]
        verifier = StepVerifier(self.judge_llm, self.run_dir, intent)
        guard = RunnerGuard(self.runner, self.run_dir)
        use_l2 = self.judge_llm is not None and bool(step.get("expect"))
        last: dict[str, Any] = {}

        def try_step(args: dict[str, Any]) -> ToolResult:
            source = args["source"]
            try:
                validate_step_source(source, expected_id=step_id)
                self.runner.replace_step(source, expected_id=step_id)
            except (ValueError, RunnerError) as exc:
                return ToolResult(f"Patch rejected: {exc}")
            result, err = guard.call(lambda: self.runner.run_step(step_id, timeout=float(step.get("timeout") or 120)))
            if err:
                return ToolResult(f"Step run aborted: {err}. Re-apply your patch with try_step after checking state.")
            passed, chks = verifier.verify(step, result, use_l2=use_l2)
            last.update(result=result, checks=chks)
            text = format_step_result(result, chks, passed)
            images = shot_path(self.run_dir, result.get("screenshot"))
            if passed:
                return ToolResult(text, images=images, done=True,
                                  final={"source": source, "result": result, "checks": chks,
                                         "summary": args.get("summary", "")})
            return ToolResult(text, images=images)

        def give_up(args: dict[str, Any]) -> ToolResult:
            return ToolResult("Escalating to a human.", done=True, final={"gave_up": args.get("reason", "")})

        tools = common_tools(self.runner, self.run_dir, self.shell) + [
            Tool(tool_spec("try_step", "Replace the failing step with this source (full @flow.step-decorated "
                                       "function, same id) and run it now in the live session.",
                           {"source": {"type": "string"},
                            "summary": {"type": "string", "description": "one-line description of the fix"}},
                           ["source"]), try_step),
            Tool(tool_spec("give_up", "Stop: the step cannot be repaired automatically; explain why.",
                           {"reason": {"type": "string"}}), give_up),
        ]
        agent = ToolAgent(self.llm, load_prompt("repairer"), tools, role="repairer", on_log=self.on_log,
                          should_stop=self.should_stop)
        try:
            current = get_step_source(flow_source, step_id)
        except KeyError:
            current = "(step source unavailable)"
        text = [
            f"# Task intent\n{intent or '(not given)'}",
            f"# Failing step: `{step_id}` — {step.get('title')}\nExpected outcome: {step.get('expect') or '-'}",
            f"## Current step source\n```python\n{current}\n```",
            f"## Failure\n{format_step_result(failure, checks, False)}",
            f"## Full flow source (context only — change ONLY step `{step_id}`)\n```python\n{flow_source}\n```",
        ]
        if history:
            text.append("## Previous repair attempts in this run (failed)\n" + "\n".join(f"- {h}" for h in history))
        if hint:
            text.append(f"## Hint from the human operator\n{hint}")
        parts: list[dict[str, Any]] = [text_part("\n\n".join(text))]
        for p in shot_path(self.run_dir, failure.get("screenshot")):
            parts += [text_part("Screenshot right after the failure:"), image_part(p)]
        res = agent.run(parts, turn_limit=self.turn_limit)
        final = res.final or {}
        if res.done and final.get("source"):
            return RepairOutcome(True, final["source"], final.get("summary") or "step repaired", final["result"],
                                 final["checks"], turns=res.turns)
        if res.done and "gave_up" in final:
            return RepairOutcome(False, summary=f"gave up: {final['gave_up']}", gave_up=True, turns=res.turns,
                                 result=last.get("result"), checks=last.get("checks", []))
        return RepairOutcome(False, summary=f"repair stopped: {res.reason}", turns=res.turns,
                             result=last.get("result"), checks=last.get("checks", []))
