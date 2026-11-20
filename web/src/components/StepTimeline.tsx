import type { StepRecord, StepStatus } from '../api';
import { STEP_STATUS_LABEL, durationBetween, fmtDuration } from '../lib/format';

export function StepIcon({ status }: { status: StepStatus }) {
  switch (status) {
    case 'pending':
      return <span className="step-icon pending">○</span>;
    case 'running':
      return (
        <span className="step-icon">
          <span className="spinner" />
        </span>
      );
    case 'passed':
      return <span className="step-icon">✅</span>;
    case 'failed':
      return <span className="step-icon">❌</span>;
    case 'repairing':
      return <span className="step-icon wobble">🔧</span>;
    case 'repaired':
      return <span className="step-icon">🩹</span>;
    case 'skipped':
      return <span className="step-icon">⏭</span>;
    default:
      return <span className="step-icon">?</span>;
  }
}

export function StepTimeline({
  steps,
  selected,
  onSelect,
}: {
  steps: StepRecord[];
  selected?: string;
  onSelect: (stepId: string) => void;
}) {
  if (!steps.length) return <div className="muted small pad">尚无步骤</div>;
  return (
    <ol className="timeline">
      {steps.map((s) => {
        const live = s.status === 'running' || s.status === 'repairing';
        const dur = s.duration_ms ?? (live ? durationBetween(s.started_at) : durationBetween(s.started_at, s.ended_at));
        const failedChecks = s.checks.filter((c) => !c.passed).length;
        return (
          <li
            key={s.step_id}
            className={`tl-item tl-${s.status} ${selected === s.step_id ? 'selected' : ''}`}
            onClick={() => onSelect(s.step_id)}
            title={STEP_STATUS_LABEL[s.status]}
          >
            <StepIcon status={s.status} />
            <div className="tl-body">
              <div className="tl-title">
                <span className="tl-index">{s.index + 1}.</span> {s.title || s.step_id}
              </div>
              <div className="tl-meta">
                <code>{s.step_id}</code>
                {dur !== undefined && <span>{fmtDuration(dur)}</span>}
                {s.attempts > 1 && <span className="warn-text">尝试 ×{s.attempts}</span>}
                {s.repairs.length > 0 && <span>修复 ×{s.repairs.length}</span>}
                {failedChecks > 0 && <span className="danger-text">检查失败 {failedChecks}</span>}
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
