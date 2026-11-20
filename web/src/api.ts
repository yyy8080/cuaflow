// Typed client for the cuaflow HTTP API. Types mirror docs/api.md (authoritative).

export type ParamType = 'str' | 'int' | 'float' | 'bool';
export type ParamSpec = { type: ParamType; description?: string; default?: any; required?: boolean };

export type StepSpec = { id: string; title: string; expect?: string; side_effect?: boolean; critical?: boolean };

export type RunStatus = 'queued' | 'running' | 'repairing' | 'waiting_human' | 'succeeded' | 'failed' | 'aborted';
export type StepStatus = 'pending' | 'running' | 'passed' | 'failed' | 'repairing' | 'repaired' | 'skipped';

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
  stats: { runs: number; successes: number; repairs: number; last_run_at?: string; last_status?: RunStatus };
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

export type Check = { level: 'L0' | 'L1' | 'L2' | 'L3'; passed: boolean; detail: string; ts: string };

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
  kind: 'flow' | 'author';
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
};

export type TaskStatus = 'queued' | 'matching' | 'authoring' | 'running' | 'succeeded' | 'failed' | 'aborted';
export type Task = {
  id: string;
  instruction: string;
  start_url?: string;
  params: Record<string, any>;
  status: TaskStatus;
  match?: { verdict: 'run' | 'adapt' | 'new'; flow_id?: string; params?: Record<string, any>; reason?: string };
  run_id?: string;
  flow_id?: string;
  final_response?: string;
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

export type ModelSpec = { base_url: string; model: string; api_key_env: string; vision?: boolean; max_output_tokens?: number };
export type ModelRole = 'author' | 'supervisor' | 'repairer' | 'vision_judge';
export const MODEL_ROLES: ModelRole[] = ['author', 'supervisor', 'repairer', 'vision_judge'];

export type L2Policy = 'always' | 'on_change' | 'sample' | 'off';
export type Settings = {
  models: { default: ModelSpec; roles: Partial<Record<ModelRole, ModelSpec>> };
  supervisor: {
    l2_policy: L2Policy;
    sample_rate: number;
    l3_enabled: boolean;
    max_repair_attempts: number;
    auto_rollback: boolean;
    rollback_after_failures: number;
  };
  safety: { blocked_patterns: string[]; confirm_side_effects: boolean };
};

export type EventType =
  | 'task_status'
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

// ---------------------------------------------------------------- auth

export const TOKEN_KEY = 'cuaflow_token';

/** Read `?token=` from the URL once, persist it, and strip it from the address bar. */
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
    /* ignore */
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
      /* ignore */
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

/** URL for an artifact (screenshots etc.); token goes in the query because <img> cannot set headers. */
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

  listFlows: () => request<Flow[]>('GET', '/api/flows'),
  getFlow: (id: string) => request<FlowDetail>('GET', `/api/flows/${enc(id)}`),
  getVersion: (id: string, v: number) => request<Version>('GET', `/api/flows/${enc(id)}/versions/${v}`),
  runFlow: (id: string, body: { params: Record<string, any>; version?: number; from_step?: string }) =>
    request<Run>('POST', `/api/flows/${enc(id)}/run`, body),
  rollback: (id: string, version: number) => request<Flow>('POST', `/api/flows/${enc(id)}/rollback`, { version }),
  deleteFlow: (id: string) => request<{ ok: boolean }>('DELETE', `/api/flows/${enc(id)}`),

  createTask: (body: { instruction: string; start_url?: string; params?: Record<string, any>; force_new?: boolean }) =>
    request<Task>('POST', '/api/tasks', body),
  listTasks: () => request<Task[]>('GET', '/api/tasks'),
  getTask: (id: string) => request<Task>('GET', `/api/tasks/${enc(id)}`),

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
