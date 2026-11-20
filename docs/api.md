# cuaflow HTTP / WebSocket API contract

All endpoints are served by `cuaflow serve` (FastAPI) on `127.0.0.1:8765` by default.

**Auth**: every `/api/*` request (except `/api/health`) and WebSocket must carry the access token,
either as `Authorization: Bearer <token>` header or `?token=<token>` query parameter. `cuaflow serve`
prints `http://127.0.0.1:8765/?token=<token>`; the web console reads `token` from the URL once,
stores it in `localStorage["cuaflow_token"]`, and strips it from the address bar.
A missing / wrong token returns `401`.

Timestamps are ISO-8601 strings (UTC). IDs are short strings.

## Data models

```ts
type ParamSpec = { type: "str" | "int" | "float" | "bool"; description?: string; default?: any; required?: boolean };

type StepSpec = { id: string; title: string; expect?: string; side_effect?: boolean; critical?: boolean };

type UsageSnapshot = {
  llm_calls: number; prompt_tokens: number; completion_tokens: number; total_tokens: number;
  script_steps: number; estimated_tokens_saved: number; savings_ratio: number;
  by_role?: Record<string, { calls: number; tokens: number }>;
};

type GlobalStats = {
  flows: number; auto_repairs: number;
  flow_runs: number; flow_successes: number; flow_success_rate: number;
  tasks: number; tasks_succeeded: number;
  llm_calls: number; total_tokens: number;
  script_steps: number; estimated_tokens_saved: number; savings_ratio: number;
  schedules_active: number; schedules_tripped: number;
};

type Flow = {
  id: string; name: string; description: string;
  intent_template: string;
  params_schema: Record<string, ParamSpec>;
  output_schema: Record<string, ParamSpec>;
  side_effects: boolean;
  current_version: number;
  created_at: string; updated_at: string;
  tags: string[];
  stats: {
    runs: number; successes: number; repairs: number;
    last_run_at?: string; last_status?: RunStatus;
    script_steps?: number; estimated_tokens_saved?: number;
  };
};

type Version = {
  flow_id: string; version: number; parent_version?: number;
  created_at: string;
  author: "author" | "repairer" | "human";
  reason: string;
  repaired_step?: string;
  steps: StepSpec[];
  health: { runs: number; successes: number };
  source?: string;
  diff?: string;
};

type RunStatus = "queued" | "running" | "repairing" | "waiting_human" | "succeeded" | "failed" | "aborted";
type StepStatus = "pending" | "running" | "passed" | "failed" | "repairing" | "repaired" | "skipped";

type Check = { level: "L0" | "L1" | "L1.5" | "L2" | "L3"; passed: boolean; detail: string; ts: string };

type Repair = {
  attempt: number; started_at: string; ended_at?: string;
  status: "running" | "succeeded" | "failed";
  summary?: string; diff?: string;
  new_version?: number;
};

type StepRecord = {
  step_id: string; index: number; title: string; expect?: string;
  status: StepStatus; attempts: number;
  started_at?: string; ended_at?: string; duration_ms?: number;
  error_kind?: string; error?: string;
  screenshot?: string;
  checks: Check[]; repairs: Repair[];
};

type Run = {
  id: string; kind: "flow" | "author" | "mission";
  flow_id?: string; version?: number; task_id?: string;
  params: Record<string, any>;
  status: RunStatus;
  created_at: string; started_at?: string; finished_at?: string;
  output?: Record<string, any>; error?: string;
  steps: StepRecord[];
  new_version?: number;
  final_verdict?: { passed: boolean; detail: string };
  usage?: UsageSnapshot;
};

type PlanPhase = {
  id: string; title: string;
  kind?: "explore" | "skill" | "batch" | "synthesize";
  status?: "pending" | "running" | "done" | "skipped" | "failed";
  note?: string;
};

type Task = {
  id: string; instruction: string; start_url?: string; params: Record<string, any>;
  mode?: "auto" | "mission" | "single";
  status: "queued" | "matching" | "planning" | "authoring" | "running" | "succeeded" | "failed" | "aborted";
  match?: { verdict: "run" | "adapt" | "mission" | "new"; flow_id?: string; params?: Record<string, any>; reason?: string };
  run_id?: string; flow_id?: string;
  final_response?: string;
  plan?: { goal?: string; phases: PlanPhase[] };
  checkpoint?: Record<string, any>;
  usage?: UsageSnapshot;
  sub_runs?: string[];
  created_at: string; updated_at: string;
};

type Schedule = {
  id: string; name: string; kind: "flow" | "task";
  flow_id?: string; instruction?: string; start_url?: string;
  params: Record<string, any>;
  interval_seconds: number; max_runs?: number;
  enabled: boolean; max_consecutive_failures: number;
  runs_count: number; success_count: number; consecutive_failures: number;
  last_run_id?: string; last_run_at?: string; last_status?: string; next_run_at?: string;
  status: "active" | "paused" | "tripped" | "completed";
  created_at: string; updated_at: string;
};

type Ticket = {
  id: string; run_id: string; step_id?: string; flow_id?: string;
  reason: string; screenshot?: string;
  status: "open" | "resolved";
  created_at: string; resolved_at?: string;
  resolution?: { action: "continue" | "hint" | "skip" | "abort"; hint?: string };
};
```

## REST endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/health` | – | `{ok: true, version, platform}` (no auth) |
| GET | `/api/stats` | – | `GlobalStats` (tokens, script savings, stability, schedules) |
| GET | `/api/flows` | – | `Flow[]` |
| GET | `/api/flows/{id}` | – | `Flow & {versions: Version[]}` (no source/diff) |
| GET | `/api/flows/{id}/versions/{v}` | – | `Version` with `source` and `diff` |
| POST | `/api/flows/{id}/run` | `{params, version?, from_step?, resume_checkpoint?, unattended?}` | `Run` |
| POST | `/api/flows/{id}/rollback` | `{version}` | `Flow` |
| DELETE | `/api/flows/{id}` | – | `{ok: true}` |
| POST | `/api/tasks` | `{instruction, start_url?, params?, force_new?, mode?: "auto"\|"mission"\|"single"}` | `Task` |
| GET | `/api/tasks` | – | `Task[]` (newest first) |
| GET | `/api/tasks/{id}` | – | `Task` |
| GET | `/api/tasks/{id}/events` | query `after_seq?` | `Event[]` |
| POST | `/api/tasks/{id}/resume` | – | `Task` (resumes from saved checkpoint) |
| POST | `/api/tasks/{id}/abort` | – | `Task` |
| GET | `/api/schedules` | – | `Schedule[]` |
| POST | `/api/schedules` | `{name, interval_seconds, kind, flow_id?, instruction?, params?, max_consecutive_failures?, run_immediately?}` | `Schedule` |
| PATCH | `/api/schedules/{id}` | `{enabled?, interval_seconds?, max_consecutive_failures?, reset_breaker?}` | `Schedule` |
| POST | `/api/schedules/{id}/trigger` | – | `{ok: true, schedule}` |
| DELETE | `/api/schedules/{id}` | – | `{ok: true}` |
| GET | `/api/runs` | query `flow_id?`, `limit?` | `Run[]` |
| GET | `/api/runs/{id}` | – | `Run` |
| GET | `/api/runs/{id}/events` | query `after_seq?` | `Event[]` |
| GET | `/api/runs/{id}/artifacts/{path}` | – | file (screenshots, logs) |
| POST | `/api/runs/{id}/abort` | – | `Run` |
| GET | `/api/tickets` | query `status?` | `Ticket[]` |
| POST | `/api/tickets/{id}/resolve` | `{action, hint?}` | `Ticket` |
| GET | `/api/settings` | – | `Settings` |
| PUT | `/api/settings` | `Settings` | `Settings` |

## WebSocket

`/ws/events?token=...` — server pushes every `Event` as a JSON text frame, in `seq` order.
Clients may send `{"subscribe": {"run_id": "..."}}` or `{"subscribe": {"task_id": "..."}}` to filter,
or `{"subscribe": {}}` for all.
