"""主控对话智能体 `CoordinatorAgent` 与多智能体并发执行池 `SubagentPool`。

核心能力：
1. `BrowserSlotPool`：为并发浏览器子智能体分配相互隔离的浏览器会话槽位，杜绝多智能体争抢同一页面。
2. `SubagentPool`：基于线程池并行运行 `explorer`、`desktop`、`supervisor`、`author`、`planner` 五类子智能体，
   支持实时进度镜像、中途动态注入调整指令（Steering）以及人工/主控放行（Approval）。
3. `CoordinatorAgent`：面向用户的对话式主智能体，负责拆解并派发并行子任务、处理中途转向与放行，并在子智能体完成后主动汇总汇报。
"""

from __future__ import annotations

import json
import threading
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cuaflow.agents.author import Author
from cuaflow.agents.loop import Tool, ToolAgent, ToolResult
from cuaflow.agents.operator import DirectOperator
from cuaflow.agents.planner import MissionPlanner
from cuaflow.agents.supervisor import Supervisor
from cuaflow.agents.tools import load_prompt
from cuaflow.agents.tracking import WorkerControl
from cuaflow.library.store import future_iso, now_iso
from cuaflow.llm import UsageMeter, usage_scope
from cuaflow.llm.base import tool_spec

if TYPE_CHECKING:
    from cuaflow.services import Services

VALID_AGENT_KINDS = ("explorer", "desktop", "supervisor", "author", "planner")


class BrowserSlotPool:
    """管理并发子智能体的独立浏览器会话槽位池。

    槽位 0 默认复用主浏览器配置（保留用户已登录态或测试注入的 `browser_session`）；
    槽位 1..N 自动分配 `<root>/browser_slots/slot_<k>/session.json` 独立实例，实现零冲突真并行。
    """

    def __init__(self, root: Path):
        self.root = Path(root) / "browser_slots"
        self._lock = threading.Lock()
        self._in_use: set[int] = set()
        self._extra_sessions: set[Path] = set()

    def acquire(self) -> tuple[int, dict[str, Any]]:
        """申请一个空闲浏览器槽位，返回 `(slot_id, runner_opts_overrides)`。"""
        with self._lock:
            slot = 0
            while slot in self._in_use:
                slot += 1
            self._in_use.add(slot)
            if slot == 0:
                return 0, {}
            sf = (self.root / f"slot_{slot}" / "session.json").resolve()
            sf.parent.mkdir(parents=True, exist_ok=True)
            self._extra_sessions.add(sf)
            return slot, {"browser_session": str(sf)}

    def release(self, slot: int) -> None:
        """归还浏览器槽位供后续子智能体复用。"""
        with self._lock:
            self._in_use.discard(slot)

    def close(self) -> None:
        """关闭所有额外创建的并发浏览器槽位进程。"""
        with self._lock:
            sessions = list(self._extra_sessions)
            self._extra_sessions.clear()
            self._in_use.clear()
        for sf in sessions:
            if sf.exists():
                try:
                    from cuaflow.platform.browser import PersistentBrowser

                    PersistentBrowser(sf).shutdown()
                except Exception:  # noqa: BLE001
                    pass


class SubagentPool:
    """多子智能体并行调度与全生命周期管控池。"""

    def __init__(self, svc: "Services", start_workers: bool = True):
        self.svc = svc
        max_w = max(2, int(svc.get_settings().coordinator.max_parallel_workers or 4))
        self._executor: ThreadPoolExecutor | None = (
            ThreadPoolExecutor(max_workers=max_w, thread_name_prefix="cuaflow-subagent") if start_workers else None
        )
        self.browser_slots = BrowserSlotPool(svc.root)
        self._desktop_lock = threading.Lock()
        self._lock = threading.Lock()
        self._controls: dict[str, WorkerControl] = {}
        self._futures: dict[str, Future] = {}
        self._run_to_worker: dict[str, str] = {}
        self._task_to_worker: dict[str, str] = {}
        self._unsubscribe = svc.bus.subscribe(self._on_bus_event)

    def close(self) -> None:
        """取消所有在途子智能体并释放线程池与额外浏览器进程。"""
        self._unsubscribe()
        with self._lock:
            ctrls = list(self._controls.values())
        for c in ctrls:
            c.cancel()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self.browser_slots.close()

    # ------------------------------------------------------------------ 事件镜像同步
    def _publish_worker(self, worker: dict[str, Any]) -> None:
        self.svc.bus.publish(
            "worker_updated",
            {"session_id": worker.get("session_id"), "worker_id": worker["id"], "worker": worker},
            run_id=worker.get("run_id"),
            flow_id=worker.get("flow_id"),
        )

    def _on_bus_event(self, ev: dict[str, Any]) -> None:
        etype = ev.get("type")
        if etype in ("worker_updated", "chat_message", "chat_status"):
            return
        run_id = ev.get("run_id")
        task_id = ev.get("task_id")
        with self._lock:
            wid = (self._run_to_worker.get(run_id) if run_id else None) or (
                self._task_to_worker.get(task_id) if task_id else None
            )
        if not wid:
            return
        w = self.svc.store.get_worker(wid)
        if not w or w["status"] in ("succeeded", "failed", "aborted"):
            return

        data = ev.get("data") or {}
        updates: dict[str, Any] = {}
        if etype == "step_start":
            updates["latest_note"] = f"正在执行步骤：{data.get('title') or ev.get('step_id') or ''}"
        elif etype == "step_end":
            if data.get("screenshot"):
                updates["latest_screenshot"] = data["screenshot"]
            st = data.get("status")
            title = data.get("title") or ev.get("step_id") or ""
            updates["latest_note"] = f"步骤 {title}: {'通过' if st == 'passed' else st}"
        elif etype == "worker_observation":
            if data.get("screenshot"):
                updates["latest_screenshot"] = data["screenshot"]
            target = data.get("title") or data.get("url") or data.get("foreground") or "最新画面"
            updates["latest_note"] = f"实时观测：{str(target)[:100]}"
        elif etype == "batch_progress":
            updates["latest_note"] = f"批量进度：{data.get('completed', 0)}/{data.get('total', 0)}"
        elif etype == "agent_log":
            kind = data.get("kind")
            text = (data.get("text") or "").strip()
            if kind == "thought" and text:
                first_line = text.splitlines()[0][:120]
                updates["latest_note"] = first_line
            elif kind == "command" and text and not text.startswith("finish("):
                updates["latest_note"] = f"执行动作：{text[:100]}"
        elif etype == "ticket_open":
            updates["status"] = "waiting_approval"
            updates["latest_note"] = f"⏸ 等待放行：{str(data.get('reason') or '')[:120]}"
            if data.get("screenshot"):
                updates["latest_screenshot"] = data["screenshot"]
            updated = self.svc.store.update_worker(wid, **updates)
            self._publish_worker(updated)
            if hasattr(self.svc, "coordinator"):
                self.svc.coordinator.on_worker_approval_needed(updated, data)
            return
        elif etype == "ticket_resolved":
            res_action = (data.get("resolution") or {}).get("action")
            if w["status"] == "waiting_approval" and res_action != "abort":
                updates["status"] = "running"
                updates["latest_note"] = "▶ 已放行，正在继续执行…"

        if updates:
            updated = self.svc.store.update_worker(wid, **updates)
            self._publish_worker(updated)

    # ------------------------------------------------------------------ 派发与运行
    def dispatch(self, session_id: str | None, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """并行派发一组子智能体任务并立即返回创建的 Worker 列表。"""
        created: list[dict[str, Any]] = []
        for idx, raw in enumerate(specs, 1):
            kind = str(raw.get("agent_kind") or "explorer").strip().lower()
            if kind not in VALID_AGENT_KINDS:
                kind = "explorer"
            instruction = str(raw.get("instruction") or "").strip()
            if not instruction and kind == "supervisor" and raw.get("flow_id"):
                instruction = f"运行已保存脚本 {raw['flow_id']}"
            if not instruction:
                continue
            default_name = {
                "explorer": f"浏览器探索 #{idx}",
                "desktop": f"桌面执行 #{idx}",
                "supervisor": f"脚本监督 #{idx}",
                "author": f"脚本录制 #{idx}",
                "planner": f"批量规划 #{idx}",
            }[kind]
            name = str(raw.get("name") or default_name).strip() or default_name
            ctrl = WorkerControl()
            worker = self.svc.store.create_worker(
                session_id=session_id,
                name=name,
                agent_kind=kind,
                instruction=instruction,
                start_url=raw.get("start_url") or None,
                params=raw.get("params") if isinstance(raw.get("params"), dict) else {},
                flow_id=raw.get("flow_id") or None,
            )
            with self._lock:
                self._controls[worker["id"]] = ctrl
            self._publish_worker(worker)
            if self._executor is not None:
                fut = self._executor.submit(self._execute_worker, worker["id"], ctrl)
                with self._lock:
                    self._futures[worker["id"]] = fut
            created.append(worker)
        return created

    def _execute_worker(self, worker_id: str, ctrl: WorkerControl) -> dict[str, Any]:
        """在工作线程内执行单个子智能体任务。"""
        store = self.svc.store
        w = store.get_worker(worker_id)
        if w is None:
            return {}
        if ctrl.cancelled:
            aborted = store.update_worker(
                worker_id, status="aborted", error="启动前已取消", latest_note="已取消", finished_at=now_iso()
            )
            self._publish_worker(aborted)
            return aborted

        kind = w["agent_kind"]
        params = dict(w.get("params") or {})
        slot_id: int | None = None
        worker_deps = self.svc.deps

        try:
            if kind in ("explorer", "supervisor", "author", "planner"):
                slot_id, slot_opts = self.browser_slots.acquire()
                if slot_opts:
                    worker_deps = self.svc.deps.with_runner_opts(**slot_opts)

            running_note = "等待桌面控制权…" if kind == "desktop" else "正在初始化执行环境…"
            w = store.update_worker(worker_id, status="running", started_at=now_iso(), latest_note=running_note)
            self._publish_worker(w)

            if kind == "explorer":
                run = store.create_run("direct", params, steps=[])
                with self._lock:
                    self._run_to_worker[run["id"]] = worker_id
                w = store.update_worker(worker_id, run_id=run["id"], latest_note="正在操控浏览器执行任务…")
                self._publish_worker(w)
                res = DirectOperator(worker_deps).execute(
                    run["id"],
                    w["instruction"],
                    ctrl,
                    surface="browser",
                    start_url=w.get("start_url"),
                    params=params,
                )
                final_worker = self._finalize_from_run(worker_id, res)

            elif kind == "desktop":
                run = store.create_run("direct", params, steps=[])
                with self._lock:
                    self._run_to_worker[run["id"]] = worker_id
                w = store.update_worker(worker_id, run_id=run["id"])
                self._publish_worker(w)
                with self._desktop_lock:
                    w = store.update_worker(worker_id, latest_note="正在操控本地桌面与命令行…")
                    self._publish_worker(w)
                    res = DirectOperator(worker_deps).execute(
                        run["id"],
                        w["instruction"],
                        ctrl,
                        surface="desktop",
                        start_url=w.get("start_url"),
                        params=params,
                    )
                final_worker = self._finalize_from_run(worker_id, res)

            elif kind == "supervisor":
                flow_id = w.get("flow_id")
                if not flow_id or store.get_flow(flow_id) is None:
                    raise ValueError(f"未找到要执行的流程脚本: {flow_id!r}")
                run = self.svc.orchestrator.submit_run(flow_id, params, enqueue=False)
                self.svc.orchestrator.tokens[run["id"]] = ctrl
                with self._lock:
                    self._run_to_worker[run["id"]] = worker_id
                w = store.update_worker(worker_id, run_id=run["id"], latest_note=f"监督运行脚本 {flow_id}…")
                self._publish_worker(w)
                res = Supervisor(worker_deps).execute(run["id"], ctrl)
                final_worker = self._finalize_from_run(worker_id, res)

            elif kind == "author":
                run = store.create_run("author", params)
                with self._lock:
                    self._run_to_worker[run["id"]] = worker_id
                w = store.update_worker(worker_id, run_id=run["id"], latest_note="正在边执行边录制可复用脚本…")
                self._publish_worker(w)
                res = Author(worker_deps).record(
                    run["id"],
                    {"instruction": w["instruction"], "start_url": w.get("start_url"), "params": params},
                    ctrl,
                )
                final_worker = self._finalize_from_run(worker_id, res)

            else:  # planner 长程批量规划
                task = store.create_task(
                    w["instruction"], w.get("start_url"), params, False, mode="mission"
                )
                mrun = store.create_run("mission", params, task_id=task["id"])
                store.append_task_run(task["id"], mrun["id"])
                with self._lock:
                    self._task_to_worker[task["id"]] = worker_id
                    self._run_to_worker[mrun["id"]] = worker_id
                w = store.update_worker(worker_id, run_id=mrun["id"], latest_note="正在执行长程拆解与批量任务…")
                self._publish_worker(w)
                sup = Supervisor(worker_deps)
                aut = Author(worker_deps)
                tres = MissionPlanner(worker_deps, sup, aut).execute(mrun["id"], task, ctrl)
                status = tres.get("status") if tres.get("status") in ("succeeded", "failed", "aborted") else "failed"
                summary = tres.get("final_response") or tres.get("error") or ""
                store.update_task(task["id"], status=status, run_id=mrun["id"],
                                  flow_id=tres.get("flow_id"), final_response=summary)
                final_worker = store.update_worker(
                    worker_id,
                    run_id=mrun["id"],
                    flow_id=tres.get("flow_id") or w.get("flow_id"),
                    status=status,
                    output={"summary": summary, "task_id": task["id"]},
                    error=None if status == "succeeded" else (tres.get("error") or summary or "长程任务未完成"),
                    usage=tres.get("usage"),
                    latest_note=(summary[:140] if summary else ("已完成" if status == "succeeded" else "执行失败")),
                    finished_at=now_iso(),
                )
        except Exception as exc:  # noqa: BLE001
            tb_tail = traceback.format_exc()[-1200:]
            final_worker = store.update_worker(
                worker_id,
                status="aborted" if ctrl.cancelled else "failed",
                error=f"{type(exc).__name__}: {exc}",
                latest_note=f"执行异常：{exc}",
                output={"traceback": tb_tail},
                finished_at=now_iso(),
            )
        finally:
            if slot_id is not None:
                self.browser_slots.release(slot_id)

        self._publish_worker(final_worker)
        if hasattr(self.svc, "coordinator"):
            self.svc.coordinator.on_worker_finished(final_worker)
        return final_worker

    def _finalize_from_run(self, worker_id: str, run: dict[str, Any]) -> dict[str, Any]:
        status = run.get("status") or "failed"
        if status not in ("succeeded", "failed", "aborted"):
            status = "failed"
        out = run.get("output") if isinstance(run.get("output"), dict) else {}
        summary = (
            out.get("summary")
            or out.get("final_response")
            or (json.dumps(out, ensure_ascii=False)[:160] if out else "")
        )
        err = run.get("error")
        note = summary[:140] if status == "succeeded" and summary else (
            "执行成功" if status == "succeeded" else (err or "已终止")[:140]
        )
        # 提取最后一张步骤截图（若有）
        shot = None
        for s in reversed(run.get("steps") or []):
            if isinstance(s, dict) and s.get("screenshot"):
                shot = s["screenshot"]
                break
        fields: dict[str, Any] = {
            "status": status,
            "flow_id": run.get("flow_id") or (self.svc.store.get_worker(worker_id) or {}).get("flow_id"),
            "output": out,
            "error": err,
            "usage": run.get("usage"),
            "latest_note": note,
            "finished_at": now_iso(),
        }
        if shot:
            fields["latest_screenshot"] = shot
        return self.svc.store.update_worker(worker_id, **fields)

    # ------------------------------------------------------------------ 中途干预：转向 / 放行 / 终止
    def resolve_target_workers(
        self, target: str, session_id: str | None = None, *, states: tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        """支持按精确 `worker_id`、`"all"` 或子智能体名称模糊匹配目标 Worker。"""
        target = (target or "").strip()
        if not target:
            return []
        exact = self.svc.store.get_worker(target)
        if exact is not None:
            return [exact]
        candidates = self.svc.store.list_workers(session_id=session_id, limit=100)
        if states:
            candidates = [w for w in candidates if w.get("status") in states]
        if target.lower() in ("all", "全部", "*"):
            return candidates
        matched = [w for w in candidates if target.lower() in (w.get("name") or "").lower() or target in w["id"]]
        return matched

    def steer(self, worker_id: str, instruction: str, *, source: str = "user") -> dict[str, Any]:
        """向正在运行或排队中的子智能体实时注入中途调整指令。"""
        w = self.svc.store.get_worker(worker_id)
        if w is None:
            raise KeyError(worker_id)
        with self._lock:
            ctrl = self._controls.get(worker_id)
        if ctrl is not None:
            ctrl.push_steering(instruction)
        updated = self.svc.store.append_worker_steering(worker_id, instruction, source=source)
        # 若该子智能体正处于等待放行状态，中途调整同时自动携带指导意见放行该工单
        if updated.get("status") == "waiting_approval":
            self.approve(worker_id, action="hint", hint=instruction)
            updated = self.svc.store.get_worker(worker_id) or updated
        self._publish_worker(updated)
        return updated

    def approve(self, worker_id: str, action: str = "continue", hint: str | None = None) -> dict[str, Any]:
        """放行或处理处于等待审批状态的子智能体关联工单。"""
        w = self.svc.store.get_worker(worker_id)
        if w is None:
            raise KeyError(worker_id)
        run_id = w.get("run_id")
        resolved_any = False
        if run_id:
            for t in self.svc.store.list_tickets("open"):
                if t.get("run_id") == run_id:
                    self.svc.tickets.resolve(t["id"], action, hint)
                    resolved_any = True
        if action == "abort":
            return self.abort(worker_id)
        if not resolved_any and hint and action in ("continue", "hint"):
            with self._lock:
                ctrl = self._controls.get(worker_id)
            if ctrl is not None:
                ctrl.push_steering(hint)
        if resolved_any:
            updated = self.svc.store.update_worker(
                worker_id, status="running", latest_note="▶ 已放行，继续执行…"
            )
            self._publish_worker(updated)
            return updated
        return w

    def abort(self, worker_id: str) -> dict[str, Any]:
        """立即中止指定子智能体并关闭其待处理工单。"""
        w = self.svc.store.get_worker(worker_id)
        if w is None:
            raise KeyError(worker_id)
        with self._lock:
            ctrl = self._controls.get(worker_id)
        if ctrl is not None:
            ctrl.cancel()
        run_id = w.get("run_id")
        if run_id:
            self.svc.orchestrator.abort(run_id)
            for t in self.svc.store.list_tickets("open"):
                if t.get("run_id") == run_id:
                    self.svc.tickets.resolve(t["id"], "abort", "aborted by user")
        if w["status"] in ("queued", "waiting_approval"):
            w = self.svc.store.update_worker(
                worker_id, status="aborted", error="用户已终止", latest_note="已终止", finished_at=now_iso()
            )
            self._publish_worker(w)
        return self.svc.store.get_worker(worker_id) or w

    def wait_session_idle(self, session_id: str, timeout: float = 30.0) -> list[dict[str, Any]]:
        """等待指定会话下的所有已派发子智能体执行完毕（主要供测试同步断言使用）。"""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            active = self.svc.store.list_workers(session_id=session_id, active_only=True)
            if not active:
                return self.svc.store.list_workers(session_id=session_id)
            time.sleep(0.05)
        return self.svc.store.list_workers(session_id=session_id)


class CoordinatorAgent:
    """对话式主控智能体：与用户自然语言沟通，调度与管控并行子智能体池。"""

    def __init__(self, svc: "Services"):
        self.svc = svc
        self._lock = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._reported_workers: set[str] = set()

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._lock:
            return self._session_locks.setdefault(session_id, threading.Lock())

    def _set_chat_status(self, session_id: str, status: str) -> None:
        if self.svc.store.get_chat_session(session_id) is None:
            return
        sess = self.svc.store.update_chat_session(session_id, status=status)
        self.svc.bus.publish("chat_status", {"session_id": session_id, "status": status, "session": sess})

    def send_user_message(self, session_id: str, content: str, *, wait: bool = False) -> dict[str, Any]:
        """接收用户发送的对话消息，持久化后触发主控智能体响应轮次。"""
        sess = self.svc.store.get_chat_session(session_id)
        if sess is None:
            raise KeyError(session_id)
        msg = self.svc.store.add_chat_message(session_id, "user", content.strip(), sender="user")
        self.svc.bus.publish("chat_message", {"session_id": session_id, "message": msg})
        if wait:
            reply = self._run_turn(session_id, user_message=msg)
            return {"session": self.svc.store.get_chat_session(session_id), "message": msg, "reply": reply}
        t = threading.Thread(
            target=self._run_turn,
            kwargs={"session_id": session_id, "user_message": msg},
            name=f"cuaflow-coord-{session_id[-6:]}",
            daemon=True,
        )
        t.start()
        return {"session": self.svc.store.get_chat_session(session_id), "message": msg}

    def on_worker_approval_needed(self, worker: dict[str, Any], ticket: dict[str, Any]) -> None:
        """当子智能体请求审批放行时，立即在所属对话流中推送可一键操作的放行卡片消息。"""
        sid = worker.get("session_id")
        if not sid or self.svc.store.get_chat_session(sid) is None:
            return
        reason = ticket.get("reason") or "关键步骤请求人工确认"
        content = (
            f"⏸ 子智能体 **{worker['name']}** 已暂停并请求放行：\n"
            f"> {reason}\n\n"
            "您可以直接点击卡片上的 **【放行】** 按钮，或在对话框中直接告诉我“放行”或补充修改要求。"
        )
        msg = self.svc.store.add_chat_message(
            sid,
            "assistant",
            content,
            sender="subagent",
            meta={
                "kind": "approval_request",
                "worker_id": worker["id"],
                "worker_name": worker["name"],
                "agent_kind": worker["agent_kind"],
                "ticket_id": ticket.get("id"),
                "run_id": worker.get("run_id"),
                "screenshot": ticket.get("screenshot"),
                "reason": reason,
            },
        )
        self.svc.bus.publish("chat_message", {"session_id": sid, "message": msg})

    def on_worker_finished(self, worker: dict[str, Any]) -> None:
        """当会话下所有活动子智能体均已结束时，自动唤醒主智能体汇总各路结果向用户汇报。"""
        sid = worker.get("session_id")
        if not sid or not self.svc.get_settings().coordinator.auto_report:
            return
        if self.svc.store.get_chat_session(sid) is None:
            return
        active = self.svc.store.list_workers(session_id=sid, active_only=True)
        if active:
            return
        t = threading.Thread(
            target=self._auto_report_finished_workers,
            args=(sid,),
            name=f"cuaflow-report-{sid[-6:]}",
            daemon=True,
        )
        t.start()

    def _auto_report_finished_workers(self, session_id: str) -> None:
        lock = self._get_session_lock(session_id)
        with lock:
            # 再次确认没有新派发的活动子智能体，并筛选出尚未汇总过的已完成 Worker
            if self.svc.store.list_workers(session_id=session_id, active_only=True):
                return
            all_workers = self.svc.store.list_workers(session_id=session_id, limit=50)
            unreported = [
                w for w in all_workers
                if w["status"] in ("succeeded", "failed", "aborted") and w["id"] not in self._reported_workers
            ]
            if not unreported:
                return
            for w in unreported:
                self._reported_workers.add(w["id"])
            digest = []
            for w in reversed(unreported):
                digest.append(
                    {
                        "worker_id": w["id"],
                        "name": w["name"],
                        "agent_kind": w["agent_kind"],
                        "status": w["status"],
                        "flow_id": w.get("flow_id"),
                        "output": w.get("output"),
                        "error": w.get("error"),
                    }
                )
            synth_prompt = (
                "【系统回调通知：本轮派发的并行子智能体已全部执行完毕】\n"
                f"子智能体结果明细：\n```json\n{json.dumps(digest, ensure_ascii=False, indent=2)}\n```\n"
                "请向用户清晰汇总上述子智能体的执行结果与关键产出（无需再次重复派发已完成的相同任务）。"
            )
            self._run_turn_locked(session_id, synthetic_prompt=synth_prompt)

    # ------------------------------------------------------------------ 主控 LLM 轮次执行
    def _run_turn(self, session_id: str, *, user_message: dict[str, Any] | None = None) -> dict[str, Any] | None:
        lock = self._get_session_lock(session_id)
        with lock:
            return self._run_turn_locked(session_id, user_message=user_message)

    def _run_turn_locked(
        self,
        session_id: str,
        *,
        user_message: dict[str, Any] | None = None,
        synthetic_prompt: str | None = None,
    ) -> dict[str, Any] | None:
        store = self.svc.store
        sess = store.get_chat_session(session_id)
        if sess is None:
            return None

        s = self.svc.get_settings()
        meter = UsageMeter(tokens_per_script_step=s.planner.tokens_saved_per_step, initial=sess.get("usage"))
        self._set_chat_status(session_id, "thinking")
        dispatched_in_turn: list[dict[str, Any]] = []
        actions_in_turn: list[dict[str, Any]] = []

        # 构造主智能体工具集
        def dispatch_tool(a: dict[str, Any]) -> ToolResult:
            raw_tasks = a.get("tasks") or []
            if not isinstance(raw_tasks, list) or not raw_tasks:
                return ToolResult("参数错误：tasks 必须是非空数组。")
            workers = self.svc.pool.dispatch(session_id, raw_tasks)
            dispatched_in_turn.extend(workers)
            summary = [
                {"worker_id": w["id"], "name": w["name"], "agent_kind": w["agent_kind"], "status": w["status"]}
                for w in workers
            ]
            return ToolResult(
                f"已成功并行启动 {len(workers)} 个子智能体：{json.dumps(summary, ensure_ascii=False)}。"
                "它们正在后台并发执行，请直接向用户说明分工安排。"
            )

        def steer_tool(a: dict[str, Any]) -> ToolResult:
            target = str(a.get("worker_id") or "").strip()
            instr = str(a.get("instruction") or "").strip()
            if not target or not instr:
                return ToolResult("参数错误：需要提供 worker_id 与 instruction。")
            targets = self.svc.pool.resolve_target_workers(
                target, session_id=session_id, states=("queued", "running", "waiting_approval")
            )
            if not targets:
                return ToolResult(f"未找到可调整的活动子智能体 {target!r}。")
            updated = [self.svc.pool.steer(w["id"], instr, source="coordinator") for w in targets]
            actions_in_turn.append({"type": "steer", "workers": [w["id"] for w in updated], "instruction": instr})
            return ToolResult(f"已向 {[w['name'] for w in updated]} 注入中途调整指令：{instr}")

        def approve_tool(a: dict[str, Any]) -> ToolResult:
            target = str(a.get("worker_id") or "all").strip()
            action = str(a.get("action") or "continue").strip()
            if action not in ("continue", "hint", "skip", "abort"):
                action = "continue"
            hint = str(a.get("hint") or "").strip() or None
            targets = self.svc.pool.resolve_target_workers(
                target, session_id=session_id, states=("waiting_approval", "running")
            )
            if not targets:
                return ToolResult(f"当前没有处于等待放行状态的子智能体（查询目标：{target!r}）。")
            updated = [self.svc.pool.approve(w["id"], action=action, hint=hint) for w in targets]
            actions_in_turn.append({"type": "approve", "workers": [w["id"] for w in updated], "action": action})
            return ToolResult(f"已对 {[w['name'] for w in updated]} 执行审批动作：{action}。")

        def abort_tool(a: dict[str, Any]) -> ToolResult:
            target = str(a.get("worker_id") or "").strip()
            targets = self.svc.pool.resolve_target_workers(
                target, session_id=session_id, states=("queued", "running", "waiting_approval")
            )
            if not targets:
                return ToolResult(f"未找到运行中的子智能体 {target!r}。")
            updated = [self.svc.pool.abort(w["id"]) for w in targets]
            for w in updated:
                self._reported_workers.add(w["id"])
            actions_in_turn.append({"type": "abort", "workers": [w["id"] for w in updated]})
            return ToolResult(f"已终止子智能体：{[w['name'] for w in updated]}。")

        def inspect_tool(a: dict[str, Any]) -> ToolResult:
            include_finished = bool(a.get("include_finished", True))
            ws = store.list_workers(session_id=session_id, active_only=not include_finished, limit=30)
            brief = [
                {
                    "worker_id": w["id"],
                    "name": w["name"],
                    "agent_kind": w["agent_kind"],
                    "status": w["status"],
                    "latest_note": w.get("latest_note"),
                    "flow_id": w.get("flow_id"),
                    "output": w.get("output"),
                    "error": w.get("error"),
                }
                for w in ws
            ]
            return ToolResult(json.dumps(brief, ensure_ascii=False))

        def list_flows_tool(a: dict[str, Any]) -> ToolResult:
            flows = [
                {
                    "flow_id": f["id"],
                    "name": f["name"],
                    "description": f.get("description"),
                    "params_schema": f.get("params_schema"),
                }
                for f in store.list_flows()
            ]
            return ToolResult(json.dumps(flows, ensure_ascii=False))

        def create_schedule_tool(a: dict[str, Any]) -> ToolResult:
            name = str(a.get("name") or "").strip() or "定时监控任务"
            interval = max(10, int(a.get("interval_seconds") or 300))
            flow_id = str(a.get("flow_id") or "").strip() or None
            instr = str(a.get("instruction") or "").strip() or None
            kind = "flow" if flow_id else "task"
            if kind == "flow" and store.get_flow(flow_id) is None:  # type: ignore[arg-type]
                return ToolResult(f"未找到流程 {flow_id!r}，请先使用 list_flows 确认或改传 instruction。")
            if kind == "task" and not instr:
                return ToolResult("创建任务型定时计划时必须提供 instruction 或有效的 flow_id。")
            sc = store.create_schedule(
                name=name,
                interval_seconds=interval,
                kind=kind,
                flow_id=flow_id,
                instruction=instr,
                start_url=a.get("start_url") or None,
                params=a.get("params") if isinstance(a.get("params"), dict) else {},
                enabled=True,
            )
            sc = store.update_schedule(sc["id"], next_run_at=future_iso(interval))
            actions_in_turn.append({"type": "schedule_created", "schedule_id": sc["id"], "name": sc["name"]})
            return ToolResult(f"已创建后台持续监控计划：{sc['name']} (ID: {sc['id']}, 每 {interval} 秒执行一次)。")

        tools = [
            Tool(
                tool_spec(
                    "dispatch_subagents",
                    "并行派发 1 到多个子智能体在后台同时执行任务。默认使用 explorer（直连浏览器）或 desktop（直连桌面），仅在明确需要沉淀可复用脚本时使用 author。",
                    {
                        "tasks": {
                            "type": "array",
                            "description": "要并行启动的子智能体任务列表",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "description": "简短中文任务名称，如 'GitHub 热门抓取'"},
                                    "agent_kind": {
                                        "type": "string",
                                        "enum": list(VALID_AGENT_KINDS),
                                        "description": "explorer(默认直连浏览器) | desktop(直连桌面) | supervisor(运行已有flow_id) | author(录制新脚本) | planner(长程批量)",
                                    },
                                    "instruction": {"type": "string", "description": "给该子智能体的详细执行指令"},
                                    "start_url": {"type": "string", "description": "可选起始 URL"},
                                    "flow_id": {"type": "string", "description": "当 agent_kind 为 supervisor 时必填的脚本 ID"},
                                    "params": {"type": "object", "additionalProperties": True},
                                },
                                "required": ["name", "agent_kind", "instruction"],
                            },
                        }
                    },
                    ["tasks"],
                ),
                dispatch_tool,
            ),
            Tool(
                tool_spec(
                    "steer_subagent",
                    "向正在运行的子智能体实时发送中途调整指令（支持具体 worker_id、名称关键字或 'all'）。",
                    {
                        "worker_id": {"type": "string", "description": "目标子智能体 ID、名称或 'all'"},
                        "instruction": {"type": "string", "description": "新的中途调整要求"},
                    },
                    ["worker_id", "instruction"],
                ),
                steer_tool,
            ),
            Tool(
                tool_spec(
                    "approve_subagent",
                    "放行或处理处于等待确认（waiting_approval）状态的子智能体。",
                    {
                        "worker_id": {"type": "string", "description": "目标子智能体 ID、名称或 'all'"},
                        "action": {
                            "type": "string",
                            "enum": ["continue", "hint", "skip", "abort"],
                            "description": "continue(批准放行) | hint(附带指导放行) | skip(跳过敏感步骤) | abort(终止)",
                        },
                        "hint": {"type": "string", "description": "可选补充指导说明"},
                    },
                    ["worker_id"],
                ),
                approve_tool,
            ),
            Tool(
                tool_spec(
                    "abort_subagent",
                    "立即停止指定的子智能体（或传入 'all' 停止当前会话全部子智能体）。",
                    {"worker_id": {"type": "string"}},
                    ["worker_id"],
                ),
                abort_tool,
            ),
            Tool(
                tool_spec(
                    "inspect_subagents",
                    "查看当前会话下所有子智能体的实时状态、进展说明与输出结果。",
                    {"include_finished": {"type": "boolean"}},
                ),
                inspect_tool,
            ),
            Tool(
                tool_spec("list_flows", "查询脚本库中已保存的可复用流程脚本列表。", {}),
                list_flows_tool,
            ),
            Tool(
                tool_spec(
                    "create_schedule",
                    "创建持续后台定时巡检计划（可绑定已有 flow_id 或自然语言 instruction）。",
                    {
                        "name": {"type": "string"},
                        "interval_seconds": {"type": "integer", "description": "执行间隔秒数（最小 10 秒）"},
                        "flow_id": {"type": "string"},
                        "instruction": {"type": "string"},
                        "start_url": {"type": "string"},
                        "params": {"type": "object", "additionalProperties": True},
                    },
                    ["name", "interval_seconds"],
                ),
                create_schedule_tool,
            ),
        ]

        # 组装最近历史对话与当前活跃子智能体上下文快照
        history_rows = store.list_chat_messages(session_id, limit=40)
        prior: list[dict[str, Any]] = []
        for m in history_rows:
            if user_message is not None and m["id"] == user_message["id"]:
                continue
            role = "user" if m["role"] == "user" else "assistant"
            prior.append({"role": role, "content": m["content"]})

        active_workers = store.list_workers(session_id=session_id, limit=15)
        worker_ctx = ""
        if active_workers:
            snap = [
                {
                    "worker_id": w["id"],
                    "name": w["name"],
                    "agent_kind": w["agent_kind"],
                    "status": w["status"],
                    "latest_note": w.get("latest_note"),
                }
                for w in active_workers[:10]
            ]
            worker_ctx = f"\n\n[当前会话子智能体状态快照]: {json.dumps(snap, ensure_ascii=False)}"

        prompt_input = (synthetic_prompt or (user_message["content"] if user_message else "")) + worker_ctx

        with usage_scope(meter):
            try:
                agent = ToolAgent(
                    self.svc.llms.get("coordinator"),
                    load_prompt("coordinator"),
                    tools,
                    role="coordinator",
                    allow_text_finish=True,
                )
                res = agent.run(
                    prompt_input,
                    turn_limit=s.coordinator.turn_limit,
                    prior_messages=prior,
                )
                if res.done and isinstance(res.final, dict) and res.final.get("reply"):
                    reply_text = str(res.final["reply"]).strip()
                elif dispatched_in_turn:
                    names = "、".join(f"**{w['name']}** (`{w['agent_kind']}`)" for w in dispatched_in_turn)
                    reply_text = f"已为您并行派发 {len(dispatched_in_turn)} 个子智能体开始执行：{names}。您可以在右侧面板查看实时画面，或随时在对话中调整指令。"
                elif res.reason.startswith("llm_error:"):
                    reply_text = (
                        f"⚠️ 主智能体调用大模型失败（{res.reason[10:].strip()}）。\n"
                        "请前往 **【⚙️ 设置】** 检查 API Key、接口地址与模型名称是否正确配置。"
                    )
                else:
                    reply_text = "已处理您的请求。"
            except Exception as exc:  # noqa: BLE001
                reply_text = f"⚠️ 主智能体处理异常：{exc}。请检查模型设置后重试。"

        meta: dict[str, Any] = {}
        if dispatched_in_turn:
            meta["dispatched_workers"] = [
                {"id": w["id"], "name": w["name"], "agent_kind": w["agent_kind"]} for w in dispatched_in_turn
            ]
        if actions_in_turn:
            meta["actions"] = actions_in_turn

        assistant_msg = store.add_chat_message(
            session_id, "assistant", reply_text, sender="coordinator", meta=meta
        )
        store.update_chat_session(session_id, status="idle", usage=meter.snapshot())
        self.svc.bus.publish("chat_message", {"session_id": session_id, "message": assistant_msg})
        self.svc.bus.publish(
            "chat_status",
            {"session_id": session_id, "status": "idle", "session": store.get_chat_session(session_id)},
        )
        return assistant_msg
