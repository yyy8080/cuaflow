import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, type CuaEvent, type Run, type StepRecord } from '../api';
import { JsonView } from '../components/CodeView';
import { DiffView } from '../components/DiffView';
import { EventStream } from '../components/EventStream';
import { StepIcon, StepTimeline } from '../components/StepTimeline';
import { Badge, StatusBadge } from '../components/StatusBadge';
import { ErrorBox, Loading, Screenshot } from '../components/common';
import { TERMINAL_RUN, durationBetween, fmtDuration, fmtTime, STEP_STATUS_LABEL } from '../lib/format';
import { useDebounced, useTick } from '../lib/hooks';
import { REFETCH_TYPES, initialRunState, runReducer } from '../lib/runState';
import { useEventSocket, type WsStatus } from '../ws';

const WS_LABEL: Record<WsStatus, string> = { connecting: '连接中', open: '实时', closed: '已断开，重连中' };

/** 当用户未手动锁定步骤时自动选择展示的步骤：优先当前执行/修复中的步骤，其次失败步骤，否则取最近执行过的步骤。 */
function autoStep(run: Run | null): string | undefined {
  if (!run || !run.steps.length) return undefined;
  const live = run.steps.find((s) => s.status === 'running' || s.status === 'repairing');
  if (live) return live.step_id;
  const failed = run.steps.find((s) => s.status === 'failed');
  if (failed) return failed.step_id;
  const touched = run.steps.filter((s) => s.status !== 'pending');
  return (touched[touched.length - 1] ?? run.steps[0]).step_id;
}

export function RunPage() {
  const { id = '' } = useParams();
  const [state, dispatch] = useReducer(runReducer, initialRunState);
  const [loadErr, setLoadErr] = useState<Error>();
  const [pinned, setPinned] = useState<string>();
  const [actionErr, setActionErr] = useState<string>();
  const [aborting, setAborting] = useState(false);
  const lastSeq = useRef(0);
  lastSeq.current = state.lastSeq;
  const currentId = useRef(id);
  currentId.current = id;

  const refetch = useCallback(async () => {
    const rid = currentId.current;
    try {
      const run = await api.getRun(rid);
      if (rid === currentId.current) dispatch({ type: 'snapshot', run });
    } catch {
      /* 保留当前实时状态，待后续事件触发时再重试 */
    }
  }, []);
  const refetchSoon = useDebounced(() => void refetch(), 400);

  const ingest = useCallback(
    (evs: CuaEvent[]) => {
      if (!evs.length) return;
      dispatch({ type: 'events', events: evs });
      if (evs.some((e) => REFETCH_TYPES.has(e.type))) refetchSoon();
    },
    [refetchSoon],
  );

  // 初始加载：先拉取历史事件入库，再获取运行快照（权威状态，已包含历史事件的结果）。
  useEffect(() => {
    let cancelled = false;
    dispatch({ type: 'reset' });
    setPinned(undefined);
    setLoadErr(undefined);
    (async () => {
      try {
        const evs = await api.runEvents(id);
        if (cancelled) return;
        dispatch({ type: 'events', events: evs });
        const run = await api.getRun(id);
        if (cancelled) return;
        dispatch({ type: 'snapshot', run });
      } catch (e) {
        if (!cancelled) setLoadErr(e as Error);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  const wsStatus = useEventSocket({
    runId: id,
    onEvent: (ev) => {
      if (!ev.run_id || ev.run_id === currentId.current) ingest([ev]);
    },
    onOpen: () => {
      // 补拉断线期间遗漏的事件
      const rid = currentId.current;
      api
        .runEvents(rid, lastSeq.current || undefined)
        .then((evs) => {
          if (rid === currentId.current) ingest(evs);
        })
        .catch(() => undefined);
    },
  });

  const run = state.run;
  const live = !!run && !TERMINAL_RUN.has(run.status);
  useTick(1000, live);

  const selectedId = pinned ?? autoStep(run);
  const selected = run?.steps.find((s) => s.step_id === selectedId);

  const abort = async () => {
    if (!run || !confirm('确认终止此次执行？')) return;
    setAborting(true);
    setActionErr(undefined);
    try {
      dispatch({ type: 'snapshot', run: await api.abortRun(run.id) });
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setAborting(false);
    }
  };

  if (loadErr && !run) {
    return (
      <div className="page">
        <ErrorBox error={loadErr} onRetry={() => window.location.reload()} />
      </div>
    );
  }
  if (!run) {
    return (
      <div className="page">
        <Loading />
      </div>
    );
  }

  return (
    <div className="run-page">
      <header className="run-top">
        <div className="run-top-row">
          <Link to="/runs" className="muted">
            ← 执行记录
          </Link>
          <h1 className="mono">{run.id}</h1>
          <StatusBadge status={run.status} />
          <Badge tone="neutral">{run.kind === 'author' ? '录制' : run.kind === 'mission' ? '长任务规划' : '回放'}</Badge>
          {run.flow_id && (
            <Link to={`/flows/${run.flow_id}${run.version != null ? `?v=${run.version}` : ''}`}>
              📄 {run.flow_id}
              {run.version != null && ` v${run.version}`}
            </Link>
          )}
          {run.new_version != null && run.flow_id && (
            <Link to={`/flows/${run.flow_id}?v=${run.new_version}`}>
              <Badge tone="success">✨ 已生成新版本 v{run.new_version}</Badge>
            </Link>
          )}
          {run.usage && (
            <Badge tone="info" title={`LLM 调用 ${run.usage.llm_calls} 次，脚本执行 ${run.usage.script_steps} 步`}>
              ⚡ 消耗 {run.usage.total_tokens} tok
              {run.usage.estimated_tokens_saved > 0 ? ` · 省 ~${run.usage.estimated_tokens_saved} tok` : ''}
            </Badge>
          )}
          <span className="grow" />
          <span className={`ws-dot ws-${wsStatus}`} title={WS_LABEL[wsStatus]}>
            ● {WS_LABEL[wsStatus]}
          </span>
          <span className="muted small nowrap">
            {fmtTime(run.started_at ?? run.created_at)} · {fmtDuration(durationBetween(run.started_at, run.finished_at)) || '–'}
          </span>
          {live && (
            <button className="btn btn-danger btn-sm" disabled={aborting} onClick={() => void abort()}>
              {aborting ? '终止中…' : '■ 终止'}
            </button>
          )}
        </div>
        <div className="run-top-details">
          <details>
            <summary>参数</summary>
            <JsonView value={run.params} empty="无参数" />
          </details>
          <details open={!!run.output && Object.keys(run.output).length > 0}>
            <summary>输出</summary>
            <JsonView value={run.output} empty="暂无输出" />
          </details>
          {run.final_verdict && (
            <div className={`verdict ${run.final_verdict.passed ? 'ok' : 'bad'}`}>
              <strong>最终校验（L3）：{run.final_verdict.passed ? '✅ 通过' : '❌ 未通过'}</strong>
              <span>{run.final_verdict.detail}</span>
            </div>
          )}
          {run.task_id && <span className="muted small">任务 {run.task_id}</span>}
        </div>
        {run.status === 'waiting_human' && (
          <div className="alert alert-warning">
            🙋 执行已暂停，等待人工介入。<Link to="/tickets">前往处理 →</Link>
          </div>
        )}
        {run.error && <div className="alert alert-danger">❌ {run.error}</div>}
        <ErrorBox error={actionErr} />
      </header>

      <div className="run-grid">
        <section className="run-col run-steps">
          <div className="col-head">
            <span>步骤（{run.steps.filter((s) => ['passed', 'repaired', 'skipped'].includes(s.status)).length}/{run.steps.length}）</span>
            {pinned && (
              <button className="btn btn-sm btn-ghost" onClick={() => setPinned(undefined)} title="自动跟随当前步骤">
                跟随
              </button>
            )}
          </div>
          <div className="col-body">
            <StepTimeline steps={run.steps} selected={selectedId} onSelect={setPinned} />
          </div>
        </section>

        <section className="run-col run-detail">
          {selected ? (
            <StepDetail runId={run.id} step={selected} events={state.events} />
          ) : (
            <div className="muted pad">{run.kind === 'author' ? '录制中，步骤会在生成后出现…' : '尚无步骤'}</div>
          )}
        </section>

        <section className="run-col run-events">
          <div className="col-head">
            <span>事件流</span>
            <span className="muted small">{state.events.length} 条</span>
          </div>
          <EventStream events={state.events} onSelectStep={setPinned} />
        </section>
      </div>
    </div>
  );
}

function StepDetail({ runId, step, events }: { runId: string; step: StepRecord; events: CuaEvent[] }) {
  const live = step.status === 'running' || step.status === 'repairing';
  const dur = step.duration_ms ?? durationBetween(step.started_at, live ? undefined : step.ended_at);
  const repairLog = useMemo(
    () =>
      events.filter(
        (e) =>
          e.step_id === step.step_id &&
          (e.type === 'repair_log' || (e.type === 'agent_log' && ['repairer', 'fallback'].includes(e.data?.role))),
      ),
    [events, step.step_id],
  );
  return (
    <div className="step-detail">
      <div className="step-detail-head">
        <StepIcon status={step.status} />
        <h2>
          {step.index + 1}. {step.title || step.step_id}
        </h2>
        <StatusBadge status={step.status} />
      </div>
      <div className="muted small step-meta">
        <code>{step.step_id}</code>
        <span>{STEP_STATUS_LABEL[step.status]}</span>
        {dur !== undefined && <span>耗时 {fmtDuration(dur)}</span>}
        <span>尝试 {step.attempts} 次</span>
        {step.started_at && <span>开始 {fmtTime(step.started_at)}</span>}
      </div>

      <div className="shot-wrap">
        <Screenshot key={step.screenshot} runId={runId} path={step.screenshot} />
      </div>

      {step.expect && (
        <div className="kv">
          <div className="kv-k">预期</div>
          <div className="kv-v">{step.expect}</div>
        </div>
      )}
      {step.error && (
        <div className="kv">
          <div className="kv-k">错误{step.error_kind && <Badge tone="danger">{step.error_kind}</Badge>}</div>
          <pre className="error-pre">{step.error}</pre>
        </div>
      )}

      <h4>检查（{step.checks.length}）</h4>
      {step.checks.length ? (
        <ul className="checks">
          {step.checks.map((c, i) => (
            <li key={i} className={c.passed ? 'ok' : 'bad'}>
              <span className={`lvl lvl-${c.level}`}>{c.level}</span>
              <span>{c.passed ? '✅' : '❌'}</span>
              <span className="check-detail">{c.detail}</span>
              <span className="muted small nowrap">{fmtTime(c.ts)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="muted small">暂无检查结果</div>
      )}

      {(step.repairs.length > 0 || repairLog.length > 0) && (
        <>
          <h4>修复（{step.repairs.length}）</h4>
          {step.repairs.map((r) => (
            <div key={r.attempt} className={`repair repair-${r.status}`}>
              <div className="repair-head">
                <strong>第 {r.attempt} 次</strong>
                <StatusBadge status={r.status} />
                {r.new_version != null && <Badge tone="success">→ v{r.new_version}</Badge>}
                <span className="grow" />
                <span className="muted small">
                  {fmtTime(r.started_at)}
                  {r.ended_at && ` · ${fmtDuration(durationBetween(r.started_at, r.ended_at))}`}
                </span>
              </div>
              {r.summary && <div className="repair-summary">{r.summary}</div>}
              {r.diff && <DiffView diff={r.diff} maxHeight={360} />}
            </div>
          ))}
          {repairLog.length > 0 && (
            <details className="repair-log" open={step.status === 'repairing'}>
              <summary>修复日志（{repairLog.length}）</summary>
              <pre>{repairLog.map((e) => `[${fmtTime(e.ts)}]${e.data?.kind ? ' ' + e.data.kind + ':' : ''} ${e.data?.text ?? ''}`).join('\n')}</pre>
            </details>
          )}
        </>
      )}
    </div>
  );
}
