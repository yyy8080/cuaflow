# cuaflow HTTP / WebSocket API 契约

所有接口默认由 `cuaflow serve`（基于 FastAPI）在 `127.0.0.1:8765` 上提供服务。

**鉴权**：除 `/api/health` 外的所有 `/api/*` 请求及 WebSocket 连接均须携带访问令牌，可通过 `Authorization: Bearer <token>` 请求头或 `?token=<token>` 查询参数传递。`cuaflow serve` 启动时会打印 `http://127.0.0.1:8765/?token=<token>`；Web 控制台首次从 URL 读取 `token` 后，会将其存入 `localStorage["cuaflow_token"]` 并从地址栏移除。缺失或错误的令牌将返回 `401`。

时间戳均为 ISO-8601 字符串（UTC），各类 ID 均为短字符串。

## 数据模型

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

## REST 接口

| 方法 | 路径 | 请求体 / 参数 | 返回值 |
|---|---|---|---|
| GET | `/api/health` | – | `{ok: true, version, platform}`（无需鉴权） |
| GET | `/api/stats` | – | `GlobalStats`（Token 消耗、脚本节省量、并发 Worker、稳定性与定时监控统计） |
| GET | `/api/chats` | 查询参数 `limit?` | `ChatSession[]` |
| POST | `/api/chats` | `{title?}` | `ChatSession` |
| GET | `/api/chats/{id}` | – | `ChatSession & {messages: ChatMessage[], workers: Worker[]}` |
| DELETE | `/api/chats/{id}` | – | `{ok: true}` |
| POST | `/api/chats/{id}/messages` | `{content, wait?}` | `{session, message, reply?}`（异步唤醒主控智能体响应与并发调度） |
| GET | `/api/workers` | 查询参数 `session_id?`、`active_only?`、`limit?` | `Worker[]` |
| POST | `/api/workers/dispatch` | `{session_id?, tasks: [...]}` | `Worker[]`（直接向并行池并发投递多个子智能体） |
| GET | `/api/workers/{id}` | – | `Worker` |
| POST | `/api/workers/{id}/steer` | `{instruction}` | `Worker`（向运行中的子智能体实时注入中途动态调整指令） |
| POST | `/api/workers/{id}/approve` | `{action: "continue"\|"hint"\|"skip"\|"abort", hint?}` | `Worker`（放行或处理子智能体挂起请求） |
| POST | `/api/workers/{id}/abort` | – | `Worker` |
| GET | `/api/flows` | – | `Flow[]` |
| GET | `/api/flows/{id}` | – | `Flow & {versions: Version[]}`（不含 `source` 与 `diff`） |
| GET | `/api/flows/{id}/versions/{v}` | – | 含 `source` 与 `diff` 的 `Version` |
| POST | `/api/flows/{id}/run` | `{params, version?, from_step?, resume_checkpoint?, unattended?}` | `Run` |
| POST | `/api/flows/{id}/rollback` | `{version}` | `Flow` |
| DELETE | `/api/flows/{id}` | – | `{ok: true}` |
| POST | `/api/tasks` | `{instruction, start_url?, params?, force_new?, mode?: "auto"\|"direct"\|"mission"\|"single"}` | `Task` |
| GET | `/api/tasks` | – | `Task[]`（按最新优先排序） |
| GET | `/api/tasks/{id}` | – | `Task` |
| GET | `/api/tasks/{id}/events` | 查询参数 `after_seq?` | `Event[]` |
| POST | `/api/tasks/{id}/resume` | – | `Task`（从已保存的检查点恢复执行） |
| POST | `/api/tasks/{id}/abort` | – | `Task` |
| GET | `/api/schedules` | – | `Schedule[]` |
| POST | `/api/schedules` | `{name, interval_seconds, kind, flow_id?, instruction?, params?, max_consecutive_failures?, run_immediately?}` | `Schedule` |
| PATCH | `/api/schedules/{id}` | `{enabled?, interval_seconds?, max_consecutive_failures?, reset_breaker?}` | `Schedule` |
| POST | `/api/schedules/{id}/trigger` | – | `{ok: true, schedule}` |
| DELETE | `/api/schedules/{id}` | – | `{ok: true}` |
| GET | `/api/runs` | 查询参数 `flow_id?`、`limit?` | `Run[]` |
| GET | `/api/runs/{id}` | – | `Run` |
| GET | `/api/runs/{id}/events` | 查询参数 `after_seq?` | `Event[]` |
| GET | `/api/runs/{id}/artifacts/{path}` | – | 产物文件（截图、日志等） |
| POST | `/api/runs/{id}/abort` | – | `Run` |
| GET | `/api/tickets` | 查询参数 `status?` | `Ticket[]` |
| POST | `/api/tickets/{id}/resolve` | `{action, hint?}` | `Ticket` |
| GET | `/api/settings` | – | `Settings` |
| PUT | `/api/settings` | `Settings` | `Settings` |

## WebSocket

`/ws/events?token=...` — 服务端按 `seq` 顺序将每条 `Event` 以 JSON 文本帧推送给客户端。
客户端可发送 `{"subscribe": {"run_id": "..."}}`、`{"subscribe": {"task_id": "..."}}` 或 `{"subscribe": {"session_id": "..."}}` 按条件过滤，
也可发送 `{"subscribe": {}}` 订阅全部事件。
