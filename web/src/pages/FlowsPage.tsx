import { Link } from 'react-router-dom';
import { api } from '../api';
import { Badge, StatusBadge } from '../components/StatusBadge';
import { Card, Empty, ErrorBox, Loading } from '../components/common';
import { fmtTime, pct } from '../lib/format';
import { useAsync } from '../lib/hooks';

export function FlowsPage() {
  const flows = useAsync(() => api.listFlows(), [], 10000);
  return (
    <div className="page">
      <div className="page-head">
        <h1>脚本库</h1>
        <p className="subtitle">录制得到的可复用步骤化脚本（Flow）。每次修复都会生成新版本，可随时回滚。</p>
      </div>
      <Card>
        {flows.loading && !flows.data ? (
          <Loading />
        ) : flows.error && !flows.data ? (
          <ErrorBox error={flows.error} onRetry={flows.reload} />
        ) : !flows.data?.length ? (
          <Empty>脚本库为空。提交任务后，系统会自动录制新脚本。</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>意图模板</th>
                  <th>当前版本</th>
                  <th>运行 / 成功率</th>
                  <th>修复</th>
                  <th>最近状态</th>
                  <th>副作用</th>
                </tr>
              </thead>
              <tbody>
                {flows.data.map((f) => (
                  <tr key={f.id}>
                    <td className="cell-main">
                      <Link to={`/flows/${f.id}`} className="strong">
                        {f.name || f.id}
                      </Link>
                      <div className="muted small">
                        <code>{f.id}</code>
                        {f.tags?.map((t) => (
                          <span key={t} className="tag">
                            {t}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="mono small clamp-2" title={f.intent_template}>
                      {f.intent_template}
                    </td>
                    <td>
                      <Badge tone="info">v{f.current_version}</Badge>
                    </td>
                    <td className="nowrap">
                      {f.stats.runs} / {pct(f.stats.successes, f.stats.runs)}
                    </td>
                    <td>{f.stats.repairs}</td>
                    <td className="nowrap">
                      <StatusBadge status={f.stats.last_status} pulse={false} />
                      {f.stats.last_run_at && <div className="muted small">{fmtTime(f.stats.last_run_at)}</div>}
                    </td>
                    <td>{f.side_effects ? <Badge tone="warning">有副作用</Badge> : <Badge tone="muted">只读</Badge>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
