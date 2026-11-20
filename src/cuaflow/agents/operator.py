"""直连执行子智能体 `DirectOperator`：集成内置工具集、双层自动上下文压缩与 `Supervisor` 实时巡检及兜底接管。"""

from __future__ import annotations

import json
import time
from typing import Any

from cuaflow.agents.builtin_tools import build_builtin_tools
from cuaflow.agents.loop import AgentResult, Cancelled, Tool, ToolAgent, ToolResult
from cuaflow.agents.supervisor import Deps, Supervisor
from cuaflow.agents.tools import RunnerGuard, common_tools, load_prompt, shot_path
from cuaflow.agents.tracking import CancelToken, RunTracker
from cuaflow.flow.client import RunnerClient, RunnerError
from cuaflow.flow.patching import render_flow_header
from cuaflow.llm import UsageMeter, usage_scope
from cuaflow.llm.base import tool_spec


class DirectOperator:
    """面向即席探索、多站点并行调研与桌面操作的生产级直连执行子智能体。"""

    def __init__(self, deps: Deps):
        self.d = deps

    def execute(
        self,
        run_id: str,
        instruction: str,
        cancel: CancelToken | None = None,
        *,
        surface: str = "browser",
        start_url: str | None = None,
        params: dict[str, Any] | None = None,
        unattended: bool | None = None,
        turn_limit: int | None = None,
        runner: RunnerClient | None = None,
    ) -> dict[str, Any]:
        """在独立运行器进程中执行任务：具备内置工具集、自动滚动压缩、看门狗实时纠偏与 Supervisor 兜底接管能力。"""
        cancel = cancel or CancelToken()
        store, s = self.d.store, self.d.settings()
        effective_unattended = s.supervisor.unattended if unattended is None else bool(unattended)
        meter = UsageMeter(tokens_per_script_step=s.planner.tokens_saved_per_step)
        run_dir = store.run_dir(run_id)
        scratch_flow = run_dir / "scratch_flow.py"
        scratch_flow.write_text(render_flow_header(f"direct_{surface}", start_url=start_url), encoding="utf-8")

        tr = RunTracker(store, self.d.bus, run_id)
        tr.status("running")

        owns_runner = runner is None
        rc = runner or self.d.new_runner(run_dir)
        guard = RunnerGuard(rc, run_dir)
        supervisor = Supervisor(self.d)
        role_name = "desktop" if surface == "desktop" else "explorer"
        captcha_aborted: list[str] = []

        def request_approval(reason: str, screenshot_rel: str | None = None) -> str:
            """挂起当前子智能体线程并创建审批工单，等待用户在对话或面板中放行。"""
            shot = screenshot_rel
            if not shot:
                obs, err = guard.call(lambda: rc.observe(detail=False, tag="approval"))
                if not err and isinstance(obs, dict):
                    shot = obs.get("screenshot")
            tr.status("waiting_human")
            t = self.d.tickets.open(run_id, reason, screenshot=shot)
            res = self.d.tickets.wait(t["id"], cancel)
            tr.status("running")
            action = res.get("action") or "abort"
            if action == "abort":
                cancel.cancel()
                raise Cancelled()
            if action == "skip":
                return "用户选择跳过该动作，请勿执行该敏感步骤，继续后续安全操作或汇总结果。"
            if action == "hint":
                return f"用户已放行并给出补充指示：{res.get('hint') or '请按最新状态继续'}"
            return f"用户已批准放行（Approved）。{(' 补充说明：' + res['hint']) if res.get('hint') else '请继续执行。'}"

        def handle_captcha_event(payload: dict[str, Any]) -> str:
            msg = "检测到人机验证（CAPTCHA），触发安全边界保护，严禁自动绕过。"
            if effective_unattended:
                captcha_aborted.append(msg)
                cancel.cancel()
                raise Cancelled()
            note = request_approval(
                "页面出现人机验证（CAPTCHA），子智能体已安全暂停，请在浏览器中人工完成后点击【放行】。",
                screenshot_rel=payload.get("screenshot"),
            )
            return f"CAPTCHA 安全暂停已由人工处理：{note}。请重新观察最新页面后继续。"

        def emit_live_observation(payload: dict[str, Any]) -> None:
            tr.emit(
                "worker_observation",
                {
                    "screenshot": payload.get("screenshot"),
                    "url": payload.get("url"),
                    "title": payload.get("title"),
                },
            )

        with usage_scope(meter):
            try:
                try:
                    rc.load(scratch_flow, params or {}, run_dir, **self.d.runner_opts)
                except RunnerError:
                    scratch_flow.write_text(render_flow_header(f"direct_{surface}"), encoding="utf-8")
                    rc.load(scratch_flow, {}, run_dir, **self.d.runner_opts)

                shell = self.d.shell_factory()
                ask_fn = (lambda q: request_approval(f"子智能体请求确认: {q}")) if not effective_unattended else None
                base_tools = common_tools(rc, run_dir, shell, ask_user=ask_fn)

                # 包装 observe 工具：同步广播实时截图，并执行被动 CAPTCHA 安全拦截
                wrapped_tools: list[Tool] = []
                for bt in base_tools:
                    if bt.name == "observe":
                        def observe_with_telemetry(args: dict[str, Any]) -> ToolResult:
                            obs, err = guard.call(lambda: rc.observe(detail=True, tag="observe"))
                            if err:
                                return ToolResult(f"observe failed: {err}")
                            shot = obs.get("screenshot")
                            if shot or obs.get("url") or obs.get("foreground"):
                                tr.emit(
                                    "worker_observation",
                                    {
                                        "screenshot": shot,
                                        "url": obs.get("url"),
                                        "title": obs.get("title"),
                                        "foreground": (obs.get("foreground") or {}).get("title")
                                        if isinstance(obs.get("foreground"), dict)
                                        else None,
                                    },
                                )
                            health = obs.get("page_health") or {}
                            if health.get("captcha_detected"):
                                msg = handle_captcha_event(obs)
                                return ToolResult(msg, images=shot_path(run_dir, shot))
                            lines: list[str] = []
                            if obs.get("url"):
                                lines.append(f"Browser URL: {obs['url']}\nTitle: {obs.get('title')}")
                            if health.get("alerts"):
                                lines.append(f"Visible alerts: {health['alerts']}")
                            if obs.get("foreground"):
                                lines.append(f"Foreground window: {json.dumps(obs['foreground'], ensure_ascii=False)}")
                            if obs.get("modal_dialogs"):
                                lines.append(f"Modal dialogs: {obs['modal_dialogs']}")
                            if obs.get("aria"):
                                lines.append(f"ARIA snapshot:\n{obs['aria']}")
                            if obs.get("ui_tree"):
                                lines.append(f"UIA tree (foreground window):\n{obs['ui_tree']}")
                            for k in ("screenshot_error", "browser_error", "desktop_error"):
                                if obs.get(k):
                                    lines.append(f"{k}: {obs[k]}")
                            return ToolResult("\n".join(lines) or "(no observation)", images=shot_path(run_dir, shot))

                        wrapped_tools.append(Tool(bt.spec, observe_with_telemetry))
                    else:
                        wrapped_tools.append(bt)

                def approval_tool(a: dict[str, Any]) -> ToolResult:
                    reason = (a.get("reason") or "请求人工审批放行").strip()
                    if effective_unattended:
                        return ToolResult("当前处于无人值守模式，无法挂起等待人工放行；若该操作安全可执行请继续，若存在不可逆高风险请调用 finish 汇报原因。")
                    ans = request_approval(reason)
                    return ToolResult(ans)

                def finish_tool(a: dict[str, Any]) -> ToolResult:
                    summary = (a.get("summary") or "").strip()
                    extra_out = a.get("output") if isinstance(a.get("output"), dict) else {}
                    if extra_out:
                        guard.call(lambda: rc.update_output(extra_out))
                    return ToolResult(
                        "Finished.",
                        done=True,
                        final={"summary": summary, "output": extra_out},
                    )

                agent_ref: list[ToolAgent] = []

                builtin_suite = build_builtin_tools(
                    rc,
                    run_dir,
                    on_captcha=handle_captcha_event,
                    on_observation=emit_live_observation,
                    on_memory_update=lambda k, v: agent_ref[0].pin_note(k, v) if agent_ref else None,
                )

                tools = (
                    builtin_suite
                    + wrapped_tools
                    + [
                        Tool(
                            tool_spec(
                                "request_approval",
                                "在执行高风险/不可逆操作前或按用户要求暂停，向主智能体与用户请求审批放行。",
                                {"reason": {"type": "string", "description": "需要用户确认或放行的具体事项说明"}},
                                ["reason"],
                            ),
                            approval_tool,
                        ),
                        Tool(
                            tool_spec(
                                "finish",
                                "当子任务目标已完成时调用，提交结果摘要与可选的结构化数据字典。",
                                {
                                    "summary": {"type": "string", "description": "简明中文执行结果汇报"},
                                    "output": {
                                        "type": "object",
                                        "description": "提取到的结构化键值对结果（可选）",
                                        "additionalProperties": True,
                                    },
                                },
                                ["summary"],
                            ),
                            finish_tool,
                        ),
                    ]
                )

                def on_watchdog_anomaly(_msgs: list[dict[str, Any]], anomaly: dict[str, Any]) -> str | None:
                    if not s.coordinator.supervisor_rescue:
                        return None
                    return supervisor.advise_stalled_loop(instruction, rc, run_dir, anomaly)

                agent = ToolAgent(
                    self.d.llms.get("operator"),
                    load_prompt("operator", surface=surface),
                    tools,
                    role=role_name,
                    on_log=lambda k, t: tr.log(role_name, k, t),
                    should_stop=lambda: cancel.cancelled,
                    drain_steering=getattr(cancel, "drain_steering", None),
                    supervisor_hook=on_watchdog_anomaly,
                )
                agent_ref.append(agent)

                intro = [f"# 子任务目标\n{instruction}"]
                if start_url:
                    intro.append(f"起始网址 (Start URL): {start_url}")
                if params:
                    intro.append(f"输入参数: {json.dumps(params, ensure_ascii=False)}")
                intro.append(f"当前本地时间: {time.strftime('%Y-%m-%d %H:%M:%S %A')}")

                base_limit = max(5, int(turn_limit or s.coordinator.operator_turn_limit or 120))
                max_cont = 0 if turn_limit is not None else max(0, int(s.coordinator.max_auto_continues or 0))

                res = agent.run(
                    "\n\n".join(intro),
                    turn_limit=base_limit,
                    max_continuations=max_cont,
                )

                if captcha_aborted:
                    return tr.status("failed", error=captcha_aborted[-1], usage=meter.snapshot())
                if cancel.cancelled:
                    return tr.status("aborted", error="aborted by user", usage=meter.snapshot())

                # 第 1 道防线（交互模式）：若跑满当前预算但尚未完成，优先允许用户在对话/面板点击放行继续追加轮数
                while (
                    not res.done
                    and res.reason == "turn_limit"
                    and not cancel.cancelled
                    and not captcha_aborted
                    and not effective_unattended
                ):
                    progress = agent.summarize_progress()
                    tr.log(role_name, "observation", f"[阶段步数已满，等待确认续跑或转交监督者收尾] {progress}")
                    try:
                        ans = request_approval(
                            f"子智能体已连续执行 {res.turns} 轮操作仍未全部收尾。\n"
                            f"阶段性进展：{progress[:260]}\n"
                            f"点击【批准放行】将自动压缩上下文并继续追加 {base_limit} 轮步数；点击【跳过此步】将交由监督者自动汇总并保存现有成果。"
                        )
                    except Cancelled:
                        break
                    if "跳过该动作" in ans:
                        break
                    res = agent.continue_after_limit(
                        base_limit,
                        instruction=f"{ans}\n请基于当前实时页面/桌面状态继续完成剩余未竟工作，完成后立即调用 finish。",
                    )

                if captcha_aborted:
                    return tr.status("failed", error=captcha_aborted[-1], usage=meter.snapshot())
                if cancel.cancelled:
                    return tr.status("aborted", error="aborted by user", usage=meter.snapshot())

                # 第 2 道防线（监督者兜底接管）：无论无人值守还是模型异常/空转/轮数耗尽，均由 Supervisor 接管现场完成兜底收尾
                if not res.done and s.coordinator.supervisor_rescue:
                    rescue = supervisor.rescue_direct_task(
                        instruction,
                        rc,
                        run_dir,
                        cancel,
                        prior_memory=agent.working_memory or agent.summarize_progress(),
                        pinned_notes=agent.pinned_notes,
                        fail_reason=res.reason,
                        on_log=lambda k, t: tr.log("supervisor", k, t),
                    )
                    if rescue.get("captcha"):
                        return tr.status("failed", error=rescue["summary"], usage=meter.snapshot())
                    if rescue.get("rescued"):
                        res = AgentResult(
                            True,
                            final={"summary": rescue["summary"], "output": rescue.get("output") or {}},
                            turns=res.turns,
                            messages=agent.messages,
                            working_memory=agent.working_memory,
                        )
                    else:
                        runner_out_fail: dict[str, Any] = dict(rescue.get("output") or {})
                        return tr.status(
                            "failed",
                            output={**runner_out_fail, "summary": rescue.get("summary") or ""},
                            error=rescue.get("summary") or f"子智能体与监督者兜底均未能突破阻塞（{res.reason}）",
                            usage=meter.snapshot(),
                        )

                runner_out: dict[str, Any] = {}
                try:
                    fin = rc.finish() or {}
                    if isinstance(fin.get("output"), dict):
                        runner_out = fin["output"]
                except RunnerError:
                    runner_out = {}

                if not res.done:
                    progress = agent.working_memory or agent.summarize_progress()
                    return tr.status(
                        "failed",
                        output={**runner_out, **agent.pinned_notes, "partial_summary": progress},
                        error=f"子智能体未能完成目标（{res.reason}）。阶段性进展：{progress[:300]}",
                        usage=meter.snapshot(),
                    )

                final_payload = res.final if isinstance(res.final, dict) else {}
                merged_output = {
                    **runner_out,
                    **agent.pinned_notes,
                    **(final_payload.get("output") or {}),
                    "summary": final_payload.get("summary") or "",
                }
                return tr.status("succeeded", output=merged_output, usage=meter.snapshot())
            finally:
                if owns_runner:
                    rc.close()
