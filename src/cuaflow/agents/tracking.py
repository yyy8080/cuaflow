"""供 `Supervisor` 与 `Author` 共用的运行状态记录器：步骤记录、事件广播、人工工单与取消令牌。"""

from __future__ import annotations

import threading
from typing import Any

from cuaflow.flow.engine import now_iso
from cuaflow.library.events import EventBus
from cuaflow.library.store import Store


class CancelToken:
    def __init__(self) -> None:
        self._ev = threading.Event()

    def cancel(self) -> None:
        self._ev.set()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()


class TicketBroker:
    """人机协同工单代理。``wait`` 会阻塞工作线程，直至控制台处理并结单。"""

    def __init__(self, store: Store, bus: EventBus):
        self.store, self.bus = store, bus
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def open(self, run_id: str, reason: str, step_id: str | None = None, flow_id: str | None = None,
             screenshot: str | None = None) -> dict[str, Any]:
        t = self.store.create_ticket(run_id, reason, step_id=step_id, flow_id=flow_id, screenshot=screenshot)
        with self._lock:
            self._events[t["id"]] = threading.Event()
        self.bus.publish("ticket_open", t, run_id=run_id, flow_id=flow_id, step_id=step_id)
        return t

    def wait(self, ticket_id: str, cancel: CancelToken | None = None, timeout: float | None = None) -> dict:
        with self._lock:
            ev = self._events.setdefault(ticket_id, threading.Event())
        waited = 0.0
        while not ev.wait(0.5):
            waited += 0.5
            if cancel is not None and cancel.cancelled:
                self.resolve(ticket_id, "abort", "run aborted")
                break
            if timeout is not None and waited >= timeout:
                self.resolve(ticket_id, "abort", "ticket timed out")
                break
        t = self.store.get_ticket(ticket_id) or {}
        return t.get("resolution") or {"action": "abort"}

    def resolve(self, ticket_id: str, action: str, hint: str | None = None) -> dict[str, Any]:
        if action not in ("continue", "hint", "skip", "abort"):
            raise ValueError(f"invalid action {action!r}")
        t = self.store.get_ticket(ticket_id)
        if t is None:
            raise KeyError(ticket_id)
        if t["status"] == "resolved":
            return t
        t = self.store.resolve_ticket(ticket_id, action, hint)
        with self._lock:
            ev = self._events.get(ticket_id)
        if ev is not None:
            ev.set()
        self.bus.publish("ticket_resolved", t, run_id=t["run_id"], flow_id=t.get("flow_id"), step_id=t.get("step_id"))
        return t


class RunTracker:
    def __init__(self, store: Store, bus: EventBus, run_id: str, flow_id: str | None = None,
                 task_id: str | None = None):
        self.store, self.bus, self.run_id = store, bus, run_id
        self.flow_id, self.task_id = flow_id, task_id
        self.records: list[dict[str, Any]] = []

    # --- 步骤记录 -----------------------------------------------------------
    def init_steps(self, steps: list[dict[str, Any]], keep_existing: bool = False) -> None:
        existing = {r["step_id"]: r for r in self.records} if keep_existing else {}
        self.records = [existing.get(s["id"]) or {
            "step_id": s["id"], "index": i, "title": s.get("title", s["id"]), "expect": s.get("expect", ""),
            "status": "pending", "attempts": 0, "checks": [], "repairs": []} for i, s in enumerate(steps)]
        for i, r in enumerate(self.records):
            r["index"] = i
        self.persist()

    def rec(self, step_id: str) -> dict[str, Any]:
        for r in self.records:
            if r["step_id"] == step_id:
                return r
        r = {"step_id": step_id, "index": len(self.records), "title": step_id, "status": "pending", "attempts": 0,
             "checks": [], "repairs": []}
        self.records.append(r)
        return r

    def persist(self) -> None:
        self.store.update_run(self.run_id, steps=self.records)

    # --- 事件发布 -----------------------------------------------------------
    def emit(self, type: str, data: dict[str, Any] | None = None, step_id: str | None = None) -> None:  # noqa: A002
        self.bus.publish(type, data or {}, run_id=self.run_id, flow_id=self.flow_id, task_id=self.task_id,
                         step_id=step_id)

    def status(self, status: str, **fields: Any) -> dict[str, Any]:
        if status == "running" and not (self.store.get_run(self.run_id) or {}).get("started_at"):
            fields.setdefault("started_at", now_iso())
        if status in ("succeeded", "failed", "aborted"):
            fields.setdefault("finished_at", now_iso())
        run = self.store.update_run(self.run_id, status=status, **fields)
        self.emit("run_status", {"status": status, **{k: v for k, v in fields.items() if k in ("error",)}})
        return run

    def log(self, role: str, kind: str, text: str, step_id: str | None = None) -> None:
        self.emit("agent_log", {"role": role, "kind": kind, "text": text[:6000]}, step_id=step_id)

    def step_start(self, step_id: str) -> None:
        r = self.rec(step_id)
        r.update(status="running", started_at=r.get("started_at") or now_iso())
        self.persist()
        self.emit("step_start", {"index": r["index"], "title": r["title"]}, step_id=step_id)

    def step_result(self, step_id: str, result: dict[str, Any], checks: list[dict[str, Any]], status: str) -> None:
        r = self.rec(step_id)
        r["attempts"] = r.get("attempts", 0) + 1
        r.update(status=status, ended_at=result.get("ended_at") or now_iso(), duration_ms=result.get("duration_ms"),
                 error_kind=result.get("error_kind"), error=result.get("error"))
        if result.get("screenshot"):
            r["screenshot"] = result["screenshot"]
        r["checks"] = r.get("checks", []) + checks
        for c in checks:
            self.emit("check", c, step_id=step_id)
        self.persist()
        self.emit("step_end", r, step_id=step_id)

    def set_step_status(self, step_id: str, status: str, **fields: Any) -> None:
        r = self.rec(step_id)
        r.update(status=status, **fields)
        self.persist()
        self.emit("step_end", r, step_id=step_id)
