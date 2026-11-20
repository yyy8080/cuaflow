// @vitest-environment jsdom
// 冒烟测试：使用模拟 API 与虚拟 WebSocket 渲染各个页面。
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
  if (p === '/api/stats') {
    return {
      flows: 1,
      auto_repairs: 1,
      flow_runs: 4,
      flow_successes: 3,
      flow_success_rate: 0.75,
      tasks: 1,
      tasks_succeeded: 1,
      llm_calls: 5,
      total_tokens: 2400,
      script_steps: 12,
      estimated_tokens_saved: 21600,
      savings_ratio: 0.9,
      schedules_active: 1,
      schedules_tripped: 0,
    };
  }
  if (p === '/api/schedules') {
    return [
      {
        id: 'sched1',
        name: '每5分钟报表巡检',
        kind: 'flow',
        flow_id: 'export_report',
        params: {},
        interval_seconds: 300,
        enabled: true,
        max_consecutive_failures: 3,
        runs_count: 5,
        success_count: 5,
        consecutive_failures: 0,
        status: 'active',
        created_at: '2024-01-03T00:00:00Z',
        updated_at: '2024-01-03T00:00:00Z',
      },
    ];
  }
  if (p === '/api/chats') {
    return [
      {
        id: 'chat1',
        title: '双站点并行比价调研',
        status: 'idle',
        created_at: '2024-01-03T00:00:00Z',
        updated_at: '2024-01-03T00:00:05Z',
      },
    ];
  }
  if (p === '/api/chats/chat1') {
    return {
      id: 'chat1',
      title: '双站点并行比价调研',
      status: 'idle',
      created_at: '2024-01-03T00:00:00Z',
      updated_at: '2024-01-03T00:00:05Z',
      messages: [
        {
          id: 'm1',
          session_id: 'chat1',
          role: 'user',
          sender: 'user',
          content: '请同时派发两个浏览器探索子智能体查询价格',
          created_at: '2024-01-03T00:00:01Z',
        },
        {
          id: 'm2',
          session_id: 'chat1',
          role: 'assistant',
          sender: 'coordinator',
          content: '已为您并行启动两个直连浏览器子智能体。',
          meta: {
            dispatched_workers: [
              { id: 'w1', name: '京东行情抓取', agent_kind: 'explorer' },
              { id: 'w2', name: '提交确认前等待', agent_kind: 'explorer' },
            ],
          },
          created_at: '2024-01-03T00:00:02Z',
        },
      ],
      workers: [
        {
          id: 'w1',
          session_id: 'chat1',
          name: '京东行情抓取',
          agent_kind: 'explorer',
          instruction: '查询笔记本报价',
          params: {},
          run_id: 'run42',
          status: 'running',
          latest_note: '正在读取列表首屏报价',
          latest_screenshot: 'shots/open.png',
          created_at: '2024-01-03T00:00:02Z',
          updated_at: '2024-01-03T00:00:04Z',
        },
        {
          id: 'w2',
          session_id: 'chat1',
          name: '提交确认前等待',
          agent_kind: 'explorer',
          instruction: '准备提交预约单',
          params: {},
          run_id: 'run42',
          status: 'waiting_approval',
          latest_note: '⏸ 等待放行：准备点击确认提交按钮',
          created_at: '2024-01-03T00:00:02Z',
          updated_at: '2024-01-03T00:00:05Z',
        },
      ],
    };
  }
  if (p === '/api/workers') {
    return [
      {
        id: 'w1',
        session_id: 'chat1',
        name: '京东行情抓取',
        agent_kind: 'explorer',
        instruction: '查询笔记本报价',
        params: {},
        run_id: 'run42',
        status: 'running',
        latest_note: '正在读取列表首屏报价',
        created_at: '2024-01-03T00:00:02Z',
        updated_at: '2024-01-03T00:00:04Z',
      },
    ];
  }
  if (p === '/api/update/status') {
    return {
      current_version: '0.2.2',
      latest_version: '0.3.0',
      tag_name: 'v0.3.0',
      has_update: true,
      status: 'available',
      install_mode: 'windows_installer',
      can_self_update: true,
      repo: 'yyy8080/cuaflow',
      release_url: 'https://github.com/yyy8080/cuaflow/releases/tag/v0.3.0',
      release_notes: '新增一键在线更新能力',
      checked_at: '2024-01-03T00:00:05Z',
      asset_name: 'cuaflow-setup-0.3.0-win64.exe',
      asset_size: 98 * 1024 * 1024,
      message: '发现新版本 v0.3.0',
    };
  }
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
  // 等待异步请求与定时器稳定
  for (let i = 0; i < 5; i++) await act(async () => new Promise((r) => setTimeout(r, 10)));
  return container.textContent || '';
}

describe('pages render with mocked API', () => {
  it('chat studio home page renders coordinator chat and parallel subagent fleet', async () => {
    const t = await render('/');
    expect(t).toContain('双站点并行比价调研');
    expect(t).toContain('京东行情抓取');
    expect(t).toContain('立即放行');
    expect(t).toContain('21.6k');
    const shot = container.querySelector('.worker-shot') as HTMLImageElement;
    expect(shot.getAttribute('src')).toBe('/api/runs/run42/artifacts/shots/open.png?token=tok');
  });

  it('tasks page', async () => {
    const t = await render('/tasks');
    expect(t).toContain('导出十月报表');
    expect(t).toContain('直接复用');
    expect(t).toContain('21.6k');
  });

  it('schedules page', async () => {
    const t = await render('/schedules');
    expect(t).toContain('每5分钟报表巡检');
    expect(t).toContain('监控中');
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

    // 选中第一个步骤 -> 验证截图地址携带访问令牌
    const first = container.querySelector('.tl-item') as HTMLElement;
    await act(async () => first.click());
    const img = container.querySelector('.run-detail img.shot') as HTMLImageElement;
    expect(img.getAttribute('src')).toBe('/api/runs/run42/artifacts/shots/open.png?token=tok');

    // 实时事件更新页面状态
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
