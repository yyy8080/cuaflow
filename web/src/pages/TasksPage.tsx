import { useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, type Task } from '../api';
import { Badge, StatusBadge } from '../components/StatusBadge';
import { Card, Empty, ErrorBox, Loading } from '../components/common';
import { VERDICT_LABEL, fmtTime, shortJson } from '../lib/format';
import { useAsync } from '../lib/hooks';
import { guessValue } from '../lib/params';

type Row = { key: string; value: string };

const VERDICT_TONE: Record<string, 'success' | 'info' | 'warning'> = { run: 'success', adapt: 'info', new: 'warning' };

export function TasksPage() {
  const navigate = useNavigate();
  const tasks = useAsync(() => api.listTasks(), [], 3000);
  const [instruction, setInstruction] = useState('');
  const [startUrl, setStartUrl] = useState('');
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
        <h1>任务</h1>
        <p className="subtitle">用自然语言描述要做的事。系统会先在脚本库中匹配可复用的脚本，找不到时再录制新脚本。</p>
      </div>

      <Card title="新建任务">
        <form className="task-form" onSubmit={submit}>
          <textarea
            className="instruction"
            rows={3}
            placeholder="例如：打开 Edge，导出 2024-10 的销售报表并保存到 D:\reports"
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
              强制重新录制
            </label>
            <span className="grow" />
            <span className="muted small">Ctrl+Enter 提交</span>
            <button className="btn btn-primary" type="submit" disabled={submitting || !instruction.trim()}>
              {submitting ? '提交中…' : '提交任务'}
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
          <TaskTable tasks={tasks.data} />
        )}
      </Card>
    </div>
  );
}

function TaskTable({ tasks }: { tasks: Task[] }) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>任务</th>
            <th>状态</th>
            <th>匹配</th>
            <th>脚本</th>
            <th>执行</th>
            <th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((t) => (
            <tr key={t.id}>
              <td className="cell-main">
                <div className="clamp-2" title={t.instruction}>
                  {t.instruction}
                </div>
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
              <td>
                {t.flow_id || t.match?.flow_id ? (
                  <Link to={`/flows/${t.flow_id || t.match?.flow_id}`}>{t.flow_id || t.match?.flow_id}</Link>
                ) : (
                  <span className="muted">–</span>
                )}
              </td>
              <td>{t.run_id ? <Link to={`/runs/${t.run_id}`}>查看执行 →</Link> : <span className="muted">–</span>}</td>
              <td className="nowrap muted">{fmtTime(t.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
