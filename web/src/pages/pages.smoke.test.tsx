// @vitest-environment jsdom
// Smoke tests: render each page against a mocked API and fake WebSocket.
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import type { CuaEvent, FlowDetail, Run, Settings, Task, Ticket } from '../api';

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const flow: FlowDetail = {
  id: 'export_report',
  name: '导出报表',
  description: '从后台导出月度报表',
  intent_template: 'Export the {month} report to {save_path}',
  params_schema: { month: { type: 'str', required: true }, retries: { type: 'int', default: 2 }, headless: { type: 'bool' } },
  output_schema: {},
  side_effects: false,
  current_version: 2,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-02T00:00:00Z',
  tags: ['edge'],
  stats: { runs: 4, successes: 3, repairs: 1, last_status: 'succeeded' },
  versions: [
    { flow_id: 'export_report', version: 1, created_at: '2024-01-01T00:00:00Z', author: 'author', reason: '初始录制', steps: [{ id: 'open', title: '打开后台' }], health: { runs: 2, successes: 1 } },
    { flow_id: 'export_report', version: 2, parent_version: 1, created_at: '2024-01-02T00:00:00Z', author: 'repairer', reason: '修复导出按钮选择器', repaired_step: 'export', steps: [{ id: 'open', title: '打开后台' }, { id: 'export', title: '点击导出' }], health: { runs: 2, successes: 2 } },
  ],
};

const run: Run = {
  id: 'run42',
  kind: 'flow',
  flow_id: 'export_report',
  version: 2,
  params: { month: '2024-10' },
  status: 'running',
  created_at: '2024-01-03T00:00:00Z',
  started_at: '2024-01-03T00:00:01Z',
  steps: [
    { step_id: 'open', index: 0, title: '打开后台', status: 'passed', attempts: 1, duration_ms: 900, screenshot: 'shots/open.png', checks: [{ level: 'L1', passed: true, detail: 'URL 匹配', ts: '2024-01-03T00:00:02Z' }], repairs: [] },
    { step_id: 'export', index: 1, title: '点击导出', status: 'repairing', attempts: 2, checks: [], repairs: [{ attempt: 1, started_at: '2024-01-03T00:00:03Z', status: 'running' }], error: '元素未找到' },
  ],
};

const events: CuaEvent[] = [
  { seq: 1, ts: '2024-01-03T00:00:01Z', type: 'run_status', run_id: 'run42', data: { status: 'running' } },
  { seq: 2, ts: '2024-01-03T00:00:03Z', type: 'agent_log', run_id: 'run42', step_id: 'export', data: { role: 'repairer', kind: 'thought', text: '按钮可能移到了菜单里' } },
];

const task: Task = { id: 't1', instruction: '导出十月报表', params: {}, status: 'running', match: { verdict: 'run', flow_id: 'export_report' }, run_id: 'run42', created_at: '2024-01-03T00:00:00Z', updated_at: '2024-01-03T00:00:00Z' };
const ticket: Ticket = { id: 'k1', run_id: 'run42', step_id: 'export', reason: '需要登录验证码', status: 'open', created_at: '2024-01-03T00:00:05Z' };
const settings: Settings = {
  models: { default: { base_url: 'https://api.example.com/v1', model: 'gpt-x', api_key_env: 'OPENAI_API_KEY', vision: true }, roles: { repairer: { base_url: '', model: 'o-x', api_key_env: 'K' } } },
  supervisor: { l2_policy: 'on_change', sample_rate: 0.2, l3_enabled: true, max_repair_attempts: 2, auto_rollback: true, rollback_after_failures: 3 },
  safety: { blocked_patterns: ['rm -rf'], confirm_side_effects: true },
};

function route(url: string): unknown {
  const u = new URL(url, 'http://localhost');
  const p = u.pathname;
  if (p === '/api/flows') return [flow];
  if (p === '/api/flows/export_report') return flow;
  if (p.startsWith('/api/flows/export_report/versions/')) {
    const v = Number(p.split('/').pop());
    return { ...flow.versions.find((x) => x.version === v), source: 'def open(ctx):\n    pass\n', diff: '@@ -1 +1 @@\n-a\n+b\n' };
  }
  if (p === '/api/runs') return [run];
  if (p === '/api/runs/run42') return run;
  if (p === '/api/runs/run42/events') return events;
  if (p === '/api/tasks') return [task];
  if (p === '/api/tickets') return u.searchParams.get('status') === 'open' ? [ticket] : [];
  if (p === '/api/settings') return settings;
  return null;
}

class FakeWS {
  static instances: FakeWS[] = [];
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((m: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeWS.instances.push(this);
    setTimeout(() => this.onopen?.(), 0);
  }
  send(s: string) {
    this.sent.push(s);
  }
  close() {}
}

let root: Root;
let container: HTMLDivElement;
const fetchMock = vi.fn(async (url: string) => {
  const body = route(url);
  return { ok: body !== null, status: body !== null ? 200 : 404, statusText: '', json: async () => body } as Response;
});

beforeEach(() => {
  localStorage.setItem('cuaflow_token', 'tok');
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('WebSocket', FakeWS);
  FakeWS.instances = [];
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function render(path: string) {
  await act(async () => {
    root.render(
      <MemoryRouter key={path} initialEntries={[path]}>
        <App />
      </MemoryRouter>,
    );
  });
  // let fetches / timers settle
  for (let i = 0; i < 5; i++) await act(async () => new Promise((r) => setTimeout(r, 10)));
  return container.textContent || '';
}

describe('pages render with mocked API', () => {
  it('tasks page', async () => {
    const t = await render('/');
    expect(t).toContain('导出十月报表');
    expect(t).toContain('直接复用');
    expect(container.querySelector('.nav-count')?.textContent).toBe('1');
  });

  it('flows list and detail', async () => {
    expect(await render('/flows')).toContain('Export the {month} report');
    const t = await render('/flows/export_report');
    expect(t).toContain('修复导出按钮选择器');
    expect(t).toContain('版本 v2');
    expect(container.querySelector('#p-month')).not.toBeNull();
  });

  it('run monitor subscribes and renders steps/events, with token on images', async () => {
    const t = await render('/runs/run42');
    expect(t).toContain('点击导出');
    expect(t).toContain('按钮可能移到了菜单里');
    const ws = FakeWS.instances[FakeWS.instances.length - 1];
    expect(ws.url).toContain('/ws/events?token=tok');
    expect(ws.sent.map((s) => JSON.parse(s))).toContainEqual({ subscribe: { run_id: 'run42' } });

    // select the first step -> screenshot URL carries the token
    const first = container.querySelector('.tl-item') as HTMLElement;
    await act(async () => first.click());
    const img = container.querySelector('.run-detail img.shot') as HTMLImageElement;
    expect(img.getAttribute('src')).toBe('/api/runs/run42/artifacts/shots/open.png?token=tok');

    // live event updates state
    await act(async () => {
      ws.onmessage?.({ data: JSON.stringify({ seq: 3, ts: '2024-01-03T00:00:09Z', type: 'run_status', run_id: 'run42', data: { status: 'failed', error: '超时' } }) });
    });
    expect(container.querySelector('.run-top')?.textContent).toContain('超时');
  });

  it('tickets and settings', async () => {
    expect(await render('/tickets')).toContain('需要登录验证码');
    const t = await render('/settings');
    expect(t).toContain('角色模型覆盖');
    expect((container.querySelector('textarea.mono') as HTMLTextAreaElement).value).toBe('rm -rf');
  });

  it('shows the auth screen on 401', async () => {
    fetchMock.mockImplementation(async () => ({ ok: false, status: 401, statusText: '', json: async () => ({}) }) as Response);
    const t = await render('/flows');
    expect(t).toContain('需要访问令牌');
  });
});
