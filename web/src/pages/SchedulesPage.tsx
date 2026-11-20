import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { api, type Schedule } from '../api';
import { StatusBadge } from '../components/StatusBadge';
import { Card, Empty, ErrorBox, Loading } from '../components/common';
import { fmtTime, pct } from '../lib/format';
import { useAsync } from '../lib/hooks';

export function SchedulesPage() {
  const schedules = useAsync(() => api.listSchedules(), [], 4000);
  const flows = useAsync(() => api.listFlows(), []);
  const [name, setName] = useState('');
  const [kind, setKind] = useState<'flow' | 'task'>('flow');
  const [flowId, setFlowId] = useState('');
  const [instruction, setInstruction] = useState('');
  const [intervalSec, setIntervalSec] = useState(300);
  const [maxFailures, setMaxFailures] = useState(3);
  const [runNow, setRunNow] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string>();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setSubmitting(true);
    setErr(undefined);
    try {
      await api.createSchedule({
        name: name.trim(),
        interval_seconds: Math.max(5, intervalSec),
        kind,
        flow_id: kind === 'flow' ? flowId || flows.data?.[0]?.id : undefined,
        instruction: kind === 'task' ? instruction.trim() : undefined,
        max_consecutive_failures: Math.max(1, maxFailures),
        run_immediately: runNow,
      });
      setName('');
      setInstruction('');
      await schedules.reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1>持续监控与巡检</h1>
        <p className="subtitle">
          以固定周期无人值守运行脚本或长任务。监督者逐次检查并自动自愈异常；若目标站点大面积故障连续失败达到阈值，熔断器会自动暂停任务防止浪费 Token。
        </p>
      </div>

      <Card title="新建持续监控">
        <form className="task-form" onSubmit={submit}>
          <div className="form-grid">
            <div className="field">
              <label>监控名称</label>
              <input
                type="text"
                placeholder="例如：每 5 分钟订单队列巡检"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="field">
              <label>目标类型</label>
              <select value={kind} onChange={(e) => setKind(e.target.value as 'flow' | 'task')}>
                <option value="flow">已编译脚本（Flow · 零探索 Token）</option>
                <option value="task">自然语言使命（动态规划）</option>
              </select>
            </div>
            {kind === 'flow' ? (
              <div className="field">
                <label>选择脚本</label>
                <select value={flowId || flows.data?.[0]?.id || ''} onChange={(e) => setFlowId(e.target.value)}>
                  {!flows.data?.length && <option value="">（暂无脚本）</option>}
                  {flows.data?.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.name} ({f.id})
                    </option>
                  ))}
                </select>
              </div>
            ) : (
              <div className="field">
                <label>任务指令</label>
                <input
                  type="text"
                  placeholder="检查仪表盘告警并汇总异常指标"
                  value={instruction}
                  onChange={(e) => setInstruction(e.target.value)}
                />
              </div>
            )}
            <div className="field">
              <label>执行间隔（秒）</label>
              <input
                type="number"
                min={5}
                value={intervalSec}
                onChange={(e) => setIntervalSec(Math.max(5, parseInt(e.target.value, 10) || 60))}
              />
            </div>
            <div className="field">
              <label>连续失败熔断阈值</label>
              <input
                type="number"
                min={1}
                value={maxFailures}
                onChange={(e) => setMaxFailures(Math.max(1, parseInt(e.target.value, 10) || 3))}
              />
            </div>
          </div>
          <div className="form-actions">
            <label className="check">
              <input type="checkbox" checked={runNow} onChange={(e) => setRunNow(e.target.checked)} />
              创建后立即执行一次
            </label>
            <span className="grow" />
            <button
              className="btn btn-primary"
              type="submit"
              disabled={submitting || !name.trim() || (kind === 'flow' ? !flows.data?.length && !flowId : !instruction.trim())}
            >
              {submitting ? '创建中…' : '启动持续监控'}
            </button>
          </div>
          <ErrorBox error={err} />
        </form>
      </Card>

      <Card title="监控列表" actions={<button className="btn btn-sm" onClick={() => void schedules.reload()}>刷新</button>}>
        {schedules.loading && !schedules.data ? (
          <Loading />
        ) : schedules.error && !schedules.data ? (
          <ErrorBox error={schedules.error} onRetry={schedules.reload} />
        ) : !schedules.data?.length ? (
          <Empty>暂无持续监控计划。</Empty>
        ) : (
          <ScheduleTable list={schedules.data} onReload={schedules.reload} />
        )}
      </Card>
    </div>
  );
}

function ScheduleTable({ list, onReload }: { list: Schedule[]; onReload: () => void }) {
  const [busy, setBusy] = useState<string>();

  const actOn = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    try {
      await fn();
      onReload();
    } finally {
      setBusy(undefined);
    }
  };

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>名称 / 目标</th>
            <th>状态</th>
            <th>间隔</th>
            <th>稳定度</th>
            <th>连续失败</th>
            <th>最近执行</th>
            <th>下次计划</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {list.map((sc) => (
            <tr key={sc.id}>
              <td className="cell-main">
                <div>
                  <strong>{sc.name}</strong>
                </div>
                <div className="muted small">
                  {sc.kind === 'flow' && sc.flow_id ? (
                    <Link to={`/flows/${sc.flow_id}`}>📄 {sc.flow_id}</Link>
                  ) : (
                    <span>✨ {sc.instruction}</span>
                  )}
                </div>
              </td>
              <td>
                <StatusBadge status={sc.status} />
              </td>
              <td className="nowrap">{sc.interval_seconds}s</td>
              <td className="nowrap">
                {sc.success_count}/{sc.runs_count} ({pct(sc.success_count, sc.runs_count)})
              </td>
              <td className="nowrap">
                <span className={sc.consecutive_failures > 0 ? 'danger-text' : 'muted'}>
                  {sc.consecutive_failures}/{sc.max_consecutive_failures}
                </span>
              </td>
              <td className="nowrap">
                {sc.last_run_id ? (
                  sc.kind === 'flow' ? (
                    <Link to={`/runs/${sc.last_run_id}`}>{fmtTime(sc.last_run_at)} →</Link>
                  ) : (
                    <span>{fmtTime(sc.last_run_at)}</span>
                  )
                ) : (
                  <span className="muted">–</span>
                )}
              </td>
              <td className="nowrap muted">{fmtTime(sc.next_run_at)}</td>
              <td className="nowrap">
                <div className="row-actions">
                  <button
                    className="btn btn-sm"
                    disabled={busy === sc.id}
                    onClick={() => void actOn(sc.id, () => api.triggerSchedule(sc.id))}
                  >
                    立即执行
                  </button>
                  {sc.status === 'tripped' ? (
                    <button
                      className="btn btn-sm btn-primary"
                      disabled={busy === sc.id}
                      onClick={() => void actOn(sc.id, () => api.patchSchedule(sc.id, { reset_breaker: true }))}
                    >
                      重置熔断
                    </button>
                  ) : (
                    <button
                      className="btn btn-sm"
                      disabled={busy === sc.id}
                      onClick={() => void actOn(sc.id, () => api.patchSchedule(sc.id, { enabled: !sc.enabled }))}
                    >
                      {sc.enabled ? '暂停' : '恢复'}
                    </button>
                  )}
                  <button
                    className="btn btn-sm btn-danger"
                    disabled={busy === sc.id}
                    onClick={() => void actOn(sc.id, () => api.deleteSchedule(sc.id))}
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
