"""监督智能体 `Supervisor`：逐步执行流程，完成每一步校验（L0–L3），并驱动四级自愈机制：

第 0 级：零 Token 确定性浏览器恢复（CDP 重连、清理泄漏标签页、关闭安全遮罩弹窗）。
第 1 级：单步修补器 ``Repairer``（原位热修补单个故障步骤并保存为新流程版本）。
第 2 级：自主兜底智能体 ``FallbackAgent`` 接管（遇到多步绕行场景时交互式恢复实时浏览器或桌面状态）。
第 3 级：安全中止（无人值守模式）或升级人工工单（交互模式 / 命中 CAPTCHA 安全边界）。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cuaflow.agents.fallback import FallbackAgent
from cuaflow.agents.judge import judge_task
from cuaflow.agents.repairer import Repairer
from cuaflow.agents.sentinel import verify_output_contract
from cuaflow.agents.tools import RunnerGuard, StepVerifier, check
from cuaflow.agents.tracking import CancelToken, RunTracker, TicketBroker
from cuaflow.config import Settings
from cuaflow.flow.client import RunnerClient, RunnerError
from cuaflow.flow.engine import latest_checkpoint_state, now_iso
from cuaflow.flow.patching import get_step_source, replace_step, unified_diff
from cuaflow.library.events import EventBus
from cuaflow.library.store import Store
from cuaflow.llm import LLMProvider, UsageMeter, usage_scope
from cuaflow.platform.base import ShellBackend


@dataclass
class Deps:
    store: Store
    bus: EventBus
    llms: LLMProvider
    settings: Callable[[], Settings]
    tickets: TicketBroker
    shell_factory: Callable[[], ShellBackend]
    runner_opts: dict[str, Any] = field(default_factory=dict)  # 例如 {"desktop": "fake", "headless": True}

    def new_runner(self, run_dir: Path) -> RunnerClient:
        rc = RunnerClient(log_path=run_dir / "runner.log")
        rc.start()
        return rc

    def with_runner_opts(self, **overrides: Any) -> "Deps":
        """派生携带独立 `runner_opts`（如独立浏览器会话路径）的依赖副本，供并行子智能体隔离使用。"""
        return Deps(
            store=self.store,
            bus=self.bus,
            llms=self.llms,
            settings=self.settings,
            tickets=self.tickets,
            shell_factory=self.shell_factory,
            runner_opts={**self.runner_opts, **overrides},
        )


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
            return bool(step.get("critical") or random.random() < s.supervisor.sample_rate)
        stats = self.d.store.step_stats(flow_id, step["id"])
        runs = int(stats.get("runs") or 0)
        audit_n = int(s.supervisor.audit_every_n_runs or 0)
        periodic_audit = audit_n > 0 and runs > 0 and (runs % audit_n == 0)
        return bool(step.get("critical") or stats.get("needs_verify", 1) or runs == 0 or periodic_audit)

    def execute(
        self,
        run_id: str,
        cancel: CancelToken | None = None,
        from_step: str | None = None,
        *,
        runner: RunnerClient | None = None,
        resume_checkpoint: bool = False,
        unattended: bool | None = None,
    ) -> dict[str, Any]:
        cancel = cancel or CancelToken()
        store, s = self.d.store, self.d.settings()
        effective_unattended = s.supervisor.unattended if unattended is None else bool(unattended)
        run = store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        flow_id = run["flow_id"]
        flow = store.get_flow(flow_id)
        if flow is None:
            raise KeyError(flow_id)
        version = run.get("version") or flow["current_version"]
        meter = UsageMeter(tokens_per_script_step=s.planner.tokens_saved_per_step, initial=run.get("usage"))
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

        owns_runner = runner is None
        with usage_scope(meter):
            try:
                if runner is None:
                    runner = self.d.new_runner(run_dir)
                init_state: dict[str, Any] | None = None
                passed_ckpt: list[str] = []
                if resume_checkpoint:
                    init_state, passed_ckpt = latest_checkpoint_state(run_dir)
                try:
                    meta = runner.load(work_file, params, run_dir, state=init_state, **self.d.runner_opts)
                except RunnerError as exc:
                    return tr.status("failed", error=f"cannot load flow: {exc}", usage=meter.snapshot())
                steps = meta["steps"]
                tr.init_steps(steps, keep_existing=True)
                start = 0
                if from_step:
                    start = [st["id"] for st in steps].index(from_step)
                    for st in steps[:start]:
                        tr.set_step_status(st["id"], "skipped")
                elif passed_ckpt:
                    # 跳过 steps.jsonl 中已连续通过的前置步骤
                    done_set = set(passed_ckpt)
                    while start < len(steps) and steps[start]["id"] in done_set:
                        tr.set_step_status(steps[start]["id"], "passed")
                        start += 1

                judge_llm = self.d.llms.get("vision_judge") if (
                    s.supervisor.l2_policy != "off" or s.supervisor.adaptive_l2
                ) else None
                verifier = StepVerifier(
                    judge_llm,
                    run_dir,
                    intent,
                    l15_enabled=s.supervisor.l15_sentinel,
                    adaptive_l2=s.supervisor.adaptive_l2,
                )
                guard = RunnerGuard(runner, run_dir)
                shell = self.d.shell_factory()
                patched: list[tuple[str, str]] = []
                working = source

                for step in steps[start:]:
                    sid = step["id"]
                    if cancel.cancelled:
                        return tr.status("aborted", error="aborted by user", usage=meter.snapshot())
                    tr.step_start(sid)
                    result, passed, checks = self._run_with_retries(runner, guard, verifier, tr, flow_id, step, s)
                    if passed:
                        from cuaflow.llm.base import record_active_script_step

                        record_active_script_step()
                        continue

                    # 安全边界：严禁尝试通过 AI 修复或绕过 CAPTCHA 人机验证。
                    if result.get("error_kind") == "captcha_blocked":
                        if effective_unattended:
                            tr.set_step_status(sid, "failed", error=result.get("error"))
                            return tr.status("failed", error=result.get("error") or "CAPTCHA blocked",
                                             usage=meter.snapshot())
                        tr.status("waiting_human")
                        ticket = self.d.tickets.open(
                            run_id,
                            f"步骤 {step.get('title')!r} 遇到人机验证/CAPTCHA，需要人工完成验证后继续",
                            step_id=sid,
                            flow_id=flow_id,
                            screenshot=result.get("screenshot"),
                        )
                        res = self.d.tickets.wait(ticket["id"], cancel)
                        action = res.get("action")
                        if action == "continue":
                            tr.rec(sid)["checks"].append(check("L0", True, "CAPTCHA resolved manually by operator"))
                            tr.set_step_status(sid, "passed")
                            tr.status("running")
                            continue
                        if action == "skip":
                            tr.set_step_status(sid, "skipped")
                            tr.status("running")
                            continue
                        tr.set_step_status(sid, "failed")
                        return tr.status("aborted", error="aborted at CAPTCHA gate", usage=meter.snapshot())

                    # ---------------- 第 1 级：单步修补器 `Repairer` ----------------
                    tr.set_step_status(sid, "repairing")
                    tr.status("repairing")
                    repairer = Repairer(self.d.llms.get("repairer"), judge_llm, runner, run_dir, shell,
                                        on_log=lambda k, t, _sid=sid: tr.log("repairer", k, t, step_id=_sid),
                                        should_stop=lambda: cancel.cancelled, turn_limit=s.supervisor.repair_turn_limit)
                    history: list[str] = []
                    hint: str | None = None
                    resolved = False
                    fallback_tried = False
                    while not resolved:
                        for _attempt in range(1, s.supervisor.max_repair_attempts + 1):
                            if cancel.cancelled:
                                return tr.status("aborted", error="aborted by user", usage=meter.snapshot())
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

                        # ---------------- 第 2 级：`FallbackAgent` 自主接管 ----------------
                        if (
                            s.supervisor.autonomous_fallback
                            and not fallback_tried
                            and not cancel.cancelled
                            and result.get("error_kind") != "captcha_blocked"
                        ):
                            fallback_tried = True
                            tr.log("fallback", "thought", f"启动 AI 自主接管恢复步骤 {sid}", step_id=sid)
                            fb_agent = FallbackAgent(
                                self.d.llms.get("fallback"),
                                judge_llm,
                                runner,
                                run_dir,
                                shell,
                                on_log=lambda k, t, _sid=sid: tr.log("fallback", k, t, step_id=_sid),
                                should_stop=lambda: cancel.cancelled,
                                drain_steering=getattr(cancel, "drain_steering", None),
                                turn_limit=s.supervisor.fallback_turn_limit,
                            )
                            fb = fb_agent.recover(working, step, result, checks, intent=intent, repair_history=history)
                            if fb.recovered:
                                rep = {
                                    "attempt": len(tr.rec(sid)["repairs"]) + 1,
                                    "started_at": now_iso(),
                                    "ended_at": now_iso(),
                                    "status": "succeeded",
                                    "summary": f"[AI自主接管] {fb.summary}",
                                }
                                if fb.step_source:
                                    old_step = get_step_source(working, sid)
                                    working = replace_step(working, sid, fb.step_source)
                                    work_file.write_text(working, encoding="utf-8")
                                    rep["diff"] = unified_diff(old_step, get_step_source(working, sid), f"{sid}.py")
                                    patched.append((sid, fb.summary))
                                tr.rec(sid)["repairs"].append(rep)
                                store.mark_needs_verify(flow_id, sid, True)
                                tr.step_result(sid, fb.result or {}, fb.checks, "repaired")
                                tr.emit("repair_end", rep, step_id=sid)
                                resolved = True
                                break
                            history.append(fb.summary)

                        # ---------------- 第 3 级：无人值守失败中止或升级人工工单 ----------------
                        if effective_unattended:
                            err_msg = (
                                f"步骤 {step.get('title')!r} 自动修复与自主接管均未成功: "
                                f"{history[-1] if history else result.get('error')}"
                            )
                            tr.set_step_status(sid, "failed", error=err_msg)
                            return tr.status("failed", error=err_msg, usage=meter.snapshot())

                        tr.status("waiting_human")
                        ticket = self.d.tickets.open(
                            run_id,
                            f"步骤 {step.get('title')!r} 自动修复失败: "
                            f"{history[-1] if history else result.get('error')}",
                            step_id=sid,
                            flow_id=flow_id,
                            screenshot=result.get("screenshot"),
                        )
                        res = self.d.tickets.wait(ticket["id"], cancel)
                        action = res.get("action")
                        if action == "abort":
                            tr.set_step_status(sid, "failed")
                            return tr.status("aborted", error="aborted by operator", usage=meter.snapshot())
                        if action == "skip":
                            tr.set_step_status(sid, "skipped")
                            resolved = True
                        elif action == "continue":
                            tr.rec(sid)["checks"].append(check("L0", True, "completed manually by operator"))
                            tr.set_step_status(sid, "passed")
                            store.mark_needs_verify(flow_id, sid)
                            resolved = True
                        else:  # 人工提供提示（hint） -> 进入下一轮自动修补尝试
                            hint = res.get("hint") or ""
                        tr.status("repairing" if not resolved else "running")
                    tr.status("running", usage=meter.snapshot())

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
                has_skipped = any(r["status"] == "skipped" for r in tr.records[start:])
                if not has_skipped:
                    contract_ok, contract_msg = verify_output_contract(output, flow.get("output_schema"))
                    if not contract_ok:
                        status = "failed"
                        fields["error"] = contract_msg
                        fields["final_verdict"] = {"passed": False, "reason": contract_msg, "detail": contract_msg}
                        tr.emit("check", check("L3", False, contract_msg))
                if status == "succeeded" and s.supervisor.l3_enabled:
                    verdict = judge_task(self.d.llms.get("supervisor"), intent, tr.records, output, run_dir)
                    if verdict["passed"] is not None:
                        verdict_with_detail = {**verdict, "detail": verdict["reason"]}
                        fields["final_verdict"] = verdict_with_detail
                        tr.emit("check", check("L3", bool(verdict["passed"]), verdict["reason"]))
                        if not verdict["passed"]:
                            status, fields["error"] = "failed", f"task-level verification failed: {verdict['reason']}"
                if has_skipped:
                    fields.setdefault("error", "some steps were skipped by the operator")
                fields["usage"] = meter.snapshot()
                return tr.status(status, **fields)
            except Exception as exc:  # noqa: BLE001
                tr.log("supervisor", "observation", f"internal error: {type(exc).__name__}: {exc}")
                return tr.status("failed", error=f"{type(exc).__name__}: {exc}", usage=meter.snapshot())
            finally:
                if owns_runner and runner is not None:
                    runner.close()
                self._after_run(run_id)

    # ------------------------------------------------------------------
    def _run_with_retries(self, runner, guard, verifier, tr, flow_id, step, s):
        sid = step["id"]
        retries = 0 if step.get("side_effect") else int(step.get("retries") or 0)
        timeout = float(step.get("timeout") or s.supervisor.step_timeout_default)
        result: dict[str, Any] = {}
        checks: list[dict[str, Any]] = []
        tier0_used = False
        attempt_idx = 0
        max_loops = retries + 1
        while attempt_idx < max_loops:
            result, err = guard.call(lambda: runner.run_step(sid, timeout=timeout))
            if err:
                result = {"step_id": sid, "status": "failed", "error_kind": "timeout", "error": err,
                          "ended_at": now_iso(), "duration_ms": int(timeout * 1000)}
            use_l2 = self._use_l2(flow_id, step, s)
            stats = self.d.store.step_stats(flow_id, sid)
            passed, checks = verifier.verify(step, result, use_l2, step_stats=stats)
            self.d.store.record_step(flow_id, sid, passed, int(result.get("duration_ms") or 0))
            if passed:
                if any(c["level"] == "L2" and c["passed"] and "skipped" not in c["detail"] for c in checks):
                    self.d.store.mark_needs_verify(flow_id, sid, False)
                tr.step_result(sid, result, checks, "passed")
                return result, True, checks

            attempt_idx += 1
            # 第 0 级：零 Token 确定性浏览器自愈（关闭安全遮罩弹窗 / 清理泄漏标签页 / 重连 CDP）
            if (
                attempt_idx >= max_loops
                and not tier0_used
                and s.supervisor.tier0_heal
                and not step.get("side_effect")
                and result.get("error_kind") != "captcha_blocked"
                and "browser" in (result.get("surfaces") or [])
            ):
                tier0_used = True
                err_text = str(result.get("error") or "")
                transient_nav = any(
                    tok in err_text
                    for tok in (
                        "ERR_ABORTED",
                        "Execution context was destroyed",
                        "Target page, context or browser has been closed",
                    )
                )
                heal, herr = guard.call(lambda: runner.heal_browser(keep_tabs=s.planner.tab_limit))
                if not herr and isinstance(heal, dict) and (
                    transient_nav or heal.get("reconnected") or heal.get("tabs_closed") or heal.get("dismissed")
                ):
                    tr.log("supervisor", "observation", f"Tier-0 零Token浏览器自愈尝试: {heal}", step_id=sid)
                    max_loops += 1

            last = attempt_idx >= max_loops
            tr.step_result(sid, result, checks, "failed" if last else "running")
        return result, False, checks

    def _after_run(self, run_id: str) -> None:
        """自动回滚：若当前版本连续失败且父版本历史健康度良好，则回退至父版本。"""
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

    # ------------------------------------------------------------------ 直连智能体循环的实时巡检与兜底接管
    def advise_stalled_loop(
        self,
        instruction: str,
        runner: RunnerClient,
        run_dir: Path,
        anomaly: dict[str, Any],
    ) -> str:
        """当 `ToolAgent` 看门狗检测到连续报错或重复死循环时，先执行零 Token 浏览器自愈再给出监督者纠偏指导。"""
        s = self.d.settings()
        guard = RunnerGuard(runner, run_dir)
        heal_info: dict[str, Any] = {}
        if s.supervisor.tier0_heal:
            hres, herr = guard.call(lambda: runner.heal_browser(keep_tabs=s.planner.tab_limit))
            if not herr and isinstance(hres, dict):
                heal_info = hres

        obs, _ = guard.call(lambda: runner.observe(detail=False, tag="sup_watchdog"))
        obs = obs if isinstance(obs, dict) else {}
        health = obs.get("page_health") if isinstance(obs.get("page_health"), dict) else {}

        if health.get("captcha_detected"):
            return "页面检测到人机验证（CAPTCHA），严禁继续自动点击或绕过；请立即调用 request_approval 交由人工处理或安全收尾。"

        fallback_advice = (
            f"检测到异常模式（{anomaly.get('kind')}）：最近调用 `{anomaly.get('last_call', '')[:160]}` 未能取得进展。"
            f"{f' 已自动执行浏览器清理自愈：{heal_info}。' if heal_info else ''}"
            "请勿重复完全相同的参数或选择器：建议先调用 `browser_extract(mode='elements')` 或 `observe` 查看最新可用控件，"
            "优先改用 `browser_interact` / `browser_extract` / `web_fetch` 等内置工具换一种路径推进。"
        )

        try:
            from cuaflow.llm.base import image_part, text_part

            sup_llm = self.d.llms.get("supervisor")
            diag_prompt = (
                "你是 **Supervisor（运行监督者）**。直连执行子智能体在执行任务过程中出现卡滞或连续报错，请你给出简明扼要的纠偏方案（2~4 条具体可执行建议，不要调用工具）：\n"
                f"- 任务目标：{instruction}\n"
                f"- 异常类型：{anomaly.get('kind')}\n"
                f"- 最近卡住的调用：{anomaly.get('last_call')}\n"
                f"- 最近一次报错/观测输出：{str(anomaly.get('last_observation') or '')[:800]}\n"
                f"- 当前浏览器状态：URL={obs.get('url')} | Title={obs.get('title')} | 告警={health.get('alerts')}\n"
                f"- 已自动执行的零Token环境自愈结果：{heal_info}\n"
                "请直接输出给执行智能体的中文纠偏指令（指出失败原因以及下一步应改用哪个内置工具或定位策略）。"
            )
            parts: list[dict[str, Any]] = [text_part(diag_prompt)]
            shot_rel = obs.get("screenshot")
            if shot_rel and getattr(sup_llm, "vision", False):
                sp = Path(shot_rel) if Path(shot_rel).is_absolute() else (run_dir / shot_rel)
                if sp.is_file():
                    parts.append(image_part(sp))
            resp = sup_llm.chat([{"role": "user", "content": parts}], max_tokens=500)
            txt = (resp.content or "").strip()
            if txt:
                return txt
        except Exception:  # noqa: BLE001
            pass
        return fallback_advice

    def rescue_direct_task(
        self,
        instruction: str,
        runner: RunnerClient,
        run_dir: Path,
        cancel: CancelToken,
        *,
        prior_memory: str = "",
        pinned_notes: dict[str, Any] | None = None,
        fail_reason: str = "",
        on_log: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        """当主执行子智能体未能自行完成（如触及轮数上限、模型空转或工具异常）时，由监督者接管现场完成兜底收尾。"""
        import json

        from cuaflow.agents.builtin_tools import build_builtin_tools
        from cuaflow.agents.loop import Tool, ToolAgent, ToolResult
        from cuaflow.agents.tools import common_tools, shot_path
        from cuaflow.llm.base import image_part, text_part, tool_spec

        log = on_log or (lambda k, t: None)
        s = self.d.settings()
        guard = RunnerGuard(runner, run_dir)
        log("observation", f"[监督者兜底接管] 原执行循环未收尾（原因: {fail_reason}），Supervisor 正在检查现场并接管完成剩余步骤…")

        if s.supervisor.tier0_heal:
            guard.call(lambda: runner.heal_browser(keep_tabs=s.planner.tab_limit))

        obs, _ = guard.call(lambda: runner.observe(detail=True, tag="sup_rescue"))
        obs = obs if isinstance(obs, dict) else {}
        health = obs.get("page_health") if isinstance(obs.get("page_health"), dict) else {}
        if health.get("captcha_detected"):
            return {
                "rescued": False,
                "captcha": True,
                "summary": "页面存在人机验证（CAPTCHA），监督者已按安全规范停止自动操作。",
                "output": dict(pinned_notes or {}),
            }

        fin_before, _ = guard.call(lambda: runner.finish())
        existing_out = dict((fin_before or {}).get("output") or {})
        if pinned_notes:
            existing_out.update(pinned_notes)

        def complete_rescue(args: dict[str, Any]) -> ToolResult:
            accomplished = bool(args.get("accomplished", True))
            summary = str(args.get("summary") or "").strip()
            extra = args.get("output") if isinstance(args.get("output"), dict) else {}
            if extra:
                guard.call(lambda: runner.update_output(extra))
            return ToolResult(
                "Supervisor rescue completed.",
                done=True,
                final={
                    "rescued": accomplished,
                    "summary": summary,
                    "output": {**existing_out, **extra},
                },
            )

        shell = self.d.shell_factory()
        rescue_tools = (
            build_builtin_tools(runner, run_dir)
            + common_tools(runner, run_dir, shell)
            + [
                Tool(
                    tool_spec(
                        "complete_rescue",
                        "监督者兜底收尾工具：当确认目标已达成、已补齐最后缺失动作，或确认存在不可抗外部阻塞时调用。",
                        {
                            "accomplished": {
                                "type": "boolean",
                                "description": "任务核心目标（或有价值的阶段性提取）是否已达成",
                            },
                            "summary": {"type": "string", "description": "给用户的最终中文结论与进展汇报"},
                            "output": {"type": "object", "additionalProperties": True},
                        },
                        ["accomplished", "summary"],
                    ),
                    complete_rescue,
                )
            ]
        )

        rescue_sys = (
            "你是 **Supervisor（监督者兜底智能体）**。前序执行智能体因步数耗尽或局部卡顿未能调用 finish 收尾，现在由你接管实时浏览器与桌面现场。\n"
            "工作原则：\n"
            "1. 先核对当前页面/桌面状态、前序累计记忆与已提取数据：如果用户目标其实已经完成或数据已拿齐，直接调用 `complete_rescue(accomplished=True, summary=..., output=...)` 汇总交付！\n"
            "2. 如果还差最后几步操作即可完成，优先使用 `browser_interact`、`browser_extract`、`web_fetch`、`file_ops` 快速补齐，然后调用 `complete_rescue(accomplished=True, ...)`。\n"
            "3. 若确实遇到需要人工登录凭证或目标站点故障等外部阻塞，保留全部已获取数据并调用 `complete_rescue(accomplished=False, summary=...)` 清晰说明原因。"
        )

        rescue_agent = ToolAgent(
            self.d.llms.get("fallback"),
            rescue_sys,
            rescue_tools,
            role="supervisor",
            on_log=log,
            should_stop=lambda: cancel.cancelled,
            drain_steering=getattr(cancel, "drain_steering", None),
            allow_text_finish=True,
        )

        ctx_lines = [
            f"# 原始任务目标\n{instruction}",
            f"# 前序未完成原因\n{fail_reason}",
            f"# 前序累计工作记忆\n{prior_memory or '(无)'}",
            f"# 已保存的结构化输出数据\n{json.dumps(existing_out, ensure_ascii=False, indent=2)}",
            f"# 当前实时页面观测\nURL: {obs.get('url')}\nTitle: {obs.get('title')}\nARIA 摘要:\n{str(obs.get('aria') or '')[:4000]}",
        ]
        user_parts: list[dict[str, Any]] = [text_part("\n\n".join(ctx_lines))]
        for sp in shot_path(run_dir, obs.get("screenshot")):
            user_parts.append(image_part(sp))

        rres = rescue_agent.run(user_parts, turn_limit=max(12, int(s.supervisor.fallback_turn_limit or 25)))
        fin_after, _ = guard.call(lambda: runner.finish())
        merged_out = {**existing_out, **((fin_after or {}).get("output") or {})}

        if rres.done and isinstance(rres.final, dict):
            # 若 allow_text_finish 直接返回了文本结论，且已有提取产出或明确答复，也视为兜底完成
            rescued_flag = rres.final.get("rescued", True)
            summary_txt = str(rres.final.get("summary") or rres.final.get("reply") or "").strip()
            merged_out.update(rres.final.get("output") or {})
            return {
                "rescued": bool(rescued_flag),
                "summary": summary_txt or prior_memory[:500] or "监督者已完成现场兜底收尾。",
                "output": merged_out,
            }

        return {
            "rescued": bool(merged_out),
            "summary": prior_memory[:600] or f"监督者已保存现场阶段性结果（原停止原因：{fail_reason}）。",
            "output": merged_out,
        }
