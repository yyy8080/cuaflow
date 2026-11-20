import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { api, type FlowDetail, type Version } from '../api';
import { CodeView } from '../components/CodeView';
import { DiffView } from '../components/DiffView';
import { ParamsForm } from '../components/ParamsForm';
import { RunsTable } from '../components/RunsTable';
import { Badge, StatusBadge } from '../components/StatusBadge';
import { Card, Empty, ErrorBox, Loading } from '../components/common';
import { AUTHOR_LABEL, fmtTime, pct } from '../lib/format';
import { useAsync } from '../lib/hooks';
import { coerceParams, initialRawValues, type RawValues } from '../lib/params';

export function FlowDetailPage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const [sp, setSp] = useSearchParams();
  const flow = useAsync(() => api.getFlow(id), [id]);
  const runs = useAsync(() => api.listRuns({ flow_id: id, limit: 20 }), [id], 5000);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();

  const f = flow.data && flow.data.id === id ? flow.data : undefined;
  const qv = Number(sp.get('v'));
  const viewV = Number.isFinite(qv) && qv > 0 ? qv : f?.current_version;
  const setViewV = (v: number) => setSp({ v: String(v) }, { replace: true });

  if (flow.loading && !f) return <div className="page"><Loading /></div>;
  if (flow.error && !f) return <div className="page"><ErrorBox error={flow.error} onRetry={flow.reload} /></div>;
  if (!f) return null;

  const rollback = async (v: number) => {
    if (!confirm(`确认将「${f.name}」回滚到 v${v}？此后运行将默认使用 v${v}。`)) return;
    setBusy(true);
    setErr(undefined);
    try {
      await api.rollback(f.id, v);
      await flow.reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const del = async () => {
    if (!confirm(`确认删除脚本「${f.name}」及其全部版本？此操作不可撤销。`)) return;
    try {
      await api.deleteFlow(f.id);
      navigate('/flows');
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const versions = [...f.versions].sort((a, b) => b.version - a.version);

  return (
    <div className="page">
      <div className="breadcrumb">
        <Link to="/flows">脚本库</Link> / <span>{f.name || f.id}</span>
      </div>
      <div className="flow-header card">
        <div className="flow-header-main">
          <h1>
            {f.name || f.id} <Badge tone="info">v{f.current_version}</Badge>{' '}
            {f.side_effects ? <Badge tone="warning">有副作用</Badge> : <Badge tone="muted">只读</Badge>}
          </h1>
          {f.description && <p className="subtitle">{f.description}</p>}
          <div className="mono small intent">{f.intent_template}</div>
          <div className="muted small">
            <code>{f.id}</code> · 创建于 {fmtTime(f.created_at)} · 更新于 {fmtTime(f.updated_at)}
            {f.tags?.map((t) => (
              <span key={t} className="tag">
                {t}
              </span>
            ))}
          </div>
        </div>
        <div className="stats">
          <Stat label="运行" value={f.stats.runs} />
          <Stat label="成功率" value={pct(f.stats.successes, f.stats.runs)} />
          <Stat label="修复" value={f.stats.repairs} />
          <div className="stat">
            <div className="stat-label">最近</div>
            <div className="stat-value">
              <StatusBadge status={f.stats.last_status} pulse={false} />
            </div>
          </div>
          <button className="btn btn-danger btn-sm" onClick={del}>
            删除
          </button>
        </div>
      </div>
      <ErrorBox error={err} />

      <div className="grid-2">
        <Card title="版本时间线">
          <ol className="versions">
            {versions.map((v) => (
              <li key={v.version} className={`version ${viewV === v.version ? 'selected' : ''}`}>
                <div className="version-dot" data-author={v.author} />
                <div className="version-body">
                  <div className="version-head">
                    <strong>v{v.version}</strong>
                    {v.version === f.current_version && <Badge tone="success">当前</Badge>}
                    <Badge tone={v.author === 'repairer' ? 'warning' : v.author === 'human' ? 'info' : 'neutral'}>
                      {AUTHOR_LABEL[v.author] ?? v.author}
                    </Badge>
                    {v.repaired_step && (
                      <span className="small">
                        修补 <code>{v.repaired_step}</code>
                      </span>
                    )}
                    <span className="grow" />
                    <span className="muted small">{fmtTime(v.created_at)}</span>
                  </div>
                  <div className="version-reason">{v.reason || <span className="muted">（无说明）</span>}</div>
                  <div className="version-foot">
                    <span className="muted small">
                      健康度 {v.health.successes}/{v.health.runs}（{pct(v.health.successes, v.health.runs)}）
                      {v.parent_version !== undefined && v.parent_version !== null && ` · 基于 v${v.parent_version}`}
                    </span>
                    <span className="grow" />
                    <button className="btn btn-sm" onClick={() => setViewV(v.version)}>
                      查看
                    </button>
                    <button
                      className="btn btn-sm"
                      disabled={busy || v.version === f.current_version}
                      onClick={() => void rollback(v.version)}
                    >
                      回滚到此版本
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ol>
        </Card>

        <RunPanel flow={f} onStarted={(runId) => navigate(`/runs/${runId}`)} />
      </div>

      {viewV !== undefined && <VersionViewer flowId={f.id} version={viewV} />}

      <Card title="最近执行" actions={<Link to={`/runs?flow_id=${encodeURIComponent(f.id)}`}>全部 →</Link>}>
        {runs.loading && !runs.data ? (
          <Loading />
        ) : runs.error && !runs.data ? (
          <ErrorBox error={runs.error} onRetry={runs.reload} />
        ) : !runs.data?.length ? (
          <Empty>此脚本还没有执行记录。</Empty>
        ) : (
          <RunsTable runs={runs.data} showFlow={false} />
        )}
      </Card>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}

function VersionViewer({ flowId, version }: { flowId: string; version: number }) {
  const v = useAsync(() => api.getVersion(flowId, version), [flowId, version]);
  const [tab, setTab] = useState<'steps' | 'source' | 'diff'>('steps');
  const data: Version | undefined = v.data?.version === version ? v.data : undefined;
  return (
    <Card
      title={
        <>
          版本 v{version}
          {data?.parent_version !== undefined && data?.parent_version !== null && (
            <span className="muted small"> （父版本 v{data.parent_version}）</span>
          )}
        </>
      }
      actions={
        <div className="seg">
          <button className={tab === 'steps' ? 'active' : ''} onClick={() => setTab('steps')}>
            步骤
          </button>
          <button className={tab === 'source' ? 'active' : ''} onClick={() => setTab('source')}>
            源码
          </button>
          <button className={tab === 'diff' ? 'active' : ''} onClick={() => setTab('diff')}>
            与父版本差异
          </button>
        </div>
      }
    >
      {v.error && !data ? (
        <ErrorBox error={v.error} onRetry={v.reload} />
      ) : !data ? (
        <Loading />
      ) : tab === 'steps' ? (
        <ol className="step-specs">
          {data.steps.map((s, i) => (
            <li key={s.id} className={data.repaired_step === s.id ? 'hl' : ''}>
              <div className="step-spec-head">
                <span className="tl-index">{i + 1}.</span>
                <strong>{s.title}</strong>
                <code className="small">{s.id}</code>
                {s.side_effect && <Badge tone="warning">副作用</Badge>}
                {s.critical && <Badge tone="danger">关键</Badge>}
                {data.repaired_step === s.id && <Badge tone="warning">本版本修补</Badge>}
              </div>
              {s.expect && <div className="muted small">预期：{s.expect}</div>}
            </li>
          ))}
          {!data.steps.length && <Empty>无步骤</Empty>}
        </ol>
      ) : tab === 'source' ? (
        <CodeView
          source={data.source}
          maxHeight={560}
          highlight={data.repaired_step ? (_n, t) => t.includes(`def ${data.repaired_step}`) : undefined}
        />
      ) : (
        <DiffView diff={data.diff} maxHeight={560} empty={data.parent_version == null ? '初始版本，无父版本' : '无差异'} />
      )}
    </Card>
  );
}

function RunPanel({ flow, onStarted }: { flow: FlowDetail; onStarted: (runId: string) => void }) {
  const [raw, setRaw] = useState<RawValues>(() => initialRawValues(flow.params_schema));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [version, setVersion] = useState<string>('');
  const [fromStep, setFromStep] = useState<string>('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();

  useEffect(() => {
    setRaw(initialRawValues(flow.params_schema));
    // reset only when switching flows
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flow.id]);

  const steps = useMemo(() => {
    const vn = version ? Number(version) : flow.current_version;
    return flow.versions.find((v) => v.version === vn)?.steps ?? [];
  }, [flow, version]);

  const run = async () => {
    const { values, errors } = coerceParams(flow.params_schema, raw);
    setErrors(errors);
    if (Object.keys(errors).length) return;
    if (flow.side_effects && !confirm('此脚本包含有副作用的步骤（如提交、发送、删除），确认执行？')) return;
    setBusy(true);
    setErr(undefined);
    try {
      const r = await api.runFlow(flow.id, {
        params: values,
        version: version ? Number(version) : undefined,
        from_step: fromStep || undefined,
      });
      onStarted(r.id);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="运行脚本">
      <ParamsForm schema={flow.params_schema} values={raw} errors={errors} onChange={setRaw} />
      <div className="form-row">
        <div className="field grow">
          <label>版本</label>
          <select value={version} onChange={(e) => setVersion(e.target.value)}>
            <option value="">当前版本（v{flow.current_version}）</option>
            {[...flow.versions]
              .sort((a, b) => b.version - a.version)
              .map((v) => (
                <option key={v.version} value={v.version}>
                  v{v.version} · {AUTHOR_LABEL[v.author] ?? v.author}
                </option>
              ))}
          </select>
        </div>
        <div className="field grow">
          <label>从步骤开始</label>
          <select value={fromStep} onChange={(e) => setFromStep(e.target.value)}>
            <option value="">从头开始</option>
            {steps.map((s, i) => (
              <option key={s.id} value={s.id}>
                {i + 1}. {s.title}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="form-actions">
        <span className="grow" />
        <button className="btn btn-primary" disabled={busy} onClick={() => void run()}>
          {busy ? '启动中…' : '▶ 运行'}
        </button>
      </div>
      <ErrorBox error={err} />
    </Card>
  );
}
