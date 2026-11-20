# cuaflow HTTP / WebSocket API contract

All endpoints are served by `cuaflow serve` (FastAPI) on `127.0.0.1:8765` by default.

**Auth**: every `/api/*` request and WebSocket must carry the access token, either as
`Authorization: Bearer <token>` header or `?token=<token>` query parameter. `cuaflow serve`
prints `http://127.0.0.1:8765/?token=<token>`; the web console reads `token` from the URL
once, stores it in `localStorage["cuaflow_token"]`, and strips it from the address bar.
A missing / wrong token returns `401`.

Timestamps are ISO-8601 strings (UTC). IDs are short strings.

## Data models

```ts
type ParamSpec = { type: "str" | "int" | "float" | "bool"; description?: string; default?: any; required?: boolean };

type StepSpec = { id: string; title: string; expect?: string; side_effect?: boolean; critical?: boolean };

type Flow = {
  id: string; name: string; description: string;
  intent_template: string;              // e.g. "Export the {month} report to {save_path}"
  params_schema: Record<string, ParamSpec>;
  output_schema: Record<string, ParamSpec>;
  side_effects: boolean;
  current_version: number;
  created_at: string; updated_at: string;
  tags: string[];
  stats: { runs: number; successes: number; repairs: number; last_run_at?: string; last_status?: RunStatus };
};

type Version = {
  flow_id: string; version: number; parent_version?: number;
  created_at: string;
  author: "author" | "repairer" | "human";
  reason: string;                       // changelog text
  repaired_step?: string;               // step id patched (repairer versions)
  steps: StepSpec[];
  health: { runs: number; successes: number };
  source?: string;                      // flow.py text (only in single-version GET)
  diff?: string;                        // unified diff vs parent (only in single-version GET)
};

type RunStatus = "queued" | "running" | "repairing" | "waiting_human" | "succeeded" | "failed" | "aborted";
type StepStatus = "pending" | "running" | "passed" | "failed" | "repairing" | "repaired" | "skipped";

type Check = { level: "L0" | "L1" | "L2" | "L3"; passed: boolean; detail: string; ts: string };

type Repair = {
  attempt: number; started_at: string; ended_at?: string;
  status: "running" | "succeeded" | "failed";
  summary?: string; diff?: string;      // diff of the patched step function
  new_version?: number;
};

type StepRecord = {
  step_id: string; index: number; title: string; expect?: string;
  status: StepStatus; attempts: number;
  started_at?: string; ended_at?: string; duration_ms?: number;
  error_kind?: string; error?: string;
  screenshot?: string;                  // relative artifact path, fetch via /api/runs/{id}/artifacts/{path}
  checks: Check[]; repairs: Repair[];
};

type Run = {
  id: string; kind: "flow" | "author";
  flow_id?: string; version?: number; task_id?: string;
  params: Record<string, any>;
  status: RunStatus;
  created_at: string; started_at?: string; finished_at?: string;
  output?: Record<string, any>; error?: string;
  steps: StepRecord[];
  new_version?: number;                 // set if the run produced a new flow version
  final_verdict?: { passed: boolean; detail: string };   // L3
};

type Task = {
  id: string; instruction: string; start_url?: string; params: Record<string, any>;
  status: "queued" | "matching" | "authoring" | "running" | "succeeded" | "failed" | "aborted";
  match?: { verdict: "run" | "adapt" | "new"; flow_id?: string; params?: Record<string, any>; reason?: string };
  run_id?: string; flow_id?: string;
  final_response?: string;
  created_at: string; updated_at: string;
};

type Ticket = {
  id: string; run_id: string; step_id?: string; flow_id?: string;
  reason: string; screenshot?: string;
  status: "open" | "resolved";
  created_at: string; resolved_at?: string;
  resolution?: { action: "continue" | "hint" | "skip" | "abort"; hint?: string };
};

type ModelSpec = { base_url: string; model: string; api_key_env: string; vision?: boolean; max_output_tokens?: number };

type Settings = {
  models: { default: ModelSpec; roles: Partial<Record<"author" | "supervisor" | "repairer" | "vision_judge", ModelSpec>> };
  supervisor: {
    l2_policy: "always" | "on_change" | "sample" | "off";
    sample_rate: number;                // used when l2_policy == "sample"
    l3_enabled: boolean;
    max_repair_attempts: number;
    auto_rollback: boolean;
    rollback_after_failures: number;
  };
  safety: { blocked_patterns: string[]; confirm_side_effects: boolean };
};

type Event = {
  seq: number; ts: string; type: EventType;
  run_id?: string; task_id?: string; flow_id?: string; step_id?: string;
  data: Record<string, any>;
};
type EventType =
  | "task_status"      // data: {status, match?}
  | "run_status"       // data: {status, error?}
  | "step_start"       // data: {index, title}
  | "step_end"         // data: StepRecord
  | "check"            // data: Check
  | "repair_start"     // data: {attempt}
  | "repair_log"       // data: {text}   (repairer thought / command / observation, streamed)
  | "repair_end"       // data: Repair
  | "agent_log"        // data: {role: "author"|"supervisor"|"repairer", kind: "thought"|"command"|"observation", text}
  | "ticket_open"      // data: Ticket
  | "ticket_resolved"  // data: Ticket
  | "version_created"  // data: Version (without source)
  | "log";             // data: {level, text}
```

## REST endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/health` | – | `{ok: true, version, platform}` (no auth) |
| GET | `/api/flows` | – | `Flow[]` |
| GET | `/api/flows/{id}` | – | `Flow & {versions: Version[]}` (no source/diff) |
| GET | `/api/flows/{id}/versions/{v}` | – | `Version` with `source` and `diff` |
| POST | `/api/flows/{id}/run` | `{params, version?, from_step?}` | `Run` |
| POST | `/api/flows/{id}/rollback` | `{version}` | `Flow` |
| DELETE | `/api/flows/{id}` | – | `{ok: true}` |
| POST | `/api/tasks` | `{instruction, start_url?, params?, force_new?: boolean}` | `Task` |
| GET | `/api/tasks` | – | `Task[]` (newest first) |
| GET | `/api/tasks/{id}` | – | `Task` |
| GET | `/api/runs` | query `flow_id?`, `limit?` | `Run[]` (newest first, `steps` included) |
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
Clients may send `{"subscribe": {"run_id": "..."}}` to restrict to one run (or `{"subscribe": {}}`
for all). On (re)connect a client should backfill via `GET /api/runs/{id}/events?after_seq=N`.
