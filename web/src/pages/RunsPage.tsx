import { useSearchParams } from 'react-router-dom';
import { api } from '../api';
import { RunsTable } from '../components/RunsTable';
import { Card, Empty, ErrorBox, Loading } from '../components/common';
import { useAsync } from '../lib/hooks';

export function RunsPage() {
  const [sp, setSp] = useSearchParams();
  const flowId = sp.get('flow_id') || '';
  const flows = useAsync(() => api.listFlows(), []);
  const runs = useAsync(() => api.listRuns({ flow_id: flowId || undefined, limit: 100 }), [flowId], 4000);

  return (
    <div className="page">
      <div className="page-head">
        <h1>执行记录</h1>
        <p className="subtitle">所有录制与回放的执行。点击执行 ID 打开实时监控。</p>
      </div>
      <Card
        title="全部执行"
        actions={
          <>
            <select
              value={flowId}
              onChange={(e) => {
                const v = e.target.value;
                setSp(v ? { flow_id: v } : {});
              }}
            >
              <option value="">全部脚本</option>
              {flows.data?.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.name || f.id}
                </option>
              ))}
              {flowId && !flows.data?.some((f) => f.id === flowId) && <option value={flowId}>{flowId}</option>}
            </select>
            <button className="btn btn-sm" onClick={() => void runs.reload()}>
              刷新
            </button>
          </>
        }
      >
        {runs.loading && !runs.data ? (
          <Loading />
        ) : runs.error && !runs.data ? (
          <ErrorBox error={runs.error} onRetry={runs.reload} />
        ) : !runs.data?.length ? (
          <Empty>暂无执行记录。</Empty>
        ) : (
          <RunsTable runs={runs.data} />
        )}
      </Card>
    </div>
  );
}
