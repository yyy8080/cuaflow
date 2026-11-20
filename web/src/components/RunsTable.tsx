import { Link } from 'react-router-dom';
import type { Run } from '../api';
import { StatusBadge, Badge } from './StatusBadge';
import { durationBetween, fmtDuration, fmtTime, shortJson } from '../lib/format';

export function RunsTable({ runs, showFlow = true }: { runs: Run[]; showFlow?: boolean }) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>执行</th>
            <th>状态</th>
            {showFlow && <th>脚本</th>}
            <th>步骤</th>
            <th>参数</th>
            <th>耗时</th>
            <th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => {
            const passed = r.steps.filter((s) => ['passed', 'repaired', 'skipped'].includes(s.status)).length;
            const repaired = r.steps.filter((s) => s.status === 'repaired' || s.repairs.length > 0).length;
            return (
              <tr key={r.id}>
                <td>
                  <Link to={`/runs/${r.id}`} className="mono">
                    {r.id}
                  </Link>
                  <div className="muted small">{r.kind === 'author' ? '录制' : '回放'}</div>
                </td>
                <td>
                  <StatusBadge status={r.status} />
                  {r.new_version !== undefined && r.new_version !== null && (
                    <div>
                      <Badge tone="success">→ v{r.new_version}</Badge>
                    </div>
                  )}
                </td>
                {showFlow && (
                  <td>
                    {r.flow_id ? (
                      <Link to={`/flows/${r.flow_id}`}>
                        {r.flow_id}
                        {r.version !== undefined && r.version !== null ? ` v${r.version}` : ''}
                      </Link>
                    ) : (
                      <span className="muted">–</span>
                    )}
                  </td>
                )}
                <td className="nowrap">
                  {passed}/{r.steps.length}
                  {repaired > 0 && <span className="muted small"> · 🩹{repaired}</span>}
                </td>
                <td className="mono small" title={JSON.stringify(r.params)}>
                  {shortJson(r.params, 48) || <span className="muted">–</span>}
                </td>
                <td className="nowrap">{fmtDuration(durationBetween(r.started_at, r.finished_at)) || '–'}</td>
                <td className="nowrap muted">{fmtTime(r.created_at)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
