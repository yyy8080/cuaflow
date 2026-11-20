"""实现 docs/api.md 规范（REST + WebSocket）并托管 Web 控制台的 FastAPI 服务端。"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from cuaflow import __version__
from cuaflow.config import Settings
from cuaflow.library.store import future_iso
from cuaflow.services import Services

STATIC = Path(__file__).parent / "static"


class RunRequest(BaseModel):
    params: dict[str, Any] = {}
    version: int | None = None
    from_step: str | None = None
    resume_checkpoint: bool = False
    unattended: bool | None = None


class RollbackRequest(BaseModel):
    version: int


class TaskRequest(BaseModel):
    instruction: str
    start_url: str | None = None
    params: dict[str, Any] = {}
    force_new: bool = False
    mode: str = "auto"


class ScheduleCreateRequest(BaseModel):
    name: str
    interval_seconds: int = 300
    kind: str = "flow"
    flow_id: str | None = None
    instruction: str | None = None
    start_url: str | None = None
    params: dict[str, Any] = {}
    max_runs: int | None = None
    max_consecutive_failures: int = 3
    enabled: bool = True
    run_immediately: bool = False


class ScheduleUpdateRequest(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = None
    max_consecutive_failures: int | None = None
    reset_breaker: bool = False


class ResolveRequest(BaseModel):
    action: str
    hint: str | None = None


def create_app(svc: Services, token: str | None = None) -> FastAPI:
    token = token or secrets.token_urlsafe(24)
    app = FastAPI(title="cuaflow", version=__version__)
    app.state.svc, app.state.token = svc, token
    store, orch = svc.store, svc.orchestrator

    def auth(request: Request) -> None:
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token", "")
        if not secrets.compare_digest(supplied or "", token):
            raise HTTPException(401, "invalid or missing token")

    A = [Depends(auth)]

    def need(obj: Any, what: str) -> Any:
        if obj is None:
            raise HTTPException(404, f"{what} not found")
        return obj

    # ------------------------------------------------------------------ 健康检查与统计信息
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "platform": sys.platform}

    @app.get("/api/stats", dependencies=A)
    def stats() -> dict[str, Any]:
        return store.global_stats()

    # ------------------------------------------------------------------ 工作流管理
    @app.get("/api/flows", dependencies=A)
    def flows() -> list[dict[str, Any]]:
        return store.list_flows()

    @app.get("/api/flows/{flow_id}", dependencies=A)
    def flow(flow_id: str) -> dict[str, Any]:
        f = need(store.get_flow(flow_id), "flow")
        return {**f, "versions": store.list_versions(flow_id)}

    @app.get("/api/flows/{flow_id}/versions/{version}", dependencies=A)
    def version(flow_id: str, version: int) -> dict[str, Any]:
        return need(store.get_version(flow_id, version), "version")

    @app.post("/api/flows/{flow_id}/run", dependencies=A)
    def run_flow(flow_id: str, req: RunRequest) -> dict[str, Any]:
        need(store.get_flow(flow_id), "flow")
        try:
            return orch.submit_run(flow_id, req.params, version=req.version, from_step=req.from_step,
                                   resume_checkpoint=req.resume_checkpoint, unattended=req.unattended)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/flows/{flow_id}/rollback", dependencies=A)
    def rollback(flow_id: str, req: RollbackRequest) -> dict[str, Any]:
        need(store.get_flow(flow_id), "flow")
        try:
            f = store.set_current_version(flow_id, req.version)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        svc.bus.publish("log", {"level": "info", "text": f"{flow_id} rolled back to v{req.version}"}, flow_id=flow_id)
        return f

    @app.delete("/api/flows/{flow_id}", dependencies=A)
    def delete_flow(flow_id: str) -> dict[str, Any]:
        need(store.get_flow(flow_id), "flow")
        store.delete_flow(flow_id)
        return {"ok": True}

    # ------------------------------------------------------------------ 任务（长程使命）管理
    @app.post("/api/tasks", dependencies=A)
    def create_task(req: TaskRequest) -> dict[str, Any]:
        if not req.instruction.strip():
            raise HTTPException(400, "instruction is empty")
        if req.mode not in ("auto", "mission", "single"):
            raise HTTPException(400, f"invalid mode {req.mode!r}")
        return orch.submit_task(req.instruction, req.start_url, req.params, req.force_new, mode=req.mode)

    @app.get("/api/tasks", dependencies=A)
    def tasks() -> list[dict[str, Any]]:
        return store.list_tasks()

    @app.get("/api/tasks/{task_id}", dependencies=A)
    def task(task_id: str) -> dict[str, Any]:
        return need(store.get_task(task_id), "task")

    @app.get("/api/tasks/{task_id}/events", dependencies=A)
    def task_events(task_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        need(store.get_task(task_id), "task")
        return store.list_task_events(task_id, after_seq)

    @app.post("/api/tasks/{task_id}/resume", dependencies=A)
    def resume_task(task_id: str) -> dict[str, Any]:
        need(store.get_task(task_id), "task")
        return orch.resume_task(task_id)

    @app.post("/api/tasks/{task_id}/abort", dependencies=A)
    def abort_task(task_id: str) -> dict[str, Any]:
        need(store.get_task(task_id), "task")
        orch.abort(task_id)
        return store.get_task(task_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------ 定时计划（持续巡检监控）
    @app.get("/api/schedules", dependencies=A)
    def list_schedules() -> list[dict[str, Any]]:
        return store.list_schedules()

    @app.post("/api/schedules", dependencies=A)
    def create_schedule(req: ScheduleCreateRequest) -> dict[str, Any]:
        if not req.name.strip():
            raise HTTPException(400, "schedule name is empty")
        if req.kind == "flow":
            if not req.flow_id or store.get_flow(req.flow_id) is None:
                raise HTTPException(404, f"flow {req.flow_id!r} not found")
        elif req.kind == "task":
            if not (req.instruction or "").strip():
                raise HTTPException(400, "task schedule requires instruction")
        else:
            raise HTTPException(400, f"invalid schedule kind {req.kind!r}")
        sc = store.create_schedule(
            req.name.strip(),
            req.interval_seconds,
            kind=req.kind,
            flow_id=req.flow_id,
            instruction=req.instruction,
            start_url=req.start_url,
            params=req.params,
            max_runs=req.max_runs,
            max_consecutive_failures=req.max_consecutive_failures,
            enabled=req.enabled,
            run_immediately=req.run_immediately,
        )
        if req.run_immediately and req.enabled:
            orch.jobs.put(("schedule", sc["id"], None))
        return sc

    @app.patch("/api/schedules/{schedule_id}", dependencies=A)
    def patch_schedule(schedule_id: str, req: ScheduleUpdateRequest) -> dict[str, Any]:
        sc = need(store.get_schedule(schedule_id), "schedule")
        fields: dict[str, Any] = {}
        if req.interval_seconds is not None:
            fields["interval_seconds"] = max(5, int(req.interval_seconds))
        if req.max_consecutive_failures is not None:
            fields["max_consecutive_failures"] = max(1, int(req.max_consecutive_failures))
        if req.reset_breaker:
            fields.update(consecutive_failures=0, enabled=True, status="active",
                          next_run_at=future_iso(fields.get("interval_seconds", sc["interval_seconds"])))
        elif req.enabled is not None:
            fields["enabled"] = bool(req.enabled)
            fields["status"] = "active" if req.enabled else "paused"
            if req.enabled:
                fields["next_run_at"] = future_iso(fields.get("interval_seconds", sc["interval_seconds"]))
        return store.update_schedule(schedule_id, **fields)

    @app.post("/api/schedules/{schedule_id}/trigger", dependencies=A)
    def trigger_schedule(schedule_id: str) -> dict[str, Any]:
        sc = need(store.get_schedule(schedule_id), "schedule")
        orch.jobs.put(("schedule", schedule_id, None))
        return {"ok": True, "schedule": sc}

    @app.delete("/api/schedules/{schedule_id}", dependencies=A)
    def delete_schedule(schedule_id: str) -> dict[str, Any]:
        need(store.get_schedule(schedule_id), "schedule")
        store.delete_schedule(schedule_id)
        return {"ok": True}

    # ------------------------------------------------------------------ 运行记录与产物
    @app.get("/api/runs", dependencies=A)
    def runs(flow_id: str | None = None, limit: int = Query(100, le=1000)) -> list[dict[str, Any]]:
        return store.list_runs(flow_id, limit)

    @app.get("/api/runs/{run_id}", dependencies=A)
    def run(run_id: str) -> dict[str, Any]:
        return need(store.get_run(run_id), "run")

    @app.get("/api/runs/{run_id}/events", dependencies=A)
    def run_events(run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        return store.list_events(run_id, after_seq)

    @app.get("/api/runs/{run_id}/artifacts/{path:path}", dependencies=A)
    def artifact(run_id: str, path: str) -> FileResponse:
        need(store.get_run(run_id), "run")
        base = store.run_dir(run_id).resolve()
        target = (base / path).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise HTTPException(404, "artifact not found")
        return FileResponse(target)

    @app.post("/api/runs/{run_id}/abort", dependencies=A)
    def abort(run_id: str) -> dict[str, Any]:
        need(store.get_run(run_id), "run")
        orch.abort(run_id)
        for t in store.list_tickets("open"):
            if t["run_id"] == run_id:
                svc.tickets.resolve(t["id"], "abort", "run aborted")
        return store.get_run(run_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------ 人工介入工单
    @app.get("/api/tickets", dependencies=A)
    def tickets(status: str | None = None) -> list[dict[str, Any]]:
        return store.list_tickets(status)

    @app.post("/api/tickets/{ticket_id}/resolve", dependencies=A)
    def resolve(ticket_id: str, req: ResolveRequest) -> dict[str, Any]:
        need(store.get_ticket(ticket_id), "ticket")
        try:
            return svc.tickets.resolve(ticket_id, req.action, req.hint)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    # ------------------------------------------------------------------ 系统设置
    @app.get("/api/settings", dependencies=A)
    def get_settings() -> dict[str, Any]:
        from cuaflow.config import masked_settings_dict

        return masked_settings_dict(svc.get_settings())

    @app.put("/api/settings", dependencies=A)
    def put_settings(body: dict[str, Any]) -> dict[str, Any]:
        from cuaflow.config import masked_settings_dict, restore_masked_keys

        current = svc.get_settings()
        restored = restore_masked_keys(body, current)
        merged = {**current.model_dump(), **restored}
        try:
            s = Settings.model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, str(exc)) from None
        return masked_settings_dict(svc.update_settings(s))

    # ------------------------------------------------------------------ WebSocket 实时事件流
    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket) -> None:
        if not secrets.compare_digest(ws.query_params.get("token", ""), token):
            await ws.close(code=4401)
            return
        await ws.accept()
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        filt: dict[str, Any] = {}

        def on_event(ev: dict[str, Any]) -> None:
            run_id = filt.get("run_id")
            if run_id and ev.get("run_id") != run_id:
                return
            task_id = filt.get("task_id")
            if task_id and ev.get("task_id") != task_id:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, ev)
            except (RuntimeError, asyncio.QueueFull):
                pass

        unsubscribe = svc.bus.subscribe(on_event)

        async def reader() -> None:
            while True:
                msg = await ws.receive_text()
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and isinstance(data.get("subscribe"), dict):
                    filt.clear()
                    filt.update({k: v for k, v in data["subscribe"].items() if v})

        rtask = asyncio.create_task(reader())
        try:
            while True:
                get = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({get, rtask}, return_when=asyncio.FIRST_COMPLETED)
                if rtask in done:
                    get.cancel()
                    break
                await ws.send_text(json.dumps(get.result(), ensure_ascii=False, default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            unsubscribe()
            rtask.cancel()

    # ------------------------------------------------------------------ 单页应用（SPA）静态资源回退
    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith(("api/", "ws/")):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = (STATIC / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(STATIC.resolve()):
            return FileResponse(candidate)
        index = STATIC / "index.html"
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse("<h1>cuaflow</h1><p>Web console not built. Run <code>cd web && npm install && "
                            "npm run build</code>.</p>")

    return app
