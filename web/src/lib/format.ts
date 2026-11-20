// 格式化辅助函数与简体中文状态标签映射。
import type { RunStatus, StepStatus, TaskStatus } from '../api';

export const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  queued: '排队中',
  running: '运行中',
  repairing: '修复中',
  waiting_human: '等待人工',
  succeeded: '成功',
  failed: '失败',
  aborted: '已终止',
};

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  queued: '排队中',
  matching: '匹配脚本',
  planning: '自主规划执行',
  authoring: '录制中',
  running: '运行中',
  succeeded: '成功',
  failed: '失败',
  aborted: '已终止',
};

export const STEP_STATUS_LABEL: Record<StepStatus, string> = {
  pending: '待执行',
  running: '执行中',
  passed: '通过',
  failed: '失败',
  repairing: '修复中',
  repaired: '已修复',
  skipped: '已跳过',
};

export const SCHEDULE_STATUS_LABEL: Record<string, string> = {
  active: '监控中',
  paused: '已暂停',
  tripped: '已熔断',
  completed: '已完成',
};

export const VERDICT_LABEL: Record<string, string> = {
  run: '直接复用',
  adapt: '改参复用',
  mission: '长任务规划',
  new: '新录制',
};

export const AUTHOR_LABEL: Record<string, string> = {
  author: '录制者',
  repairer: '修复者',
  human: '人工',
};

export const ROLE_LABEL: Record<string, string> = {
  planner: '规划者',
  author: '录制者',
  supervisor: '监督者',
  repairer: '修复者',
  fallback: '自主接管',
  vision_judge: '视觉裁判',
  system: '系统',
  agent: '智能体',
};

export type Tone = 'neutral' | 'info' | 'success' | 'danger' | 'warning' | 'muted';

export function statusTone(s: string | undefined): Tone {
  switch (s) {
    case 'succeeded':
    case 'passed':
    case 'repaired':
    case 'resolved':
    case 'active':
    case 'done':
      return 'success';
    case 'failed':
    case 'tripped':
      return 'danger';
    case 'running':
    case 'matching':
    case 'planning':
    case 'authoring':
      return 'info';
    case 'repairing':
    case 'waiting_human':
    case 'open':
      return 'warning';
    case 'aborted':
    case 'skipped':
    case 'paused':
    case 'completed':
      return 'muted';
    default:
      return 'neutral';
  }
}

export const TERMINAL_RUN: ReadonlySet<string> = new Set(['succeeded', 'failed', 'aborted']);

export function fmtTokens(n?: number): string {
  if (n === undefined || n === null || isNaN(n)) return '0';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(Math.round(n));
}

export function fmtTime(iso?: string): string {
  if (!iso) return '–';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const pad = (n: number) => String(n).padStart(2, '0');
  const hms = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return sameDay ? hms : `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hms}`;
}

export function fmtDuration(ms?: number): string {
  if (ms === undefined || ms === null || isNaN(ms)) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rs = Math.round(s % 60);
  if (m < 60) return `${m}m${rs}s`;
  return `${Math.floor(m / 60)}h${m % 60}m`;
}

export function durationBetween(a?: string, b?: string): number | undefined {
  if (!a) return undefined;
  const t0 = Date.parse(a);
  const t1 = b ? Date.parse(b) : Date.now();
  if (isNaN(t0) || isNaN(t1)) return undefined;
  return Math.max(0, t1 - t0);
}

export function pct(num: number, den: number): string {
  if (!den) return '–';
  return `${Math.round((num / den) * 100)}%`;
}

export function shortJson(v: unknown, max = 80): string {
  if (v === undefined || v === null) return '';
  let s: string;
  try {
    s = JSON.stringify(v);
  } catch {
    s = String(v);
  }
  return s.length > max ? s.slice(0, max - 1) + '…' : s;
}
