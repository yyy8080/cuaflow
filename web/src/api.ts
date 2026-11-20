// cuaflow HTTP API 的强类型客户端，类型定义与 docs/api.md（权威契约）保持一致。

export type ParamType = 'str' | 'int' | 'float' | 'bool';
export type ParamSpec = { type: ParamType; description?: string; default?: any; required?: boolean };

export type StepSpec = { id: string; title: string; expect?: string; side_effect?: boolean; critical?: boolean };

export type RunStatus = 'queued' | 'running' | 'repairing' | 'waiting_human' | 'succeeded' | 'failed' | 'aborted';
export type StepStatus = 'pending' | 'running' | 'passed' | 'failed' | 'repairing' | 'repaired' | 'skipped';

export type UsageSnapshot = {
  llm_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  script_steps: number;
  estimated_tokens_saved: number;
  savings_ratio: number;
  by_role?: Record<string, { calls: number; tokens: number }>;
};

export type GlobalStats = {
  flows: number;
  auto_repairs: number;
  flow_runs: number;
  flow_successes: number;
  flow_success_rate: number;
  tasks: number;
  tasks_succeeded: number;
  llm_calls: number;
  total_tokens: number;
  script_steps: number;
  estimated_tokens_saved: number;
  savings_ratio: number;
  schedules_active: number;
  schedules_tripped: number;
  active_workers?: number;
  chat_sessions?: number;
};

export type AgentKind = 'explorer' | 'desktop' | 'supervisor' | 'author' | 'planner';
export type WorkerStatus = 'queued' | 'running' | 'waiting_approval' | 'succeeded' | 'failed' | 'aborted';

export type SteeringEntry = {
  ts: string;
  source: string;
  instruction: string;
};

export type Worker = {
  id: string;
  session_id?: string;
  name: string;
  agent_kind: AgentKind;
  instruction: string;
  start_url?: string;
  params: Record<string, any>;
  flow_id?: string;
  run_id?: string;
  status: WorkerStatus;
  latest_note?: string;
  latest_screenshot?: string;
  output?: Record<string, any>;
  error?: string;
  steering_log?: SteeringEntry[];
  usage?: UsageSnapshot;
  started_at?: string;
  finished_at?: string;
  created_at: string;
  updated_at: string;
};

export type ChatSession = {
  id: string;
  title: string;
  status: 'idle' | 'thinking';
  summary?: string;
  usage?: UsageSnapshot;
  created_at: string;
  updated_at: string;
};

export type ChatMessage = {
  id: string;
  session_id: string;
  role: 'user' | 'assistant' | 'system';
  sender: string;
  content: string;
  meta?: {
    kind?: 'approval_request';
    worker_id?: string;
    worker_name?: string;
    agent_kind?: AgentKind;
    ticket_id?: string;
    run_id?: string;
    screenshot?: string;
    reason?: string;
    dispatched_workers?: Array<{ id: string; name: string; agent_kind: AgentKind }>;
    actions?: Array<Record<string, any>>;
  };
  created_at: string;
};

export type ChatSessionDetail = ChatSession & {
  messages: ChatMessage[];
  workers: Worker[];
};

export type Flow = {
  id: string;
  name: string;
  description: string;
  intent_template: string;
  params_schema: Record<string, ParamSpec>;
  output_schema: Record<string, ParamSpec>;
  side_effects: boolean;
  current_version: number;
  created_at: string;
  updated_at: string;
  tags: string[];
  stats: {
    runs: number;
    successes: number;
    repairs: number;
    last_run_at?: string;
    last_status?: RunStatus;
    script_steps?: number;
    estimated_tokens_saved?: number;
  };
};

export type Version = {
  flow_id: string;
  version: number;
  parent_version?: number;
  created_at: string;
  author: 'author' | 'repairer' | 'human';
  reason: string;
  repaired_step?: string;
  steps: StepSpec[];
  health: { runs: number; successes: number };
  source?: string;
  diff?: string;
};

export type FlowDetail = Flow & { versions: Version[] };

export type Check = { level: 'L0' | 'L1' | 'L1.5' | 'L2' | 'L3'; passed: boolean; detail: string; ts: string };

export type Repair = {
  attempt: number;
  started_at: string;
  ended_at?: string;
  status: 'running' | 'succeeded' | 'failed';
  summary?: string;
  diff?: string;
  new_version?: number;
};

export type StepRecord = {
  step_id: string;
  index: number;
  title: string;
  expect?: string;
  status: StepStatus;
  attempts: number;
  started_at?: string;
  ended_at?: string;
  duration_ms?: number;
  error_kind?: string;
  error?: string;
  screenshot?: string;
  checks: Check[];
  repairs: Repair[];
};

export type Run = {
  id: string;
  kind: 'flow' | 'author' | 'mission' | 'direct';
  flow_id?: string;
  version?: number;
  task_id?: string;
  params: Record<string, any>;
  status: RunStatus;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  output?: Record<string, any>;
  error?: string;
  steps: StepRecord[];
  new_version?: number;
  final_verdict?: { passed: boolean; detail: string };
  usage?: UsageSnapshot;
};

export type TaskMode = 'auto' | 'mission' | 'single' | 'direct';
export type TaskStatus = 'queued' | 'matching' | 'planning' | 'authoring' | 'running' | 'succeeded' | 'failed' | 'aborted';

export type PlanPhase = {
  id: string;
  title: string;
  kind?: 'explore' | 'skill' | 'batch' | 'synthesize';
  status?: 'pending' | 'running' | 'done' | 'skipped' | 'failed';
  note?: string;
};

export type MissionPlan = {
  goal?: string;
  phases: PlanPhase[];
};

export type Task = {
  id: string;
  instruction: string;
  start_url?: string;
  params: Record<string, any>;
  mode?: TaskMode;
  status: TaskStatus;
  match?: { verdict: 'run' | 'adapt' | 'mission' | 'new'; flow_id?: string; params?: Record<string, any>; reason?: string };
  run_id?: string;
  flow_id?: string;
  final_response?: string;
  plan?: MissionPlan;
  checkpoint?: {
    updated_at?: string;
    batches?: Record<string, { flow_id?: string; completed_count: number; total: number }>;
  };
  usage?: UsageSnapshot;
  sub_runs?: string[];
  created_at: string;
  updated_at: string;
};

export type ScheduleStatus = 'active' | 'paused' | 'tripped' | 'completed';
export type Schedule = {
  id: string;
  name: string;
  kind: 'flow' | 'task';
  flow_id?: string;
  instruction?: string;
  start_url?: string;
  params: Record<string, any>;
  interval_seconds: number;
  max_runs?: number;
  enabled: boolean;
  max_consecutive_failures: number;
  runs_count: number;
  success_count: number;
  consecutive_failures: number;
  last_run_id?: string;
  last_run_at?: string;
  last_status?: string;
  next_run_at?: string;
  status: ScheduleStatus;
  created_at: string;
  updated_at: string;
};

export type TicketAction = 'continue' | 'hint' | 'skip' | 'abort';
export type Ticket = {
  id: string;
  run_id: string;
  step_id?: string;
  flow_id?: string;
  reason: string;
  screenshot?: string;
  status: 'open' | 'resolved';
  created_at: string;
  resolved_at?: string;
  resolution?: { action: TicketAction; hint?: string };
};

export type ModelSpec = {
  base_url: string;
  model: string;
  api_key?: string;
  api_key_env: string;
  vision?: boolean;
  max_output_tokens?: number;
};
export type ModelRole =
  | 'coordinator'
  | 'operator'
  | 'planner'
  | 'author'
  | 'supervisor'
  | 'repairer'
  | 'fallback'
  | 'vision_judge';
export const MODEL_ROLES: ModelRole[] = [
  'coordinator',
  'operator',
  'planner',
  'author',
  'supervisor',
  'repairer',
  'fallback',
  'vision_judge',
];

export type L2Policy = 'always' | 'on_change' | 'sample' | 'off';
export type Settings = {
  models: { default: ModelSpec; roles: Partial<Record<ModelRole, ModelSpec>> };
  coordinator?: {
    max_parallel_workers?: number;
    turn_limit?: number;
    operator_turn_limit?: number;
    max_auto_continues?: number;
    supervisor_rescue?: boolean;
    auto_report?: boolean;
  };
  supervisor: {
    l15_sentinel?: boolean;
    l2_policy: L2Policy;
    adaptive_l2?: boolean;
    audit_every_n_runs?: number;
    sample_rate: number;
    l3_enabled: boolean;
    tier0_heal?: boolean;
    max_repair_attempts: number;
    autonomous_fallback?: boolean;
    unattended?: boolean;
    auto_rollback: boolean;
    rollback_after_failures: number;
  };
  safety: { blocked_patterns: string[]; confirm_side_effects: boolean };
  author?: { turn_limit?: number; verify_replay?: boolean };
  planner?: {
    enabled?: boolean;
    turn_limit?: number;
    max_batch_items?: number;
    tab_limit?: number;
    auto_resume?: boolean;
  };
};

export type UpdateStateKind =
  | 'idle'
  | 'checking'
  | 'available'
  | 'up_to_date'
  | 'downloading'
  | 'verifying'
  | 'installing'
  | 'restarting'
  | 'done'
  | 'error';

export type UpdateStatus = {
  current_version: string;
  latest_version: string;
  tag_name: string;
  has_update: boolean;
  status: UpdateStateKind;
  install_mode: string;
  can_self_update: boolean;
  repo: string;
  release_url: string;
  release_notes?: string;
  published_at?: string;
  checked_at?: string;
  asset_name?: string;
  asset_url?: string;
  asset_size?: number;
  downloaded_bytes?: number;
  progress_pct?: number;
  message?: string;
  error?: string;
};

export type EventType =
  | 'update_status'
  | 'chat_message'
  | 'chat_status'
  | 'worker_updated'
  | 'worker_observation'
  | 'task_status'
  | 'plan_updated'
  | 'batch_progress'
  | 'run_status'
  | 'step_start'
  | 'step_end'
  | 'check'
  | 'repair_start'
  | 'repair_log'
  | 'repair_end'
  | 'agent_log'
  | 'ticket_open'
  | 'ticket_resolved'
  | 'version_created'
  | 'schedule_run'
  | 'schedule_tripped'
  | 'log';

export type CuaEvent = {
  seq: number;
  ts: string;
  type: EventType;
  run_id?: string;
  task_id?: string;
  flow_id?: string;
  step_id?: string;
  data: Record<string, any>;
};

// ---------------------------------------------------------------- 鉴权

export const TOKEN_KEY = 'cuaflow_token';

/** 从 URL 中一次性读取 `?token=`，持久化保存后将其从地址栏移除。 */
export function initToken(): void {
  try {
    const url = new URL(window.location.href);
    const t = url.searchParams.get('token');
    if (t) {
      localStorage.setItem(TOKEN_KEY, t);
      url.searchParams.delete('token');
      const qs = url.searchParams.toString();
      window.history.replaceState(window.history.state, '', url.pathname + (qs ? `?${qs}` : '') + url.hash);
    }
  } catch {
    /* 忽略异常 */
  }
}

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || '';
  } catch {
    return '';
  }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

type UnauthorizedListener = () => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();
export function onUnauthorized(fn: UnauthorizedListener): () => void {
  unauthorizedListeners.add(fn);
  return () => unauthorizedListeners.delete(fn);
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  if (res.status === 401) {
    unauthorizedListeners.forEach((fn) => fn());
    throw new ApiError(401, '未授权：访问令牌缺失或无效');
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j && (j.detail || j.error)) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j.error);
    } catch {
      /* 忽略异常 */
    }
    throw new ApiError(res.status, msg);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function qs(params: Record<string, string | number | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : '';
}

const enc = encodeURIComponent;

/** 生成运行产物（如截图等）的访问地址；由于 <img> 无法自定义请求头，令牌通过查询参数传递。 */
export function artifactUrl(runId: string, path: string): string {
  const clean = path.replace(/^\/+/, '').split('/').map(enc).join('/');
  return `/api/runs/${enc(runId)}/artifacts/${clean}?token=${enc(getToken())}`;
}

export function wsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/ws/events?token=${enc(getToken())}`;
}

export const api = {
  health: () => request<{ ok: boolean; version: string; platform: string }>('GET', '/api/health'),
  stats: () => request<GlobalStats>('GET', '/api/stats'),

  updateStatus: (auto = false) =>
    request<UpdateStatus>('GET', `/api/update/status${qs({ auto: auto ? 1 : undefined })}`),
  checkUpdate: () => request<UpdateStatus>('POST', '/api/update/check'),
  applyUpdate: (wait = false) => request<UpdateStatus>('POST', `/api/update/apply${qs({ wait: wait ? 1 : undefined })}`),

  listChats: (limit = 80) => request<ChatSession[]>('GET', `/api/chats${qs({ limit })}`),
  createChat: (title = '新对话') => request<ChatSession>('POST', '/api/chats', { title }),
  getChat: (id: string) => request<ChatSessionDetail>('GET', `/api/chats/${enc(id)}`),
  deleteChat: (id: string) => request<{ ok: boolean }>('DELETE', `/api/chats/${enc(id)}`),
  sendChatMessage: (id: string, content: string, wait = false) =>
    request<{ session: ChatSession; message: ChatMessage; reply?: ChatMessage }>(
      'POST',
      `/api/chats/${enc(id)}/messages`,
      { content, wait },
    ),

  listWorkers: (opts: { session_id?: string; active_only?: boolean; limit?: number } = {}) =>
    request<Worker[]>(
      'GET',
      `/api/workers${qs({
        session_id: opts.session_id,
        active_only: opts.active_only ? 1 : undefined,
        limit: opts.limit,
      })}`,
    ),
  dispatchWorkers: (body: {
    session_id?: string;
    tasks: Array<{
      name: string;
      agent_kind: AgentKind;
      instruction: string;
      start_url?: string;
      flow_id?: string;
      params?: Record<string, any>;
    }>;
  }) => request<Worker[]>('POST', '/api/workers/dispatch', body),
  getWorker: (id: string) => request<Worker>('GET', `/api/workers/${enc(id)}`),
  steerWorker: (id: string, instruction: string) =>
    request<Worker>('POST', `/api/workers/${enc(id)}/steer`, { instruction }),
  approveWorker: (id: string, body: { action: TicketAction; hint?: string }) =>
    request<Worker>('POST', `/api/workers/${enc(id)}/approve`, body),
  abortWorker: (id: string) => request<Worker>('POST', `/api/workers/${enc(id)}/abort`),

  listFlows: () => request<Flow[]>('GET', '/api/flows'),
  getFlow: (id: string) => request<FlowDetail>('GET', `/api/flows/${enc(id)}`),
  getVersion: (id: string, v: number) => request<Version>('GET', `/api/flows/${enc(id)}/versions/${v}`),
  runFlow: (id: string, body: { params: Record<string, any>; version?: number; from_step?: string }) =>
    request<Run>('POST', `/api/flows/${enc(id)}/run`, body),
  rollback: (id: string, version: number) => request<Flow>('POST', `/api/flows/${enc(id)}/rollback`, { version }),
  deleteFlow: (id: string) => request<{ ok: boolean }>('DELETE', `/api/flows/${enc(id)}`),

  createTask: (body: {
    instruction: string;
    start_url?: string;
    params?: Record<string, any>;
    force_new?: boolean;
    mode?: TaskMode;
  }) => request<Task>('POST', '/api/tasks', body),
  listTasks: () => request<Task[]>('GET', '/api/tasks'),
  getTask: (id: string) => request<Task>('GET', `/api/tasks/${enc(id)}`),
  resumeTask: (id: string) => request<Task>('POST', `/api/tasks/${enc(id)}/resume`),
  abortTask: (id: string) => request<Task>('POST', `/api/tasks/${enc(id)}/abort`),

  listSchedules: () => request<Schedule[]>('GET', '/api/schedules'),
  createSchedule: (body: {
    name: string;
    interval_seconds: number;
    kind: 'flow' | 'task';
    flow_id?: string;
    instruction?: string;
    start_url?: string;
    params?: Record<string, any>;
    max_runs?: number;
    max_consecutive_failures?: number;
    enabled?: boolean;
    run_immediately?: boolean;
  }) => request<Schedule>('POST', '/api/schedules', body),
  patchSchedule: (
    id: string,
    body: { enabled?: boolean; interval_seconds?: number; max_consecutive_failures?: number; reset_breaker?: boolean },
  ) => request<Schedule>('PATCH', `/api/schedules/${enc(id)}`, body),
  triggerSchedule: (id: string) => request<{ ok: boolean; schedule: Schedule }>('POST', `/api/schedules/${enc(id)}/trigger`),
  deleteSchedule: (id: string) => request<{ ok: boolean }>('DELETE', `/api/schedules/${enc(id)}`),

  listRuns: (opts: { flow_id?: string; limit?: number } = {}) => request<Run[]>('GET', `/api/runs${qs(opts)}`),
  getRun: (id: string) => request<Run>('GET', `/api/runs/${enc(id)}`),
  runEvents: (id: string, after_seq?: number) =>
    request<CuaEvent[]>('GET', `/api/runs/${enc(id)}/events${qs({ after_seq })}`),
  abortRun: (id: string) => request<Run>('POST', `/api/runs/${enc(id)}/abort`),

  listTickets: (status?: 'open' | 'resolved') => request<Ticket[]>('GET', `/api/tickets${qs({ status })}`),
  resolveTicket: (id: string, body: { action: TicketAction; hint?: string }) =>
    request<Ticket>('POST', `/api/tickets/${enc(id)}/resolve`, body),

  getSettings: () => request<Settings>('GET', '/api/settings'),
  putSettings: (s: Settings) => request<Settings>('PUT', '/api/settings', s),
};
