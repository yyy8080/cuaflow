import { useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, type GlobalStats, type Task, type TaskMode } from '../api';
import { Badge, StatusBadge } from '../components/StatusBadge';
import { Card, Empty, ErrorBox, Loading } from '../components/common';
import { TERMINAL_RUN, VERDICT_LABEL, fmtTime, fmtTokens, pct, shortJson } from '../lib/format';
import { useAsync } from '../lib/hooks';
import { guessValue } from '../lib/params';

type Row = { key: string; value: string };

const VERDICT_TONE: Record<string, 'success' | 'info' | 'warning'> = {
  run: 'success',
  adapt: 'info',
  mission: 'info',
  new: 'warning',
};

const MODE_LABEL: Record<TaskMode, string> = {
  auto: '智能路由（复用脚本 / 长任务自动规划）',
  mission: '长任务自主规划（拆解阶段 + 批量编译省 Token）',
  single: '单流程脚本模式（一任务一脚本）',
};

export function TasksPage() {
  const navigate = useNavigate();
  const tasks = useAsync(() => api.listTasks(), [], 3000);
  const stats = useAsync(() => api.stats().catch(() => null), [], 5000);
  const [instruction, setInstruction] = useState('');
  const [startUrl, setStartUrl] = useState('');
  const [mode, setMode] = useState<TaskMode>('auto');
  const [rows, setRows] = useState<Row[]>([]);
  const [forceNew, setForceNew] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string>();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!instruction.trim()) return;
    setSubmitting(true);
    setErr(undefined);
    try {
      const params: Record<string, any> = {};
      for (const r of rows) if (r.key.trim()) params[r.key.trim()] = guessValue(r.value);
      const t = await api.createTask({
        instruction: instruction.trim(),
        start_url: startUrl.trim() || undefined,
        params: Object.keys(params).length ? params : undefined,
        force_new: forceNew || undefined,
        mode,
      });
      setInstruction('');
      setRows([]);
      setForceNew(false);
      await tasks.reload();
      if (t.run_id) navigate(`/runs/${t.run_id}`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const setRow = (i: number, patch: Partial<Row>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <div className="page">
      <div className="page-head">
        <h1>长任务中心</h1>
        <p className="subtitle">
          AI 全流程自主规划并持续监控浏览器/桌面操作；自动将重复动作编译为参数化脚本以节省 Token，并由监督者实时守护脚本稳定运行与自动自愈。
        </p>
      </div>

      {stats.data && <StatsOverview stats={stats.data} />}

      <Card title="新建任务 / 长周期使命">
        <form className="task-form" onSubmit={submit}>
          <textarea
            className="instruction"
            rows={3}
            placeholder="例如：登录后台管理系统，遍历所有待处理订单页面，逐条提取客户与库存信息，汇总生成日报"
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void submit(e as unknown as FormEvent);
            }}
          />
          <div className="form-row">
            <div className="field grow">
              <label>起始 URL（可选）</label>
              <input type="text" placeholder="https://…" value={startUrl} onChange={(e) => setStartUrl(e.target.value)} />
            </div>
            <div className="field">
              <label>执行模式</label>
              <select value={mode} onChange={(e) => setMode(e.target.value as TaskMode)}>
                {(Object.keys(MODE_LABEL) as TaskMode[]).map((m) => (
                  <option key={m} value={m}>
                    {MODE_LABEL[m]}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="field">
            <label>参数（可选）</label>
            {rows.map((r, i) => (
              <div className="kv-row" key={i}>
                <input type="text" placeholder="名称" value={r.key} onChange={(e) => setRow(i, { key: e.target.value })} />
                <input type="text" placeholder="值" value={r.value} onChange={(e) => setRow(i, { value: e.target.value })} />
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => setRows(rows.filter((_, j) => j !== i))}>
                  ✕
                </button>
              </div>
            ))}
            <div>
              <button type="button" className="btn btn-sm" onClick={() => setRows([...rows, { key: '', value: '' }])}>
                + 添加参数
              </button>
            </div>
          </div>
          <div className="form-actions">
            <label className="check">
              <input type="checkbox" checked={forceNew} onChange={(e) => setForceNew(e.target.checked)} />
              忽略已有脚本（重新编译）
            </label>
            <span className="grow" />
            <span className="muted small">Ctrl+Enter 提交</span>
            <button className="btn btn-primary" type="submit" disabled={submitting || !instruction.trim()}>
              {submitting ? '提交中…' : '启动自主执行'}
            </button>
          </div>
          <ErrorBox error={err} />
        </form>
      </Card>

      <Card title="任务列表" actions={<button className="btn btn-sm" onClick={() => void tasks.reload()}>刷新</button>}>
        {tasks.loading && !tasks.data ? (
          <Loading />
        ) : tasks.error && !tasks.data ? (
          <ErrorBox error={tasks.error} onRetry={tasks.reload} />
        ) : !tasks.data?.length ? (
          <Empty>还没有任务，在上方提交第一个吧。</Empty>
        ) : (
          <TaskTable tasks={tasks.data} onReload={tasks.reload} />
        )}
      </Card>
    </div>
  );
}

function StatsOverview({ stats }: { stats: GlobalStats }) {
  return (
    <div className="kpi-grid">
      <div className="kpi-card">
        <div className="kpi-label">沉淀可复用脚本</div>
        <div className="kpi-val">{stats.flows}</div>
        <div className="kpi-sub">累计自动修复 {stats.auto_repairs} 次</div>
      </div>
      <div className="kpi-card">
        <div className="kpi-label">脚本零探索执行步数</div>
        <div className="kpi-val">{stats.script_steps}</div>
        <div className="kpi-sub">共回放 {stats.flow_runs} 次 · 稳定率 {pct(stats.flow_successes, stats.flow_runs || 1)}</div>
      </div>
      <div className="kpi-card">
        <div className="kpi-label">累计节省 Token（估算）</div>
        <div className="kpi-val accent">{fmtTokens(stats.estimated_tokens_saved)}</div>
        <div className="kpi-sub">
          已消耗 {fmtTokens(stats.total_tokens)} · 节省率 {Math.round((stats.savings_ratio || 0) * 100)}%
        </div>
      </div>
      <div className="kpi-card">
        <div className="kpi-label">持续监控任务</div>
        <div className="kpi-val">{stats.schedules_active}</div>
        <div className="kpi-sub">
          {stats.schedules_tripped > 0 ? (
            <Link to="/schedules" className="danger-text">
              ⚠️ {stats.schedules_tripped} 个熔断待处理
            </Link>
          ) : (
            <Link to="/schedules">管理定时巡检 →</Link>
          )}
        </div>
      </div>
    </div>
  );
}

function TaskTable({ tasks, onReload }: { tasks: Task[]; onReload: () => void }) {
  const [busyId, setBusyId] = useState<string>();

  const handleResume = async (id: string) => {
    setBusyId(id);
    try {
      await api.resumeTask(id);
      onReload();
    } finally {
      setBusyId(undefined);
    }
  };

  const handleAbort = async (id: string) => {
    setBusyId(id);
    try {
      await api.abortTask(id);
      onReload();
    } finally {
      setBusyId(undefined);
    }
  };

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>任务与规划进度</th>
            <th>状态</th>
            <th>路由</th>
            <th>Token 效率</th>
            <th>关联脚本</th>
            <th>监控与操作</th>
            <th>时间</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((t) => {
            const phases = t.plan?.phases ?? [];
            const batches = Object.entries(t.checkpoint?.batches ?? {});
            const canResume = TERMINAL_RUN.has(t.status) && t.status !== 'succeeded' && (!!t.checkpoint || !!t.flow_id);
            const isLive = !TERMINAL_RUN.has(t.status);
            return (
              <tr key={t.id}>
                <td className="cell-main">
                  <div className="clamp-2" title={t.instruction}>
                    {t.instruction}
                  </div>
                  {phases.length > 0 && (
                    <div className="phase-strip">
                      {phases.map((p) => (
                        <span key={p.id} className={`phase-pill phase-${p.status ?? 'pending'}`} title={p.note || p.title}>
                          {p.title}
                        </span>
                      ))}
                    </div>
                  )}
                  {batches.map(([bid, b]) => (
                    <div key={bid} className="muted small">
                      ⚡ 批处理 <code>{bid}</code>: {b.completed_count}/{b.total} 项
                    </div>
                  ))}
                  {t.final_response && (
                    <div className="muted small clamp-2" title={t.final_response}>
                      ↳ {t.final_response}
                    </div>
                  )}
                  {Object.keys(t.params || {}).length > 0 && <div className="muted small mono">{shortJson(t.params)}</div>}
                </td>
                <td>
                  <StatusBadge status={t.status} />
                </td>
                <td>
                  {t.match ? (
                    <Badge tone={VERDICT_TONE[t.match.verdict] ?? 'neutral'} title={t.match.reason}>
                      {VERDICT_LABEL[t.match.verdict] ?? t.match.verdict}
                    </Badge>
                  ) : (
                    <span className="muted">–</span>
                  )}
                </td>
                <td className="nowrap small">
                  {t.usage ? (
                    <div>
                      <div>消耗 {fmtTokens(t.usage.total_tokens)}</div>
                      {t.usage.estimated_tokens_saved > 0 && (
                        <div className="success-text">省 ~{fmtTokens(t.usage.estimated_tokens_saved)}</div>
                      )}
                    </div>
                  ) : (
                    <span className="muted">–</span>
                  )}
                </td>
                <td>
                  {t.flow_id || t.match?.flow_id ? (
                    <Link to={`/flows/${t.flow_id || t.match?.flow_id}`}>{t.flow_id || t.match?.flow_id}</Link>
                  ) : (
                    <span className="muted">–</span>
                  )}
                </td>
                <td className="nowrap">
                  <div className="row-actions">
                    {t.run_id && <Link to={`/runs/${t.run_id}`}>查看执行 →</Link>}
                    {canResume && (
                      <button className="btn btn-sm" disabled={busyId === t.id} onClick={() => void handleResume(t.id)}>
                        断点续跑
                      </button>
                    )}
                    {isLive && (
                      <button className="btn btn-sm btn-danger" disabled={busyId === t.id} onClick={() => void handleAbort(t.id)}>
                        终止
                      </button>
                    )}
                  </div>
                </td>
                <td className="nowrap muted">{fmtTime(t.created_at)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
