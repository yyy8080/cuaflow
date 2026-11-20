"""自主兜底智能体 `FallbackAgent`：当单步代码修补不足以恢复时，交互式修复实时浏览器或桌面状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cuaflow.agents.judge import judge_step
from cuaflow.agents.loop import Tool, ToolAgent, ToolResult
from cuaflow.agents.sentinel import inspect_step
from cuaflow.agents.tools import RunnerGuard, check, common_tools, format_step_result, load_prompt, shot_path
from cuaflow.flow.client import RunnerClient, RunnerError
from cuaflow.flow.engine import now_iso
from cuaflow.flow.patching import validate_step_source
from cuaflow.llm.base import LLM, image_part, text_part, tool_spec
from cuaflow.platform.base import ShellBackend


@dataclass
class FallbackOutcome:
    recovered: bool
    summary: str = ""
    step_source: str | None = None
    result: dict[str, Any] | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0


class FallbackAgent:
    """第 2 级自主接管：通过交互式操作恢复实时状态，使受阻流程得以继续推进。"""

    def __init__(
        self,
        llm: LLM,
        judge_llm: LLM | None,
        runner: RunnerClient,
        run_dir: Path,
        shell: ShellBackend,
        on_log: Callable[[str, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        drain_steering: Callable[[], list[str]] | None = None,
        turn_limit: int = 25,
    ):
        self.llm = llm
        self.judge_llm = judge_llm
        self.runner = runner
        self.run_dir = Path(run_dir)
        self.shell = shell
        self.on_log = on_log or (lambda k, t: None)
        self.should_stop = should_stop
        self.drain_steering = drain_steering
        self.turn_limit = turn_limit

    def recover(
        self,
        flow_source: str,
        step: dict[str, Any],
        failure: dict[str, Any],
        checks: list[dict[str, Any]],
        *,
        intent: str = "",
        repair_history: list[str] | None = None,
    ) -> FallbackOutcome:
        step_id = step["id"]
        guard = RunnerGuard(self.runner, self.run_dir)

        def complete_step(args: dict[str, Any]) -> ToolResult:
            state_upd = args.get("state_updates")
            if isinstance(state_upd, dict) and state_upd:
                cur_state, err = guard.call(lambda: self.runner.call("get_state"))
                if not err and isinstance(cur_state, dict):
                    self.runner.call("set_state", state={**cur_state, **state_upd})
            out_upd = args.get("output_updates")
            if isinstance(out_upd, dict) and out_upd:
                guard.call(lambda: self.runner.update_output(out_upd))

            patched_src = (args.get("patched_source") or "").strip() or None
            if patched_src:
                try:
                    validate_step_source(patched_src, expected_id=step_id)
                    self.runner.replace_step(patched_src, expected_id=step_id)
                except (ValueError, RunnerError) as exc:
                    return ToolResult(f"patched_source invalid ({exc}); fix it or omit patched_source.")

            obs, err = guard.call(lambda: self.runner.observe(detail=False, tag=f"fallback_{step_id}"))
            if err:
                return ToolResult(f"Cannot verify live state: {err}")

            synth_result: dict[str, Any] = {
                "step_id": step_id,
                "title": step.get("title", step_id),
                "expect": step.get("expect", ""),
                "status": "passed",
                "attempt": int(failure.get("attempt") or 1) + 1,
                "ended_at": now_iso(),
                "duration_ms": 0,
                "stdout": f"[autonomous fallback] {args.get('summary', '')}",
                "surfaces": ["browser"] if obs.get("url") else [],
                "observation": obs,
                "screenshot": obs.get("screenshot"),
            }
            sr = inspect_step(step, synth_result)
            chks = [check("L0", True, f"recovered by autonomous fallback: {args.get('summary', 'ok')}")]
            if not sr.passed:
                chks.append(check("L1.5", False, sr.detail))
                return ToolResult(
                    f"Verification failed: {sr.detail}. Fix the state before calling complete_step.",
                    images=shot_path(self.run_dir, obs.get("screenshot")),
                )
            chks.append(check("L1.5", True, sr.detail))
            if self.judge_llm is not None and step.get("expect"):
                verdict = judge_step(self.judge_llm, step, synth_result, self.run_dir, intent)
                if verdict["passed"] is False:
                    chks.append(check("L2", False, verdict["reason"]))
                    return ToolResult(
                        f"Supervisor L2 rejected completion: {verdict['reason']}. Ensure `{step.get('expect')}` is met.",
                        images=shot_path(self.run_dir, obs.get("screenshot")),
                    )
                if verdict["passed"] is True:
                    chks.append(check("L2", True, verdict["reason"]))

            return ToolResult(
                "Step recovery verified.",
                images=shot_path(self.run_dir, obs.get("screenshot")),
                done=True,
                final={
                    "recovered": True,
                    "summary": args.get("summary") or "recovered by autonomous fallback",
                    "step_source": patched_src,
                    "result": synth_result,
                    "checks": chks,
                },
            )

        def give_up(args: dict[str, Any]) -> ToolResult:
            return ToolResult("Stopping autonomous fallback.", done=True,
                              final={"recovered": False, "reason": args.get("reason", "unrecoverable")})

        from cuaflow.agents.builtin_tools import build_builtin_tools

        tools = build_builtin_tools(self.runner, self.run_dir) + common_tools(self.runner, self.run_dir, self.shell) + [
            Tool(
                tool_spec(
                    "complete_step",
                    "Declare that you have recovered the live state and satisfied this step's expected outcome.",
                    {
                        "summary": {"type": "string", "description": "what went wrong and how you recovered it"},
                        "state_updates": {"type": "object", "description": "optional keys to merge into c.state"},
                        "output_updates": {"type": "object", "description": "optional keys to merge into c.output"},
                        "patched_source": {
                            "type": "string",
                            "description": "optional updated @flow.step function source if a general code fix applies",
                        },
                    },
                    ["summary"],
                ),
                complete_step,
            ),
            Tool(
                tool_spec(
                    "give_up",
                    "Stop when blocked by CAPTCHA, missing credentials/2FA, or an unrecoverable condition.",
                    {"reason": {"type": "string"}},
                    ["reason"],
                ),
                give_up,
            ),
        ]

        agent = ToolAgent(self.llm, load_prompt("fallback"), tools, role="fallback",
                          on_log=self.on_log, should_stop=self.should_stop, drain_steering=self.drain_steering)
        prompt_sections = [
            f"# Overall Task Intent\n{intent or '(not specified)'}",
            f"# Blocked Step: `{step_id}` — {step.get('title')}\nExpected outcome: {step.get('expect') or '-'}",
            f"## Failure Details\n{format_step_result(failure, checks, False)}",
            f"## Full Flow Source (so you see what `c.state` / `c.output` downstream steps need)\n```python\n{flow_source}\n```",
        ]
        if repair_history:
            prompt_sections.append("## Failed Single-Step Repair Attempts\n" + "\n".join(f"- {h}" for h in repair_history))
        parts: list[dict[str, Any]] = [text_part("\n\n".join(prompt_sections))]
        for p in shot_path(self.run_dir, failure.get("screenshot")):
            parts += [text_part("Screenshot at failure:"), image_part(p)]

        res = agent.run(parts, turn_limit=self.turn_limit)
        final = res.final or {}
        if res.done and final.get("recovered"):
            return FallbackOutcome(
                recovered=True,
                summary=final.get("summary", "recovered"),
                step_source=final.get("step_source"),
                result=final.get("result"),
                checks=final.get("checks", []),
                turns=res.turns,
            )
        reason = final.get("reason") if res.done else res.reason
        return FallbackOutcome(recovered=False, summary=f"fallback unsuccessful: {reason}", turns=res.turns)
