import { useEffect, useMemo, useRef, useState } from 'react';
import type { CuaEvent } from '../api';
import { ROLE_LABEL, fmtTime } from '../lib/format';
import { eventRole } from '../lib/runState';

const ROLES = ['all', 'planner', 'author', 'supervisor', 'repairer', 'fallback', 'system'] as const;
type RoleFilter = (typeof ROLES)[number];

const TYPE_LABEL: Record<string, string> = {
  task_status: '任务',
  plan_updated: '规划更新',
  batch_progress: '批量进度',
  run_status: '运行',
  step_start: '步骤开始',
  step_end: '步骤结束',
  check: '检查',
  repair_start: '修复开始',
  repair_log: '修复',
  repair_end: '修复结束',
  agent_log: '日志',
  ticket_open: '工单',
  ticket_resolved: '工单处理',
  version_created: '新版本',
  schedule_run: '定时执行',
  schedule_tripped: '监控熔断',
  log: '日志',
};

function describe(ev: CuaEvent): string {
  const d = ev.data || {};
  switch (ev.type) {
    case 'agent_log':
      return d.text ?? '';
    case 'repair_log':
      return d.text ?? '';
    case 'log':
      return d.text ?? '';
    case 'plan_updated': {
      const phases = Array.isArray(d.phases) ? d.phases : [];
      return phases.map((p: any) => `${p.title}[${p.status ?? 'pending'}]`).join(' → ') || '规划已更新';
    }
    case 'batch_progress':
      return `[${d.batch_id ?? 'batch'}] ${d.completed}/${d.total} (${d.last_status}) · 脚本 ${d.flow_id ?? ''}`;
    case 'schedule_run':
      return `监控 ${d.schedule_id} 执行${d.succeeded ? '成功' : '失败'}`;
    case 'schedule_tripped':
      return `监控 ${d.name ?? d.schedule_id} 连续失败 ${d.consecutive_failures} 次，已自动熔断暂停`;
    case 'check':
      return `${d.level} ${d.passed ? '通过' : '失败'}${d.detail ? ' — ' + d.detail : ''}`;
    case 'step_start':
      return `#${(d.index ?? 0) + 1} ${d.title ?? ev.step_id ?? ''}`;
    case 'step_end':
      return `${d.title ?? ev.step_id ?? ''} → ${d.status ?? ''}${d.error ? ' — ' + d.error : ''}`;
    case 'run_status':
      return `${d.status ?? ''}${d.error ? ' — ' + d.error : ''}`;
    case 'task_status':
      return `${d.status ?? ''}${d.match?.verdict ? ' (' + d.match.verdict + ')' : ''}`;
    case 'repair_start':
      return `第 ${d.attempt ?? '?'} 次修复`;
    case 'repair_end':
      return `第 ${d.attempt ?? '?'} 次修复 ${d.status ?? ''}${d.summary ? ' — ' + d.summary : ''}`;
    case 'ticket_open':
      return `需要人工介入：${d.reason ?? ''}`;
    case 'ticket_resolved':
      return `工单已处理：${d.resolution?.action ?? ''}`;
    case 'version_created':
      return `v${d.version ?? '?'} ${d.reason ?? ''}`;
    default:
      return JSON.stringify(d);
  }
}

function eventClass(ev: CuaEvent): string {
  const d = ev.data || {};
  if (ev.type === 'check') return d.passed ? 'ev-ok' : 'ev-bad';
  if (ev.type === 'log' && (d.level === 'error' || d.level === 'ERROR')) return 'ev-bad';
  if (ev.type === 'log' && (d.level === 'warning' || d.level === 'WARNING')) return 'ev-warn';
  if (ev.type === 'step_end') return d.status === 'failed' ? 'ev-bad' : d.status === 'passed' || d.status === 'repaired' ? 'ev-ok' : '';
  if (ev.type === 'repair_end') return d.status === 'succeeded' ? 'ev-ok' : d.status === 'failed' ? 'ev-bad' : '';
  if (ev.type === 'ticket_open') return 'ev-warn';
  if (ev.type === 'version_created') return 'ev-ok';
  if (ev.type === 'agent_log') return `ev-kind-${d.kind ?? 'thought'}`;
  return '';
}

/** 支持按角色筛选与贴底自动滚动的实时事件流组件。 */
export function EventStream({ events, onSelectStep }: { events: CuaEvent[]; onSelectStep?: (stepId: string) => void }) {
  const [role, setRole] = useState<RoleFilter>('all');
  const [follow, setFollow] = useState(true);
  const box = useRef<HTMLDivElement>(null);

  const shown = useMemo(() => {
    const list = role === 'all' ? events : events.filter((e) => eventRole(e) === role);
    return list.length > 1500 ? list.slice(list.length - 1500) : list;
  }, [events, role]);

  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [shown, follow]);

  const onScroll = () => {
    const el = box.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (atBottom !== follow) setFollow(atBottom);
  };

  return (
    <div className="events">
      <div className="events-toolbar">
        <div className="seg">
          {ROLES.map((r) => (
            <button key={r} className={role === r ? 'active' : ''} onClick={() => setRole(r)}>
              {r === 'all' ? '全部' : ROLE_LABEL[r]}
            </button>
          ))}
        </div>
        {!follow && (
          <button className="btn btn-sm" onClick={() => setFollow(true)}>
            ↓ 跟随
          </button>
        )}
      </div>
      <div className="events-list" ref={box} onScroll={onScroll}>
        {!shown.length && <div className="muted small pad">暂无事件</div>}
        {shown.map((ev) => {
          const r = eventRole(ev);
          return (
            <div key={ev.seq} className={`ev ${eventClass(ev)}`}>
              <div className="ev-head">
                <span className="ev-time">{fmtTime(ev.ts)}</span>
                <span className={`ev-role role-${r}`}>{ROLE_LABEL[r] ?? r}</span>
                <span className="ev-type">
                  {TYPE_LABEL[ev.type] ?? ev.type}
                  {ev.type === 'agent_log' && ev.data?.kind ? ` · ${ev.data.kind}` : ''}
                </span>
                {ev.step_id && (
                  <button className="link ev-step" onClick={() => onSelectStep?.(ev.step_id!)}>
                    {ev.step_id}
                  </button>
                )}
              </div>
              <div className="ev-text">{describe(ev)}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
