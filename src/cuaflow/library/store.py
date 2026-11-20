"""持久化存储：流程与版本（磁盘文件），以及运行记录、任务、定时调度、工单和事件（SQLite）。"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from cuaflow.flow.patching import unified_diff

SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
  id TEXT PRIMARY KEY, name TEXT, description TEXT, intent_template TEXT,
  params_schema TEXT, output_schema TEXT, side_effects INTEGER, start_url TEXT,
  current_version INTEGER, tags TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS versions (
  flow_id TEXT, version INTEGER, parent_version INTEGER, created_at TEXT, author TEXT,
  reason TEXT, repaired_step TEXT, steps TEXT, PRIMARY KEY (flow_id, version)
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, kind TEXT, flow_id TEXT, version INTEGER, task_id TEXT, params TEXT,
  status TEXT, created_at TEXT, started_at TEXT, finished_at TEXT, output TEXT, error TEXT,
  steps TEXT, new_version INTEGER, final_verdict TEXT, usage TEXT, telemetry TEXT
);
CREATE TABLE IF NOT EXISTS step_stats (
  flow_id TEXT, step_id TEXT, runs INTEGER DEFAULT 0, passes INTEGER DEFAULT 0,
  total_ms INTEGER DEFAULT 0, sq_ms REAL DEFAULT 0, needs_verify INTEGER DEFAULT 1, last_status TEXT,
  PRIMARY KEY (flow_id, step_id)
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, instruction TEXT, start_url TEXT, params TEXT, status TEXT, match TEXT,
  run_id TEXT, flow_id TEXT, final_response TEXT, force_new INTEGER, created_at TEXT, updated_at TEXT,
  mode TEXT DEFAULT 'auto', plan TEXT, checkpoint TEXT, usage TEXT, sub_runs TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY, run_id TEXT, step_id TEXT, flow_id TEXT, reason TEXT, screenshot TEXT,
  status TEXT, created_at TEXT, resolved_at TEXT, resolution TEXT
);
CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY, name TEXT, kind TEXT, flow_id TEXT, instruction TEXT, start_url TEXT,
  params TEXT, interval_seconds INTEGER, max_runs INTEGER, enabled INTEGER DEFAULT 1,
  max_consecutive_failures INTEGER DEFAULT 3, runs_count INTEGER DEFAULT 0,
  success_count INTEGER DEFAULT 0, consecutive_failures INTEGER DEFAULT 0,
  last_run_id TEXT, last_run_at TEXT, last_status TEXT, next_run_at TEXT,
  status TEXT DEFAULT 'active', created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, type TEXT, run_id TEXT, task_id TEXT,
  flow_id TEXT, step_id TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS chat_sessions (
  id TEXT PRIMARY KEY, title TEXT, status TEXT DEFAULT 'idle', summary TEXT, usage TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id TEXT PRIMARY KEY, session_id TEXT, role TEXT, sender TEXT, content TEXT, meta TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS workers (
  id TEXT PRIMARY KEY, session_id TEXT, name TEXT, agent_kind TEXT, instruction TEXT,
  start_url TEXT, params TEXT, flow_id TEXT, run_id TEXT, status TEXT,
  latest_note TEXT, latest_screenshot TEXT, output TEXT, error TEXT,
  steering_log TEXT, usage TEXT, started_at TEXT, finished_at TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_task ON events(task_id, seq);
CREATE INDEX IF NOT EXISTS ix_runs_flow ON runs(flow_id, created_at);
CREATE INDEX IF NOT EXISTS ix_chat_msgs_session ON chat_messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS ix_workers_session ON workers(session_id, created_at);
"""

_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "runs": [("usage", "TEXT"), ("telemetry", "TEXT")],
    "tasks": [
        ("mode", "TEXT DEFAULT 'auto'"),
        ("plan", "TEXT"),
        ("checkpoint", "TEXT"),
        ("usage", "TEXT"),
        ("sub_runs", "TEXT"),
    ],
}

_JSON_COLS = {
    "params_schema", "output_schema", "tags", "steps", "params", "output", "final_verdict", "match",
    "resolution", "data", "usage", "telemetry", "plan", "checkpoint", "sub_runs", "meta", "steering_log",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def future_iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1.0, float(seconds)))).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:max_len].strip("_")
    return slug


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "flows").mkdir(exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.root / "cuaflow.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        for table, cols in _MIGRATIONS.items():
            existing = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, decl in cols:
                if col not in existing:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    # ------------------------------------------------------------------ 工具方法
    def _row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = {}
        for k in row.keys():
            v = row[k]
            if k in _JSON_COLS and isinstance(v, str):
                try:
                    v = json.loads(v)
                except json.JSONDecodeError:
                    pass
            out[k] = v
        return out

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, tuple(args))
            self._db.commit()
            return cur

    def _query(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [self._row(r) for r in self._db.execute(sql, tuple(args)).fetchall()]  # type: ignore[misc]

    def _one(self, sql: str, args: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self._query(sql, args)
        return rows[0] if rows else None

    @staticmethod
    def _j(v: Any) -> str:
        return json.dumps(v, ensure_ascii=False, default=str)

    def _update(self, table: str, key: str, key_val: Any, fields: dict[str, Any]) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = [self._j(v) if k in _JSON_COLS else (int(v) if isinstance(v, bool) else v) for k, v in fields.items()]
        self._exec(f"UPDATE {table} SET {cols} WHERE {key} = ?", [*vals, key_val])

    # ------------------------------------------------------------------ 路径辅助
    def flow_dir(self, flow_id: str) -> Path:
        return self.root / "flows" / flow_id

    def version_path(self, flow_id: str, version: int) -> Path:
        return self.flow_dir(flow_id) / "versions" / f"v{version}" / "flow.py"

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def task_dir(self, task_id: str) -> Path:
        p = self.root / "tasks" / task_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ------------------------------------------------------------------ 流程管理
    def create_flow(self, meta: dict[str, Any], source: str, author: str = "author",
                    reason: str = "initial recording", tags: list[str] | None = None) -> dict[str, Any]:
        base = slugify(meta.get("name", "")) or "flow"
        flow_id = f"{base}_{uuid.uuid4().hex[:6]}"
        ts = now_iso()
        self._exec(
            "INSERT INTO flows (id, name, description, intent_template, params_schema, output_schema, "
            "side_effects, start_url, current_version, tags, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [flow_id, meta.get("name", flow_id), meta.get("description", ""), meta.get("intent_template", ""),
             self._j(meta.get("params_schema", {})), self._j(meta.get("output_schema", {})),
             int(bool(meta.get("side_effects"))), meta.get("start_url"), 0, self._j(tags or []), ts, ts])
        self.add_version(flow_id, source, meta.get("steps", []), author=author, reason=reason)
        return self.get_flow(flow_id)  # type: ignore[return-value]

    def add_version(self, flow_id: str, source: str, steps: list[dict[str, Any]], author: str, reason: str,
                    repaired_step: str | None = None, parent_version: int | None = None,
                    meta: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            row = self._one("SELECT MAX(version) AS v FROM versions WHERE flow_id = ?", [flow_id])
            version = int(row["v"] or 0) + 1 if row else 1
            flow = self._one("SELECT current_version FROM flows WHERE id = ?", [flow_id])
            parent = parent_version if parent_version is not None else (flow or {}).get("current_version") or None
            path = self.version_path(flow_id, version)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
            self._exec(
                "INSERT INTO versions (flow_id, version, parent_version, created_at, author, reason, "
                "repaired_step, steps) VALUES (?,?,?,?,?,?,?,?)",
                [flow_id, version, parent, now_iso(), author, reason, repaired_step, self._j(steps)],
            )
            fields: dict[str, Any] = {"current_version": version, "updated_at": now_iso()}
            if meta:
                for k in ("name", "description", "intent_template", "params_schema", "output_schema",
                          "side_effects", "start_url"):
                    if k in meta:
                        fields[k] = meta[k]
            self._update("flows", "id", flow_id, fields)
            if repaired_step:
                for sid in str(repaired_step).split(","):
                    if sid.strip():
                        self.mark_needs_verify(flow_id, sid.strip())
            elif parent is None:
                for s in steps:
                    self.mark_needs_verify(flow_id, s["id"])
        return self.get_version(flow_id, version, with_source=False)  # type: ignore[return-value]

    def _stats(self, flow_id: str) -> dict[str, Any]:
        row = self._one(
            "SELECT COUNT(*) AS runs, SUM(status='succeeded') AS successes, MAX(created_at) AS last_run_at "
            "FROM runs WHERE flow_id = ? AND kind = 'flow'", [flow_id]) or {}
        last = self._one("SELECT status FROM runs WHERE flow_id = ? AND kind='flow' ORDER BY created_at DESC "
                         "LIMIT 1", [flow_id])
        rep = self._one("SELECT COUNT(*) AS n FROM versions WHERE flow_id = ? AND author = 'repairer'", [flow_id])
        usage_rows = self._query("SELECT usage FROM runs WHERE flow_id = ? AND kind = 'flow' AND usage IS NOT NULL",
                                 [flow_id])
        script_steps = 0
        saved = 0
        for ur in usage_rows:
            u = ur.get("usage") or {}
            if isinstance(u, dict):
                script_steps += int(u.get("script_steps") or 0)
                saved += int(u.get("estimated_tokens_saved") or 0)
        return {
            "runs": row.get("runs") or 0,
            "successes": row.get("successes") or 0,
            "repairs": (rep or {}).get("n", 0),
            "last_run_at": row.get("last_run_at"),
            "last_status": (last or {}).get("status"),
            "script_steps": script_steps,
            "estimated_tokens_saved": saved,
        }

    def _flow_out(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row["side_effects"] = bool(row["side_effects"])
        row["stats"] = self._stats(row["id"])
        return row

    def get_flow(self, flow_id: str) -> dict[str, Any] | None:
        return self._flow_out(self._one("SELECT * FROM flows WHERE id = ?", [flow_id]))

    def list_flows(self) -> list[dict[str, Any]]:
        return [self._flow_out(r) for r in self._query("SELECT * FROM flows ORDER BY updated_at DESC")]  # type: ignore[misc]

    def delete_flow(self, flow_id: str) -> None:
        import shutil

        self._exec("DELETE FROM flows WHERE id = ?", [flow_id])
        self._exec("DELETE FROM versions WHERE flow_id = ?", [flow_id])
        self._exec("DELETE FROM step_stats WHERE flow_id = ?", [flow_id])
        shutil.rmtree(self.flow_dir(flow_id), ignore_errors=True)

    def set_current_version(self, flow_id: str, version: int) -> dict[str, Any]:
        if not self.version_path(flow_id, version).is_file():
            raise KeyError(f"{flow_id} has no version {version}")
        self._update("flows", "id", flow_id, {"current_version": version, "updated_at": now_iso()})
        return self.get_flow(flow_id)  # type: ignore[return-value]

    def read_source(self, flow_id: str, version: int | None = None) -> str:
        if version is None:
            version = self.get_flow(flow_id)["current_version"]  # type: ignore[index]
        return self.version_path(flow_id, version).read_text(encoding="utf-8")

    def get_version(self, flow_id: str, version: int, with_source: bool = True) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM versions WHERE flow_id = ? AND version = ?", [flow_id, version])
        if row is None:
            return None
        h = self._one("SELECT COUNT(*) AS runs, SUM(status='succeeded') AS successes FROM runs "
                      "WHERE flow_id = ? AND version = ? AND kind='flow'", [flow_id, version]) or {}
        row["health"] = {"runs": h.get("runs") or 0, "successes": h.get("successes") or 0}
        if with_source:
            row["source"] = self.read_source(flow_id, version)
            parent = row.get("parent_version")
            row["diff"] = unified_diff(self.read_source(flow_id, parent), row["source"]) if parent else ""
        return row

    def list_versions(self, flow_id: str) -> list[dict[str, Any]]:
        rows = self._query("SELECT version FROM versions WHERE flow_id = ? ORDER BY version DESC", [flow_id])
        return [self.get_version(flow_id, r["version"], with_source=False) for r in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------ 步骤统计
    def mark_needs_verify(self, flow_id: str, step_id: str, value: bool = True) -> None:
        self._exec("INSERT INTO step_stats (flow_id, step_id, needs_verify) VALUES (?,?,?) "
                   "ON CONFLICT(flow_id, step_id) DO UPDATE SET needs_verify = excluded.needs_verify",
                   [flow_id, step_id, int(value)])

    def record_step(self, flow_id: str, step_id: str, passed: bool, duration_ms: int) -> None:
        self._exec(
            "INSERT INTO step_stats (flow_id, step_id, runs, passes, total_ms, sq_ms, needs_verify, last_status) "
            "VALUES (?,?,1,?,?,?,?,?) ON CONFLICT(flow_id, step_id) DO UPDATE SET runs = runs + 1, "
            "passes = passes + excluded.passes, total_ms = total_ms + excluded.total_ms, "
            "sq_ms = sq_ms + excluded.sq_ms, last_status = excluded.last_status, "
            "needs_verify = CASE WHEN excluded.passes = 0 THEN 1 ELSE needs_verify END",
            [flow_id, step_id, int(passed), duration_ms, float(duration_ms) ** 2, int(not passed),
             "passed" if passed else "failed"])

    def step_stats(self, flow_id: str, step_id: str) -> dict[str, Any]:
        return self._one("SELECT * FROM step_stats WHERE flow_id = ? AND step_id = ?", [flow_id, step_id]) or {
            "runs": 0, "passes": 0, "total_ms": 0, "sq_ms": 0.0, "needs_verify": 1}

    # ------------------------------------------------------------------ 运行记录
    def create_run(self, kind: str, params: dict[str, Any], flow_id: str | None = None,
                   version: int | None = None, task_id: str | None = None,
                   steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        run_id = new_id("run")
        self._exec(
            "INSERT INTO runs (id, kind, flow_id, version, task_id, params, status, created_at, "
            "started_at, finished_at, output, error, steps, new_version, final_verdict, usage, telemetry) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [run_id, kind, flow_id, version, task_id, self._j(params), "queued", now_iso(), None, None,
             self._j({}), None, self._j(steps or []), None, None, None, None],
        )
        self.run_dir(run_id).mkdir(parents=True, exist_ok=True)
        return self.get_run(run_id)  # type: ignore[return-value]

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        self._update("runs", "id", run_id, fields)
        return self.get_run(run_id)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM runs WHERE id = ?", [run_id])

    def list_runs(self, flow_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if flow_id:
            return self._query("SELECT * FROM runs WHERE flow_id = ? ORDER BY created_at DESC LIMIT ?",
                               [flow_id, limit])
        return self._query("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", [limit])

    def recent_version_results(self, flow_id: str, version: int, n: int) -> list[str]:
        rows = self._query("SELECT status FROM runs WHERE flow_id = ? AND version = ? AND kind='flow' AND "
                           "status IN ('succeeded','failed') ORDER BY created_at DESC LIMIT ?",
                           [flow_id, version, n])
        return [r["status"] for r in rows]

    # ------------------------------------------------------------------ 任务管理
    def create_task(self, instruction: str, start_url: str | None, params: dict[str, Any],
                    force_new: bool = False, mode: str = "auto") -> dict[str, Any]:
        tid, ts = new_id("task"), now_iso()
        self._exec(
            "INSERT INTO tasks (id, instruction, start_url, params, status, match, run_id, flow_id, "
            "final_response, force_new, created_at, updated_at, mode, plan, checkpoint, usage, sub_runs) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [tid, instruction, start_url, self._j(params or {}), "queued", None, None, None, None,
             int(force_new), ts, ts, mode or "auto", None, None, None, self._j([])],
        )
        return self.get_task(tid)  # type: ignore[return-value]

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        fields["updated_at"] = now_iso()
        self._update("tasks", "id", task_id, fields)
        return self.get_task(task_id)  # type: ignore[return-value]

    def append_task_run(self, task_id: str, run_id: str) -> None:
        with self._lock:
            task = self.get_task(task_id)
            if not task:
                return
            sub = list(task.get("sub_runs") or [])
            if run_id not in sub:
                sub.append(run_id)
            self.update_task(task_id, sub_runs=sub, run_id=run_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM tasks WHERE id = ?", [task_id])
        if row:
            row["force_new"] = bool(row.get("force_new"))
            row.setdefault("mode", "auto")
            if row.get("sub_runs") is None:
                row["sub_runs"] = []
        return row

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", [limit])
        for row in rows:
            row["force_new"] = bool(row.get("force_new"))
            row.setdefault("mode", "auto")
            if row.get("sub_runs") is None:
                row["sub_runs"] = []
        return rows

    # ------------------------------------------------------------------ 定时调度（持续监控）
    def create_schedule(
        self,
        name: str,
        interval_seconds: int,
        *,
        kind: str = "flow",
        flow_id: str | None = None,
        instruction: str | None = None,
        start_url: str | None = None,
        params: dict[str, Any] | None = None,
        max_runs: int | None = None,
        max_consecutive_failures: int = 3,
        enabled: bool = True,
        run_immediately: bool = False,
    ) -> dict[str, Any]:
        sid = new_id("sched")
        ts = now_iso()
        interval = max(5, int(interval_seconds))
        next_at = ts if run_immediately else future_iso(interval)
        status = "active" if enabled else "paused"
        self._exec(
            "INSERT INTO schedules (id, name, kind, flow_id, instruction, start_url, params, interval_seconds, "
            "max_runs, enabled, max_consecutive_failures, runs_count, success_count, consecutive_failures, "
            "last_run_id, last_run_at, last_status, next_run_at, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [sid, name, kind, flow_id, instruction, start_url, self._j(params or {}), interval, max_runs,
             int(enabled), max(1, int(max_consecutive_failures)), 0, 0, 0, None, None, None, next_at, status, ts, ts],
        )
        return self.get_schedule(sid)  # type: ignore[return-value]

    def _sched_out(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row["enabled"] = bool(row.get("enabled"))
        return row

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        return self._sched_out(self._one("SELECT * FROM schedules WHERE id = ?", [schedule_id]))

    def list_schedules(self) -> list[dict[str, Any]]:
        return [self._sched_out(r) for r in self._query("SELECT * FROM schedules ORDER BY created_at DESC")]  # type: ignore[misc]

    def update_schedule(self, schedule_id: str, **fields: Any) -> dict[str, Any]:
        fields["updated_at"] = now_iso()
        self._update("schedules", "id", schedule_id, fields)
        return self.get_schedule(schedule_id)  # type: ignore[return-value]

    def delete_schedule(self, schedule_id: str) -> None:
        self._exec("DELETE FROM schedules WHERE id = ?", [schedule_id])

    def due_schedules(self, ts: str | None = None) -> list[dict[str, Any]]:
        ts = ts or now_iso()
        rows = self._query(
            "SELECT * FROM schedules WHERE enabled = 1 AND status = 'active' AND next_run_at <= ? "
            "ORDER BY next_run_at ASC",
            [ts],
        )
        return [self._sched_out(r) for r in rows]  # type: ignore[misc]

    def record_schedule_run(self, schedule_id: str, run_or_task_id: str, succeeded: bool) -> dict[str, Any]:
        with self._lock:
            sc = self.get_schedule(schedule_id)
            if sc is None:
                raise KeyError(schedule_id)
            runs_count = int(sc.get("runs_count") or 0) + 1
            success_count = int(sc.get("success_count") or 0) + (1 if succeeded else 0)
            consec = 0 if succeeded else int(sc.get("consecutive_failures") or 0) + 1
            max_runs = sc.get("max_runs")
            max_fail = int(sc.get("max_consecutive_failures") or 3)
            enabled = bool(sc.get("enabled"))
            status = sc.get("status") or "active"
            if consec >= max_fail:
                enabled = False
                status = "tripped"
            elif max_runs is not None and runs_count >= int(max_runs):
                enabled = False
                status = "completed"
            next_at = future_iso(sc["interval_seconds"]) if enabled else None
            return self.update_schedule(
                schedule_id,
                runs_count=runs_count,
                success_count=success_count,
                consecutive_failures=consec,
                last_run_id=run_or_task_id,
                last_run_at=now_iso(),
                last_status="succeeded" if succeeded else "failed",
                next_run_at=next_at,
                enabled=enabled,
                status=status,
            )

    # ------------------------------------------------------------------ 全局遥测统计
    def global_stats(self) -> dict[str, Any]:
        flows_n = (self._one("SELECT COUNT(*) AS n FROM flows") or {}).get("n") or 0
        repairs_n = (self._one("SELECT COUNT(*) AS n FROM versions WHERE author = 'repairer'") or {}).get("n") or 0
        runs_row = self._one(
            "SELECT COUNT(*) AS total, SUM(status='succeeded') AS ok, SUM(status='failed') AS failed "
            "FROM runs WHERE kind = 'flow'"
        ) or {}
        tasks_row = self._one(
            "SELECT COUNT(*) AS total, SUM(status='succeeded') AS ok FROM tasks"
        ) or {}
        sched_rows = self.list_schedules()

        total_tokens = 0
        llm_calls = 0
        script_steps = 0
        tokens_saved = 0
        for row in self._query("SELECT usage, task_id FROM runs WHERE usage IS NOT NULL"):
            u = row.get("usage")
            if isinstance(u, dict) and not row.get("task_id"):
                total_tokens += int(u.get("total_tokens") or 0)
                llm_calls += int(u.get("llm_calls") or 0)
                script_steps += int(u.get("script_steps") or 0)
                tokens_saved += int(u.get("estimated_tokens_saved") or 0)
        for row in self._query("SELECT usage FROM tasks WHERE usage IS NOT NULL"):
            u = row.get("usage")
            if isinstance(u, dict):
                total_tokens += int(u.get("total_tokens") or 0)
                llm_calls += int(u.get("llm_calls") or 0)
                script_steps += int(u.get("script_steps") or 0)
                tokens_saved += int(u.get("estimated_tokens_saved") or 0)

        for row in self._query("SELECT usage FROM chat_sessions WHERE usage IS NOT NULL"):
            u = row.get("usage")
            if isinstance(u, dict):
                total_tokens += int(u.get("total_tokens") or 0)
                llm_calls += int(u.get("llm_calls") or 0)

        workers_active = (
            self._one(
                "SELECT COUNT(*) AS n FROM workers WHERE status IN ('queued', 'running', 'waiting_approval')"
            )
            or {}
        ).get("n") or 0
        sessions_n = (self._one("SELECT COUNT(*) AS n FROM chat_sessions") or {}).get("n") or 0

        denom = total_tokens + tokens_saved
        flow_runs = int(runs_row.get("total") or 0)
        flow_ok = int(runs_row.get("ok") or 0)
        return {
            "flows": int(flows_n),
            "auto_repairs": int(repairs_n),
            "flow_runs": flow_runs,
            "flow_successes": flow_ok,
            "flow_success_rate": round(flow_ok / flow_runs, 4) if flow_runs > 0 else 1.0,
            "tasks": int(tasks_row.get("total") or 0),
            "tasks_succeeded": int(tasks_row.get("ok") or 0),
            "llm_calls": llm_calls,
            "total_tokens": total_tokens,
            "script_steps": script_steps,
            "estimated_tokens_saved": tokens_saved,
            "savings_ratio": round(tokens_saved / denom, 4) if denom > 0 else 0.0,
            "schedules_active": sum(1 for s in sched_rows if s.get("status") == "active" and s.get("enabled")),
            "schedules_tripped": sum(1 for s in sched_rows if s.get("status") == "tripped"),
            "active_workers": int(workers_active),
            "chat_sessions": int(sessions_n),
        }

    # ------------------------------------------------------------------ 人工工单
    def create_ticket(self, run_id: str, reason: str, step_id: str | None = None, flow_id: str | None = None,
                      screenshot: str | None = None) -> dict[str, Any]:
        tid = new_id("tkt")
        self._exec("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?)",
                   [tid, run_id, step_id, flow_id, reason, screenshot, "open", now_iso(), None, None])
        return self.get_ticket(tid)  # type: ignore[return-value]

    def resolve_ticket(self, ticket_id: str, action: str, hint: str | None = None) -> dict[str, Any]:
        self._update("tickets", "id", ticket_id, {"status": "resolved", "resolved_at": now_iso(),
                                                  "resolution": {"action": action, "hint": hint}})
        return self.get_ticket(ticket_id)  # type: ignore[return-value]

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM tickets WHERE id = ?", [ticket_id])

    def list_tickets(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self._query("SELECT * FROM tickets WHERE status = ? ORDER BY created_at DESC", [status])
        return self._query("SELECT * FROM tickets ORDER BY created_at DESC")

    # ------------------------------------------------------------------ 对话会话与消息
    def create_chat_session(self, title: str = "新对话") -> dict[str, Any]:
        """创建新的主智能体对话会话。"""
        sid = new_id("chat")
        ts = now_iso()
        self._exec(
            "INSERT INTO chat_sessions VALUES (?,?,?,?,?,?,?)",
            [sid, (title or "新对话").strip(), "idle", None, None, ts, ts],
        )
        return self.get_chat_session(sid)  # type: ignore[return-value]

    def get_chat_session(self, session_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM chat_sessions WHERE id = ?", [session_id])

    def list_chat_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM chat_sessions ORDER BY updated_at DESC LIMIT ?", [limit])

    def update_chat_session(self, session_id: str, **fields: Any) -> dict[str, Any]:
        fields["updated_at"] = now_iso()
        self._update("chat_sessions", "id", session_id, fields)
        return self.get_chat_session(session_id)  # type: ignore[return-value]

    def delete_chat_session(self, session_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            self._db.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
            self._db.commit()

    def add_chat_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        sender: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """向指定会话追加一条消息，并在首条用户消息到达时自动更新会话标题。"""
        mid = new_id("msg")
        ts = now_iso()
        sender_name = sender or ("user" if role == "user" else "coordinator")
        with self._lock:
            self._exec(
                "INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?)",
                [mid, session_id, role, sender_name, content, self._j(meta or {}), ts],
            )
            sess = self.get_chat_session(session_id)
            if sess is not None:
                upd: dict[str, Any] = {}
                if role == "user" and sess.get("title") in (None, "", "新对话"):
                    clean = " ".join(content.strip().split())
                    if clean:
                        upd["title"] = clean[:32] + ("…" if len(clean) > 32 else "")
                self.update_chat_session(session_id, **upd)
        return self._one("SELECT * FROM chat_messages WHERE id = ?", [mid])  # type: ignore[return-value]

    def list_chat_messages(self, session_id: str, limit: int = 500) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY rowid ASC LIMIT ?",
            [session_id, limit],
        )

    # ------------------------------------------------------------------ 并行子智能体 Worker
    def create_worker(
        self,
        session_id: str | None,
        name: str,
        agent_kind: str,
        instruction: str,
        *,
        start_url: str | None = None,
        params: dict[str, Any] | None = None,
        flow_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """注册一个由主智能体或并发池派发的子智能体 Worker 记录。"""
        wid = new_id("wrk")
        ts = now_iso()
        self._exec(
            "INSERT INTO workers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                wid,
                session_id,
                name,
                agent_kind,
                instruction,
                start_url,
                self._j(params or {}),
                flow_id,
                run_id,
                "queued",
                "等待分配执行槽位…",
                None,
                None,
                None,
                self._j([]),
                None,
                None,
                None,
                ts,
                ts,
            ],
        )
        return self.get_worker(wid)  # type: ignore[return-value]

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM workers WHERE id = ?", [worker_id])

    def list_workers(
        self,
        session_id: str | None = None,
        active_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            args.append(session_id)
        if active_only:
            clauses.append("status IN ('queued', 'running', 'waiting_approval')")
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        args.append(limit)
        return self._query(f"SELECT * FROM workers {where}ORDER BY created_at DESC LIMIT ?", args)

    def update_worker(self, worker_id: str, **fields: Any) -> dict[str, Any]:
        fields["updated_at"] = now_iso()
        self._update("workers", "id", worker_id, fields)
        return self.get_worker(worker_id)  # type: ignore[return-value]

    def append_worker_steering(self, worker_id: str, instruction: str, source: str = "user") -> dict[str, Any]:
        """向子智能体追加一条中途调整记录并持久化。"""
        with self._lock:
            w = self.get_worker(worker_id)
            if w is None:
                raise KeyError(worker_id)
            log = list(w.get("steering_log") or [])
            log.append({"ts": now_iso(), "source": source, "instruction": instruction})
            return self.update_worker(worker_id, steering_log=log, latest_note=f"已接收调整指令: {instruction[:80]}")

    # ------------------------------------------------------------------ 事件日志
    def add_event(self, ev: dict[str, Any]) -> dict[str, Any]:
        cur = self._exec("INSERT INTO events (ts, type, run_id, task_id, flow_id, step_id, data) "
                         "VALUES (?,?,?,?,?,?,?)",
                         [ev["ts"], ev["type"], ev.get("run_id"), ev.get("task_id"), ev.get("flow_id"),
                          ev.get("step_id"), self._j(ev.get("data", {}))])
        return {**ev, "seq": cur.lastrowid}

    def list_events(self, run_id: str, after_seq: int = 0, limit: int = 5000) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                           [run_id, after_seq, limit])

    def list_task_events(self, task_id: str, after_seq: int = 0, limit: int = 5000) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM events WHERE task_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                           [task_id, after_seq, limit])

