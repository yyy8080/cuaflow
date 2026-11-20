import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, type Ticket, type TicketAction } from '../api';
import { StatusBadge } from '../components/StatusBadge';
import { Card, Empty, ErrorBox, Loading, Screenshot } from '../components/common';
import { notifyTicketsChanged } from '../lib/bus';
import { fmtTime } from '../lib/format';
import { useAsync } from '../lib/hooks';

const ACTION_LABEL: Record<TicketAction, string> = {
  continue: '已手动完成，继续',
  hint: '提示后重试',
  skip: '跳过此步',
  abort: '终止',
};

export function TicketsPage() {
  const [tab, setTab] = useState<'open' | 'resolved'>('open');
  const tickets = useAsync(() => api.listTickets(tab), [tab], 5000);

  return (
    <div className="page">
      <div className="page-head">
        <h1>人工介入</h1>
        <p className="subtitle">当监督者判定无法自动修复、或遇到需要确认的高风险步骤时，执行会暂停并在此生成工单。</p>
      </div>
      <Card
        actions={
          <div className="seg">
            <button className={tab === 'open' ? 'active' : ''} onClick={() => setTab('open')}>
              待处理
            </button>
            <button className={tab === 'resolved' ? 'active' : ''} onClick={() => setTab('resolved')}>
              已处理
            </button>
          </div>
        }
        title={tab === 'open' ? '待处理工单' : '已处理工单'}
      >
        {tickets.loading && !tickets.data ? (
          <Loading />
        ) : tickets.error && !tickets.data ? (
          <ErrorBox error={tickets.error} onRetry={tickets.reload} />
        ) : !tickets.data?.length ? (
          <Empty>{tab === 'open' ? '🎉 没有待处理的工单' : '暂无已处理的工单'}</Empty>
        ) : (
          <div className="tickets">
            {tickets.data.map((t) => (
              <TicketCard
                key={t.id}
                ticket={t}
                onResolved={() => {
                  notifyTicketsChanged();
                  void tickets.reload();
                }}
              />
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

function TicketCard({ ticket: t, onResolved }: { ticket: Ticket; onResolved: () => void }) {
  const [hintOpen, setHintOpen] = useState(false);
  const [hint, setHint] = useState('');
  const [busy, setBusy] = useState<TicketAction>();
  const [err, setErr] = useState<string>();

  const resolve = async (action: TicketAction) => {
    if (action === 'abort' && !confirm('确认终止此次执行？')) return;
    if (action === 'hint' && !hint.trim()) {
      setErr('请先填写给修复者的提示');
      return;
    }
    setBusy(action);
    setErr(undefined);
    try {
      await api.resolveTicket(t.id, action === 'hint' ? { action, hint: hint.trim() } : { action });
      onResolved();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(undefined);
    }
  };

  return (
    <div className={`ticket ticket-${t.status}`}>
      <div className="ticket-shot">
        <Screenshot runId={t.run_id} path={t.screenshot} thumb />
      </div>
      <div className="ticket-body">
        <div className="ticket-head">
          <StatusBadge status={t.status} pulse={false} />
          <Link to={`/runs/${t.run_id}`} className="mono">
            执行 {t.run_id}
          </Link>
          {t.flow_id && <Link to={`/flows/${t.flow_id}`}>📄 {t.flow_id}</Link>}
          {t.step_id && (
            <span className="small">
              步骤 <code>{t.step_id}</code>
            </span>
          )}
          <span className="grow" />
          <span className="muted small">{fmtTime(t.created_at)}</span>
        </div>
        <div className="ticket-reason">{t.reason}</div>
        {t.status === 'open' ? (
          <>
            <div className="ticket-actions">
              <button className="btn btn-primary btn-sm" disabled={!!busy} onClick={() => void resolve('continue')}>
                ✋ 我已手动完成，继续
              </button>
              <button className="btn btn-sm" disabled={!!busy} onClick={() => setHintOpen(!hintOpen)}>
                💡 给修复者提示后重试
              </button>
              <button className="btn btn-sm" disabled={!!busy} onClick={() => void resolve('skip')}>
                ⏭ 跳过此步
              </button>
              <button className="btn btn-danger btn-sm" disabled={!!busy} onClick={() => void resolve('abort')}>
                ■ 终止
              </button>
            </div>
            {hintOpen && (
              <div className="hint-box">
                <textarea
                  rows={3}
                  placeholder="例如：导出按钮在「更多」菜单里；文件名需要带日期后缀"
                  value={hint}
                  onChange={(e) => setHint(e.target.value)}
                />
                <button className="btn btn-primary btn-sm" disabled={!!busy || !hint.trim()} onClick={() => void resolve('hint')}>
                  {busy === 'hint' ? '提交中…' : '提交提示并重试'}
                </button>
              </div>
            )}
          </>
        ) : (
          t.resolution && (
            <div className="muted small">
              处理方式：{ACTION_LABEL[t.resolution.action] ?? t.resolution.action}
              {t.resolution.hint && <>（提示：{t.resolution.hint}）</>}
              {t.resolved_at && ` · ${fmtTime(t.resolved_at)}`}
            </div>
          )
        )}
        <ErrorBox error={err} />
      </div>
    </div>
  );
}
