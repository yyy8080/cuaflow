import { describe, expect, it } from 'vitest';
import type { CuaEvent, Run, StepRecord } from '../api';
import { MAX_EVENTS, eventRole, initialRunState, runReducer, type RunState } from './runState';

const step = (id: string, index: number, extra: Partial<StepRecord> = {}): StepRecord => ({
  step_id: id,
  index,
  title: id,
  status: 'pending',
  attempts: 0,
  checks: [],
  repairs: [],
  ...extra,
});

const baseRun = (): Run => ({
  id: 'r1',
  kind: 'flow',
  flow_id: 'f1',
  version: 2,
  params: {},
  status: 'queued',
  created_at: '2024-01-01T00:00:00Z',
  steps: [step('open', 0), step('export', 1)],
});

let seq = 0;
const ev = (type: CuaEvent['type'], data: Record<string, any>, extra: Partial<CuaEvent> = {}): CuaEvent => ({
  seq: ++seq,
  ts: `2024-01-01T00:00:${String(seq).padStart(2, '0')}Z`,
  type,
  run_id: 'r1',
  data,
  ...extra,
});

const withRun = (): RunState => runReducer(initialRunState, { type: 'snapshot', run: baseRun() });
const apply = (s: RunState, ...events: CuaEvent[]) => runReducer(s, { type: 'events', events });

describe('runReducer', () => {
  it('applies run_status and step lifecycle', () => {
    let s = withRun();
    s = apply(
      s,
      ev('run_status', { status: 'running' }),
      ev('step_start', { index: 0, title: '打开' }, { step_id: 'open' }),
    );
    expect(s.run!.status).toBe('running');
    expect(s.run!.steps[0]).toMatchObject({ status: 'running', attempts: 1, title: '打开' });

    s = apply(s, ev('check', { level: 'L1', passed: true, detail: 'ok' }, { step_id: 'open' }));
    expect(s.run!.steps[0].checks).toHaveLength(1);

    const rec = step('open', 0, { status: 'passed', attempts: 1, duration_ms: 1200, screenshot: 'open.png', checks: [] });
    s = apply(s, ev('step_end', rec, { step_id: 'open' }));
    expect(s.run!.steps[0]).toMatchObject({ status: 'passed', duration_ms: 1200, screenshot: 'open.png' });
    expect(s.lastSeq).toBe(seq);
  });

  it('is idempotent for re-delivered events and keeps events sorted', () => {
    let s = withRun();
    const a = ev('step_start', { index: 0 }, { step_id: 'open' });
    const b = ev('check', { level: 'L0', passed: false, detail: 'x' }, { step_id: 'open' });
    s = apply(s, b, a); // out of order
    s = apply(s, a, b); // duplicates
    expect(s.events.map((e) => e.seq)).toEqual([a.seq, b.seq]);
    expect(s.run!.steps[0].attempts).toBe(1);
    expect(s.run!.steps[0].checks).toHaveLength(1);
  });

  it('tracks repairs and new_version', () => {
    let s = withRun();
    s = apply(s, ev('repair_start', { attempt: 1 }, { step_id: 'export' }));
    expect(s.run!.steps[1].status).toBe('repairing');
    expect(s.run!.steps[1].repairs).toEqual([expect.objectContaining({ attempt: 1, status: 'running' })]);

    s = apply(
      s,
      ev('repair_end', { attempt: 1, started_at: 'x', status: 'succeeded', summary: 'fixed selector', diff: '@@', new_version: 3 }, { step_id: 'export' }),
    );
    expect(s.run!.steps[1].repairs).toHaveLength(1);
    expect(s.run!.steps[1].repairs[0]).toMatchObject({ status: 'succeeded', summary: 'fixed selector' });
    expect(s.run!.new_version).toBe(3);

    s = apply(s, ev('version_created', { flow_id: 'f1', version: 4 }, { flow_id: 'f1' }));
    expect(s.run!.new_version).toBe(4);
  });

  it('creates unknown steps from step_start (authoring runs)', () => {
    let s = withRun();
    s = apply(s, ev('step_start', { index: 2, title: '保存' }, { step_id: 'save' }));
    expect(s.run!.steps.map((x) => x.step_id)).toEqual(['open', 'export', 'save']);
  });

  it('L3 check without step sets final_verdict', () => {
    let s = withRun();
    s = apply(s, ev('check', { level: 'L3', passed: true, detail: '文件已存在' }));
    expect(s.run!.final_verdict).toEqual({ passed: true, detail: '文件已存在' });
  });

  it('ignores events for other runs and stores events before snapshot without applying', () => {
    let s = apply(initialRunState, ev('run_status', { status: 'running' }));
    expect(s.run).toBeNull();
    expect(s.events).toHaveLength(1);
    s = runReducer(s, { type: 'snapshot', run: baseRun() });
    s = apply(s, ev('run_status', { status: 'failed' }, { run_id: 'other' }));
    expect(s.run!.status).toBe('queued');
  });

  it('caps the event buffer', () => {
    let s = withRun();
    const many = Array.from({ length: MAX_EVENTS + 10 }, () => ev('log', { level: 'info', text: 'x' }));
    s = apply(s, ...many);
    expect(s.events).toHaveLength(MAX_EVENTS);
    expect(s.events[s.events.length - 1].seq).toBe(seq);
  });

  it('maps event roles', () => {
    expect(eventRole(ev('agent_log', { role: 'author', kind: 'thought', text: '' }))).toBe('author');
    expect(eventRole(ev('repair_log', { text: '' }))).toBe('repairer');
    expect(eventRole(ev('check', { level: 'L1', passed: true }))).toBe('supervisor');
    expect(eventRole(ev('log', { level: 'info', text: '' }))).toBe('system');
  });
});
