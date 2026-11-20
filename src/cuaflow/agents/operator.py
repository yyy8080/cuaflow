"""直连执行子智能体 `DirectOperator`：直接操控浏览器、桌面或命令行完成即席任务，无需录制脚本开销。"""

from __future__ import annotations

import json
import time
from typing import Any

from cuaflow.agents.loop import AgentResult, Cancelled, Tool, ToolAgent, ToolResult
from cuaflow.agents.supervisor import Deps
from cuaflow.agents.tools import RunnerGuard, common_tools, load_prompt, shot_path
from cuaflow.agents.tracking import CancelToken, RunTracker
from cuaflow.flow.client import RunnerClient, RunnerError
from cuaflow.flow.patching import render_flow_header
from cuaflow.llm import UsageMeter, usage_scope
from cuaflow.llm.base import tool_spec


class DirectOperator:
    """面向即席探索、并行网页调研与桌面单次操作的直连执行子智能体。"""

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
        """在独立运行目录与运行器进程中直接执行子任务，支持多阶段自动压缩续跑、中途转向与超限放行续跑。"""
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

                # 包装 observe 工具：广播实时截图供并行面板展示，并强制执行 CAPTCHA 被动安全拦截
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
                                msg = "检测到人机验证（CAPTCHA），触发安全边界保护，严禁自动绕过。"
                                if effective_unattended:
                                    captcha_aborted.append(msg)
                                    cancel.cancel()
                                    raise Cancelled()
                                note = request_approval(
                                    "页面出现人机验证（CAPTCHA），子智能体已安全暂停，请在浏览器中人工完成后点击【放行】。",
                                    screenshot_rel=shot,
                                )
                                return ToolResult(
                                    f"CAPTCHA 安全暂停已由人工处理：{note}。请重新调用 observe 查看最新页面。",
                                    images=shot_path(run_dir, shot),
                                )
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
                        return ToolResult("当前处于无人值守模式，无法请求人工放行；若该操作有高风险请安全终止并调用 finish 说明原因。")
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

                tools = wrapped_tools + [
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

                agent = ToolAgent(
                    self.d.llms.get("operator"),
                    load_prompt("operator", surface=surface),
                    tools,
                    role=role_name,
                    on_log=lambda k, t: tr.log(role_name, k, t),
                    should_stop=lambda: cancel.cancelled,
                    drain_steering=getattr(cancel, "drain_steering", None),
                )

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

                # 交互模式下，若多阶段自动续跑后仍触及轮数上限，绝不直接报错丢弃实时页面，而是挂起询问用户继续放行或汇总现有成果
                while (
                    not res.done
                    and res.reason == "turn_limit"
                    and not cancel.cancelled
                    and not captcha_aborted
                    and not effective_unattended
                ):
                    progress = agent.summarize_progress()
                    tr.log(role_name, "observation", f"[达到阶段步数上限，等待确认续跑] {progress}")
                    try:
                        ans = request_approval(
                            f"子智能体已连续执行 {res.turns} 轮操作仍未全部收尾。\n"
                            f"阶段性进展：{progress[:260]}\n"
                            f"点击【批准放行】将压缩上下文并继续追加 {base_limit} 轮步数；点击【跳过此步】将直接保存并汇报目前已取得的成果。"
                        )
                    except Cancelled:
                        break
                    if "跳过该动作" in ans:
                        res = AgentResult(
                            True,
                            final={"summary": f"【阶段性成果汇总（共执行 {res.turns} 轮）】\n{progress}", "output": {}},
                            turns=res.turns,
                            messages=agent.messages,
                        )
                        break
                    res = agent.continue_after_limit(
                        base_limit,
                        instruction=f"{ans}\n请基于当前实时页面/桌面状态继续完成剩余未竟工作，完成后立即调用 finish。",
                    )

                if captcha_aborted:
                    return tr.status("failed", error=captcha_aborted[-1], usage=meter.snapshot())
                if cancel.cancelled:
                    return tr.status("aborted", error="aborted by user", usage=meter.snapshot())

                runner_out: dict[str, Any] = {}
                try:
                    fin = rc.finish() or {}
                    if isinstance(fin.get("output"), dict):
                        runner_out = fin["output"]
                except RunnerError:
                    runner_out = {}

                if not res.done:
                    if res.reason == "turn_limit":
                        progress = agent.summarize_progress()
                        return tr.status(
                            "failed",
                            output={**runner_out, "partial_summary": progress},
                            error=f"已达到自动执行轮数上限（累计 {res.turns} 轮）。阶段性进展：{progress[:300]}",
                            usage=meter.snapshot(),
                        )
                    return tr.status(
                        "failed",
                        output=runner_out or None,
                        error=f"直连子智能体未能完成：{res.reason}",
                        usage=meter.snapshot(),
                    )

                final_payload = res.final if isinstance(res.final, dict) else {}
                merged_output = {
                    **runner_out,
                    **(final_payload.get("output") or {}),
                    "summary": final_payload.get("summary") or "",
                }
                return tr.status("succeeded", output=merged_output, usage=meter.snapshot())
            finally:
                if owns_runner:
                    rc.close()
