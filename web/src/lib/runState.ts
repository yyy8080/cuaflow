// Pure reducer that folds live events into a Run snapshot (used by the run monitor page).
import type { CuaEvent, EventType, Repair, Run, StepRecord } from '../api';

export type RunState = {
  run: Run | null;
  events: CuaEvent[]; // sorted by seq, de-duplicated, capped
  lastSeq: number;
};

export type RunAction =
  | { type: 'reset' }
  | { type: 'snapshot'; run: Run }
  | { type: 'events'; events: CuaEvent[] };

export const MAX_EVENTS = 5000;

export const initialRunState: RunState = { run: null, events: [], lastSeq: 0 };

/** Event types after which the page should refetch GET /api/runs/{id} (debounced). */
export const REFETCH_TYPES: ReadonlySet<EventType> = new Set<EventType>([
  'step_end',
  'run_status',
  'repair_end',
  'version_created',
  'plan_updated',
]);

export function runReducer(state: RunState, action: RunAction): RunState {
  switch (action.type) {
    case 'reset':
      return initialRunState;
    case 'snapshot':
      return { ...state, run: action.run };
    case 'events': {
      const runId = state.run?.id;
      const seen = new Set(state.events.map((e) => e.seq));
      const fresh = action.events
        .filter((e) => !seen.has(e.seq) && (!runId || !e.run_id || e.run_id === runId))
        .sort((a, b) => a.seq - b.seq);
      if (!fresh.length) return state;
      let run = state.run;
      for (const ev of fresh) run = applyEvent(run, ev);
      let events = mergeSorted(state.events, fresh);
      if (events.length > MAX_EVENTS) events = events.slice(events.length - MAX_EVENTS);
      const lastSeq = Math.max(state.lastSeq, fresh[fresh.length - 1].seq);
      return { run, events, lastSeq };
    }
    default:
      return state;
  }
}

function mergeSorted(a: CuaEvent[], b: CuaEvent[]): CuaEvent[] {
  if (!a.length) return b;
  if (a[a.length - 1].seq < b[0].seq) return a.concat(b);
  return a.concat(b).sort((x, y) => x.seq - y.seq);
}

function emptyStep(stepId: string, index: number, title: string): StepRecord {
  return { step_id: stepId, index, title, status: 'pending', attempts: 0, checks: [], repairs: [] };
}

function updateStep(run: Run, stepId: string, fn: (s: StepRecord) => StepRecord, create?: () => StepRecord): Run {
  const idx = run.steps.findIndex((s) => s.step_id === stepId);
  if (idx === -1) {
    if (!create) return run;
    const steps = [...run.steps, fn(create())].sort((a, b) => a.index - b.index);
    return { ...run, steps };
  }
  const steps = run.steps.slice();
  steps[idx] = fn(steps[idx]);
  return { ...run, steps };
}

/** Apply a single event to a run. Unknown / irrelevant events leave the run unchanged. */
export function applyEvent(run: Run | null, ev: CuaEvent): Run | null {
  if (!run) return run;
  const d = ev.data || {};
  const sid = ev.step_id ?? d.step_id;
  switch (ev.type) {
    case 'run_status': {
      const next: Run = { ...run, status: d.status ?? run.status };
      if (d.error !== undefined) next.error = d.error;
      if (['succeeded', 'failed', 'aborted'].includes(next.status) && !next.finished_at) next.finished_at = ev.ts;
      if (next.status === 'running' && !next.started_at) next.started_at = ev.ts;
      return next;
    }
    case 'step_start': {
      if (!sid) return run;
      return updateStep(
        run,
        sid,
        (s) => ({
          ...s,
          index: typeof d.index === 'number' ? d.index : s.index,
          title: d.title ?? s.title,
          status: 'running',
          // re-delivered step_start for an already-running step must not bump attempts
          attempts: s.status === 'running' ? Math.max(s.attempts || 0, 1) : (s.attempts || 0) + 1,
          started_at: s.status === 'running' && s.started_at ? s.started_at : ev.ts,
          ended_at: undefined,
          duration_ms: undefined,
          error: undefined,
          error_kind: undefined,
        }),
        () => emptyStep(sid, typeof d.index === 'number' ? d.index : run.steps.length, d.title ?? sid),
      );
    }
    case 'step_end': {
      const rec = d as StepRecord;
      const id = rec.step_id ?? sid;
      if (!id) return run;
      return updateStep(
        run,
        id,
        (s) => ({ ...s, ...rec, step_id: id, checks: rec.checks ?? s.checks, repairs: rec.repairs ?? s.repairs }),
        () => emptyStep(id, typeof rec.index === 'number' ? rec.index : run.steps.length, rec.title ?? id),
      );
    }
    case 'check': {
      const check = { level: d.level, passed: !!d.passed, detail: d.detail ?? '', ts: d.ts ?? ev.ts };
      if (!sid) {
        if (check.level === 'L3') return { ...run, final_verdict: { passed: check.passed, detail: check.detail } };
        return run;
      }
      return updateStep(run, sid, (s) =>
        s.checks.some((c) => c.level === check.level && c.ts === check.ts && c.detail === check.detail)
          ? s
          : { ...s, checks: [...s.checks, check] },
      );
    }
    case 'repair_start': {
      if (!sid) return run;
      const attempt = typeof d.attempt === 'number' ? d.attempt : 1;
      return updateStep(run, sid, (s) => {
        if (s.repairs.some((r) => r.attempt === attempt)) return s; // already known (re-delivered)
        const repairs = [...s.repairs, { attempt, started_at: ev.ts, status: 'running' as const }];
        repairs.sort((a, b) => a.attempt - b.attempt);
        return { ...s, status: 'repairing', repairs };
      });
    }
    case 'repair_end': {
      const rep = d as Repair;
      let next = run;
      if (sid) {
        next = updateStep(next, sid, (s) => {
          const repairs = s.repairs.slice();
          const i = repairs.findIndex((r) => r.attempt === rep.attempt);
          if (i === -1) repairs.push(rep);
          else repairs[i] = { ...repairs[i], ...rep };
          repairs.sort((a, b) => a.attempt - b.attempt);
          return { ...s, repairs };
        });
      }
      if (rep.new_version !== undefined && rep.new_version !== null) next = { ...next, new_version: rep.new_version };
      return next;
    }
    case 'version_created': {
      if (typeof d.version === 'number' && (!ev.flow_id || !run.flow_id || ev.flow_id === run.flow_id)) {
        return { ...run, new_version: d.version };
      }
      return run;
    }
    default:
      return run;
  }
}

/** Role of an event for the event-stream filter. */
export function eventRole(ev: CuaEvent): string {
  switch (ev.type) {
    case 'agent_log':
      return ev.data?.role || 'agent';
    case 'plan_updated':
    case 'batch_progress':
      return 'planner';
    case 'repair_log':
    case 'repair_start':
    case 'repair_end':
      return 'repairer';
    case 'check':
      return 'supervisor';
    default:
      return 'system';
  }
}
