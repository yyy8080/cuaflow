import type { ReactNode } from 'react';
import {
  RUN_STATUS_LABEL,
  SCHEDULE_STATUS_LABEL,
  STEP_STATUS_LABEL,
  TASK_STATUS_LABEL,
  statusTone,
  type Tone,
} from '../lib/format';

const ALL_LABELS: Record<string, string> = {
  ...TASK_STATUS_LABEL,
  ...STEP_STATUS_LABEL,
  ...RUN_STATUS_LABEL,
  ...SCHEDULE_STATUS_LABEL,
  done: '已完成',
  open: '待处理',
  resolved: '已处理',
};

export function Badge({ tone = 'neutral', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

/** Coloured badge for run / task / step / schedule / ticket statuses. */
export function StatusBadge({ status, pulse }: { status?: string; pulse?: boolean }) {
  if (!status) return <Badge tone="muted">–</Badge>;
  const tone = statusTone(status);
  const live =
    pulse ?? ['running', 'repairing', 'matching', 'planning', 'authoring', 'waiting_human', 'active'].includes(status);
  return (
    <span className={`badge badge-${tone}`} title={status}>
      {live && <span className="dot-pulse" />}
      {ALL_LABELS[status] ?? status}
    </span>
  );
}
