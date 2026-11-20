// 多智能体对话式工作台：主控智能体自然语言对话 + 并行子智能体实时观测、中途调整与一键放行。
import { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  api,
  artifactUrl,
  type AgentKind,
  type ChatMessage,
  type ChatSession,
  type CuaEvent,
  type GlobalStats,
  type TicketAction,
  type Worker,
} from '../api';
import { Badge } from '../components/StatusBadge';
import { ErrorBox } from '../components/common';
import { notifyTicketsChanged } from '../lib/bus';
import { AGENT_KIND_META, WORKER_STATUS_LABEL, fmtTime, fmtTokens, statusTone } from '../lib/format';
import { useEventSocket } from '../ws';

const STARTER_PROMPTS: Array<{ title: string; badge: string; desc: string; prompt: string }> = [
  {
    title: '多路浏览器并行调研',
    badge: '真并行 · 免录制',
    desc: '同时派发多个浏览器直连子智能体分别访问不同站点提取信息，互不抢占标签页并自动汇总对比。',
    prompt: '请同时派发两个浏览器探索子智能体并行工作：一个查看 Python 官网最新稳定版本号，另一个查看 Node.js 官网当前 LTS 版本号，完成后汇总对比给我。',
  },
  {
    title: '浏览器 + 本地桌面协同',
    badge: '跨端并行',
    desc: '一路子智能体查询网页资料，另一路桌面直连子智能体检查本机环境或整理写入本地文件。',
    prompt: '请并行派发两个子智能体：一个浏览器子智能体打开 example.com 提取页面标题与正文摘要，另一个桌面子智能体检查当前系统工作目录环境信息，最后合并汇报。',
  },
  {
    title: '关键步骤暂停等我放行',
    badge: '人机协同放行',
    desc: '让子智能体完成前置准备后在敏感操作前主动挂起，由你在对话或右侧面板一键放行或中途改向。',
    prompt: '派发一个浏览器子智能体打开表单页面准备填写信息，但在最终点击提交按钮之前先暂停请求我放行确认。',
  },
  {
    title: '编译可复用脚本并定时监控',
    badge: '省 Token 巡检',
    desc: '按需调度录制子智能体将高频流程沉淀为参数化脚本，后续由监督者低 Token 持续巡检并自动自愈。',
    prompt: '帮我派发脚本录制子智能体，把“打开目标网页并核对核心标题是否正常显示”录制成可重复复用的脚本，方便后续低 Token 定时巡检。',
  },
];

function isWorkerActive(status: string): boolean {
  return status === 'queued' || status === 'running' || status === 'waiting_approval';
}

function ApprovalCard({
  msg,
  worker,
  onResolve,
}: {
  msg: ChatMessage;
  worker?: Worker;
  onResolve: (workerId: string, action: TicketAction, hint?: string) => Promise<void>;
}) {
  const meta = msg.meta || {};
  const wid = meta.worker_id || '';
  const [hint, setHint] = useState('');
  const [busy, setBusy] = useState(false);
  const stillWaiting = worker ? worker.status === 'waiting_approval' : true;

  const act = async (action: TicketAction) => {
    if (!wid || busy) return;
    setBusy(true);
    try {
      await onResolve(wid, action, hint.trim() || undefined);
      setHint('');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`approval-card ${stillWaiting ? 'is-open' : 'is-resolved'}`}>
      <div className="approval-head">
        <span className="approval-title">
          ⏸ 子智能体放行请求 · <strong>{meta.worker_name || wid}</strong>
        </span>
        <Badge tone={stillWaiting ? 'warning' : 'success'}>{stillWaiting ? '等待放行' : '已处理'}</Badge>
      </div>
      <p className="approval-reason">{meta.reason || msg.content}</p>
      {meta.run_id && meta.screenshot && (
        <a
          href={artifactUrl(meta.run_id, meta.screenshot)}
          target="_blank"
          rel="noreferrer"
          className="approval-shot-wrap"
        >
          <img
            className="approval-shot"
            src={artifactUrl(meta.run_id, meta.screenshot)}
            alt="等待放行现场画面"
          />
        </a>
      )}
      {stillWaiting && wid && (
        <div className="approval-controls">
          <input
            type="text"
            placeholder="可选：附带补充指导说明（例如：改成点击右侧保存草稿）…"
            value={hint}
            onChange={(e) => setHint(e.target.value)}
            disabled={busy}
          />
          <div className="approval-btns">
            <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={() => void act(hint.trim() ? 'hint' : 'continue')}>
              ✓ {hint.trim() ? '附指导放行' : '批准放行'}
            </button>
            <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void act('skip')}>
              跳过此步
            </button>
            <button type="button" className="btn btn-danger btn-sm" disabled={busy} onClick={() => void act('abort')}>
              终止任务
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function WorkerCard({
  worker,
  highlighted,
  onSteer,
  onApprove,
  onAbort,
}: {
  worker: Worker;
  highlighted: boolean;
  onSteer: (workerId: string, instruction: string) => Promise<void>;
  onApprove: (workerId: string, action: TicketAction, hint?: string) => Promise<void>;
  onAbort: (workerId: string) => Promise<void>;
}) {
  const [steerText, setSteerText] = useState('');
  const [busy, setBusy] = useState(false);
  const active = isWorkerActive(worker.status);
  const kindMeta = AGENT_KIND_META[worker.agent_kind] || { label: worker.agent_kind, icon: '🤖', desc: '' };

  const submitSteer = async (e: FormEvent) => {
    e.preventDefault();
    const txt = steerText.trim();
    if (!txt || busy) return;
    setBusy(true);
    try {
      await onSteer(worker.id, txt);
      setSteerText('');
    } finally {
      setBusy(false);
    }
  };

  const summary =
    worker.output && typeof worker.output.summary === 'string' && worker.output.summary.trim()
      ? worker.output.summary.trim()
      : '';

  return (
    <article
      id={`worker-card-${worker.id}`}
      className={`worker-card status-${worker.status} ${highlighted ? 'is-highlighted' : ''}`}
    >
      <div className="worker-card-top">
        <div className="worker-title-row">
          <span className={`kind-chip kind-${worker.agent_kind}`} title={kindMeta.desc}>
            {kindMeta.icon} {kindMeta.label}
          </span>
          <strong className="worker-name" title={worker.name}>
            {worker.name}
          </strong>
        </div>
        <Badge tone={statusTone(worker.status)}>
          {WORKER_STATUS_LABEL[worker.status] ?? worker.status}
        </Badge>
      </div>

      <div className="worker-instruction" title={worker.instruction}>
        {worker.instruction}
      </div>

      {worker.run_id && worker.latest_screenshot && (
        <div className="worker-shot-box">
          <img
            src={artifactUrl(worker.run_id, worker.latest_screenshot)}
            alt={`${worker.name} 实时画面`}
            className="worker-shot"
          />
        </div>
      )}

      {worker.latest_note && (
        <div className="worker-note">
          <span className={`pulse-dot ${active ? 'is-live' : ''}`} />
          <span>{worker.latest_note}</span>
        </div>
      )}

      {!active && summary && summary !== worker.latest_note && (
        <div className="worker-summary">{summary}</div>
      )}

      {worker.error && worker.status !== 'succeeded' && (
        <div className="worker-error">{worker.error}</div>
      )}

      {worker.steering_log && worker.steering_log.length > 0 && (
        <div className="worker-steers">
          已动态调整 {worker.steering_log.length} 次：
          <span>“{worker.steering_log[worker.steering_log.length - 1].instruction}”</span>
        </div>
      )}

      {worker.status === 'waiting_approval' && (
        <div className="worker-approval-bar">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={busy}
            onClick={() => void onApprove(worker.id, 'continue')}
          >
            ✓ 立即放行
          </button>
          <button
            type="button"
            className="btn btn-sm"
            disabled={busy}
            onClick={() => void onApprove(worker.id, 'skip')}
          >
            跳过此步
          </button>
          <button
            type="button"
            className="btn btn-danger btn-sm"
            disabled={busy}
            onClick={() => void onApprove(worker.id, 'abort')}
          >
            终止
          </button>
        </div>
      )}

      {active && (
        <form className="worker-steer-form" onSubmit={(e) => void submitSteer(e)}>
          <input
            type="text"
            placeholder={worker.status === 'waiting_approval' ? '输入修改意见并放行…' : '中途调整指令，回车即时注入…'}
            value={steerText}
            onChange={(e) => setSteerText(e.target.value)}
            disabled={busy}
          />
          <button type="submit" className="btn btn-sm" disabled={busy || !steerText.trim()}>
            调整
          </button>
          {worker.status !== 'waiting_approval' && (
            <button
              type="button"
              className="btn btn-danger btn-sm"
              disabled={busy}
              title="终止此子智能体"
              onClick={() => void onAbort(worker.id)}
            >
              停止
            </button>
          )}
        </form>
      )}

      <div className="worker-foot">
        <span className="muted small">{fmtTime(worker.updated_at)}</span>
        <div className="worker-links">
          {worker.flow_id && (
            <Link to={`/flows/${worker.flow_id}`} className="small">
              脚本 {worker.flow_id}
            </Link>
          )}
          {worker.run_id && (
            <Link to={`/runs/${worker.run_id}`} className="small">
              执行轨迹 →
            </Link>
          )}
        </div>
      </div>
    </article>
  );
}

export function ChatStudioPage() {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeId, setActiveId] = useState<string>('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [scopeAllWorkers, setScopeAllWorkers] = useState(false);
  const [stats, setStats] = useState<GlobalStats | null>(null);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [highlightWorker, setHighlightWorker] = useState<string>('');
  const [quickModal, setQuickModal] = useState(false);
  const [quickKind, setQuickKind] = useState<AgentKind>('explorer');
  const [quickName, setQuickName] = useState('');
  const [quickInstr, setQuickInstr] = useState('');
  const [quickUrl, setQuickUrl] = useState('');

  const streamEndRef = useRef<HTMLDivElement | null>(null);
  const activeIdRef = useRef(activeId);
  activeIdRef.current = activeId;

  const loadSessionDetail = useCallback(async (sid: string) => {
    if (!sid) return;
    const detail = await api.getChat(sid);
    setSessions((prev) => {
      const exists = prev.some((x) => x.id === sid);
      return exists ? prev.map((x) => (x.id === sid ? detail : x)) : [detail, ...prev];
    });
    setMessages(detail.messages || []);
    if (!scopeAllWorkers) {
      setWorkers(detail.workers || []);
    }
  }, [scopeAllWorkers]);

  const boot = useCallback(async () => {
    setError(null);
    try {
      const [chatList, st] = await Promise.all([api.listChats(), api.stats().catch(() => null)]);
      if (st) setStats(st);
      setSessions(chatList);
      if (chatList.length > 0) {
        const targetId = activeIdRef.current && chatList.some((c) => c.id === activeIdRef.current)
          ? activeIdRef.current
          : chatList[0].id;
        setActiveId(targetId);
        await loadSessionDetail(targetId);
      } else {
        setActiveId('');
        setMessages([]);
        const globalW = await api.listWorkers({ limit: 30 }).catch(() => []);
        setWorkers(globalW);
      }
    } catch (e) {
      setError(e as Error);
    }
  }, [loadSessionDetail]);

  useEffect(() => {
    void boot();
  }, [boot]);

  useEffect(() => {
    if (!scopeAllWorkers && activeId) {
      void api.listWorkers({ session_id: activeId, limit: 60 }).then(setWorkers).catch(() => {});
    } else if (scopeAllWorkers) {
      void api.listWorkers({ limit: 60 }).then(setWorkers).catch(() => {});
    }
  }, [scopeAllWorkers, activeId]);

  const wsStatus = useEventSocket({
    onEvent: (ev: CuaEvent) => {
      const d = ev.data || {};
      if (ev.type === 'chat_message' && d.message) {
        const m = d.message as ChatMessage;
        if (m.session_id === activeIdRef.current) {
          setMessages((prev) => (prev.some((x) => x.id === m.id) ? prev : [...prev, m]));
        }
        void api.listChats().then(setSessions).catch(() => {});
      } else if (ev.type === 'chat_status' && d.session) {
        const s = d.session as ChatSession;
        setSessions((prev) => {
          const exists = prev.some((x) => x.id === s.id);
          return exists ? prev.map((x) => (x.id === s.id ? s : x)) : [s, ...prev];
        });
      } else if (ev.type === 'worker_updated' && d.worker) {
        const w = d.worker as Worker;
        if (scopeAllWorkers || !activeIdRef.current || w.session_id === activeIdRef.current) {
          setWorkers((prev) => {
            const exists = prev.some((x) => x.id === w.id);
            return exists ? prev.map((x) => (x.id === w.id ? w : x)) : [w, ...prev];
          });
        }
        if (w.status === 'waiting_approval' || w.status === 'running' || w.status === 'succeeded') {
          notifyTicketsChanged();
        }
        if (!isWorkerActive(w.status)) {
          void api.stats().then(setStats).catch(() => {});
        }
      }
    },
    onOpen: () => {
      if (activeIdRef.current) {
        void loadSessionDetail(activeIdRef.current).catch(() => {});
      }
    },
  });

  useEffect(() => {
    if (typeof streamEndRef.current?.scrollIntoView === 'function') {
      streamEndRef.current.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }
  }, [messages.length]);

  const activeSession = useMemo(
    () => sessions.find((s) => s.id === activeId) || null,
    [sessions, activeId],
  );
  const activeWorkersCount = useMemo(
    () => workers.filter((w) => isWorkerActive(w.status)).length,
    [workers],
  );
  const workerMap = useMemo(() => {
    const m = new Map<string, Worker>();
    for (const w of workers) m.set(w.id, w);
    return m;
  }, [workers]);

  const handleSelectSession = async (sid: string) => {
    setActiveId(sid);
    try {
      await loadSessionDetail(sid);
    } catch (e) {
      setError(e as Error);
    }
  };

  const handleNewChat = async () => {
    try {
      const created = await api.createChat('新对话');
      setSessions((prev) => [created, ...prev]);
      setActiveId(created.id);
      setMessages([]);
      if (!scopeAllWorkers) setWorkers([]);
    } catch (e) {
      setError(e as Error);
    }
  };

  const handleDeleteChat = async (sid: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await api.deleteChat(sid);
      const next = sessions.filter((x) => x.id !== sid);
      setSessions(next);
      if (activeId === sid) {
        if (next.length > 0) {
          setActiveId(next[0].id);
          await loadSessionDetail(next[0].id);
        } else {
          setActiveId('');
          setMessages([]);
          if (!scopeAllWorkers) setWorkers([]);
        }
      }
    } catch (err) {
      setError(err as Error);
    }
  };

  const sendMessage = async (rawText?: string) => {
    const text = (rawText ?? input).trim();
    if (!text || sending) return;
    setSending(true);
    setError(null);
    if (rawText === undefined) setInput('');
    try {
      let sid = activeId;
      if (!sid) {
        const created = await api.createChat(text.slice(0, 28));
        sid = created.id;
        setActiveId(sid);
        setSessions((prev) => [created, ...prev]);
      }
      const res = await api.sendChatMessage(sid, text, false);
      setMessages((prev) => (prev.some((x) => x.id === res.message.id) ? prev : [...prev, res.message]));
      setSessions((prev) => prev.map((x) => (x.id === sid ? { ...res.session, status: 'thinking' } : x)));
    } catch (e) {
      setError(e as Error);
      if (rawText === undefined) setInput(text);
    } finally {
      setSending(false);
    }
  };

  const onComposerKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void sendMessage();
    }
  };

  const handleSteerWorker = async (workerId: string, instruction: string) => {
    const updated = await api.steerWorker(workerId, instruction);
    setWorkers((prev) => prev.map((w) => (w.id === workerId ? updated : w)));
  };

  const handleApproveWorker = async (workerId: string, action: TicketAction, hint?: string) => {
    const updated = await api.approveWorker(workerId, { action, hint });
    setWorkers((prev) => prev.map((w) => (w.id === workerId ? updated : w)));
    notifyTicketsChanged();
  };

  const handleAbortWorker = async (workerId: string) => {
    const updated = await api.abortWorker(workerId);
    setWorkers((prev) => prev.map((w) => (w.id === workerId ? updated : w)));
    notifyTicketsChanged();
  };

  const handleQuickDispatch = async (e: FormEvent) => {
    e.preventDefault();
    if (!quickInstr.trim()) return;
    try {
      let sid = activeId;
      if (!sid) {
        const created = await api.createChat(quickName.trim() || quickInstr.trim().slice(0, 24));
        sid = created.id;
        setActiveId(sid);
        setSessions((prev) => [created, ...prev]);
      }
      const spawned = await api.dispatchWorkers({
        session_id: sid,
        tasks: [
          {
            name: quickName.trim() || AGENT_KIND_META[quickKind].label,
            agent_kind: quickKind,
            instruction: quickInstr.trim(),
            start_url: quickUrl.trim() || undefined,
          },
        ],
      });
      setWorkers((prev) => [...spawned, ...prev]);
      setQuickInstr('');
      setQuickName('');
      setQuickUrl('');
      setQuickModal(false);
    } catch (err) {
      setError(err as Error);
    }
  };

  const focusWorker = (wid: string) => {
    setHighlightWorker(wid);
    const el = document.getElementById(`worker-card-${wid}`);
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    setTimeout(() => setHighlightWorker((cur) => (cur === wid ? '' : cur)), 2200);
  };

  return (
    <div className="studio-shell">
      {/* 左侧栏：对话会话列表与全局省 Token 概览 */}
      <section className="studio-sessions" aria-label="对话会话列表">
        <div className="studio-sessions-head">
          <button
            id="btn-new-chat"
            type="button"
            className="btn btn-primary studio-new-btn"
            onClick={() => void handleNewChat()}
          >
            + 新建对话
          </button>
        </div>

        <div className="studio-session-list">
          {sessions.length === 0 ? (
            <div className="studio-session-empty muted small">
              发送第一条消息即可开启主智能体协作会话
            </div>
          ) : (
            sessions.map((s) => (
              <div
                key={s.id}
                role="button"
                tabIndex={0}
                className={`studio-session-item ${s.id === activeId ? 'is-active' : ''}`}
                onClick={() => void handleSelectSession(s.id)}
                onKeyDown={(e) => e.key === 'Enter' && void handleSelectSession(s.id)}
              >
                <div className="studio-session-main">
                  <span className="studio-session-title">{s.title || '新对话'}</span>
                  <span className="studio-session-meta muted small">
                    {s.status === 'thinking' ? '思考调度中…' : fmtTime(s.updated_at)}
                  </span>
                </div>
                <button
                  type="button"
                  className="studio-session-del"
                  title="删除会话"
                  onClick={(e) => void handleDeleteChat(s.id, e)}
                >
                  ×
                </button>
              </div>
            ))
          )}
        </div>

        {stats && (
          <div className="studio-telemetry">
            <div className="studio-tel-row">
              <span className="muted small">可复用脚本</span>
              <strong>{stats.flows} 个</strong>
            </div>
            <div className="studio-tel-row">
              <span className="muted small">累计省 Token</span>
              <strong className="tel-good">{fmtTokens(stats.estimated_tokens_saved)}</strong>
            </div>
            <div className="studio-tel-row">
              <span className="muted small">自动自愈修复</span>
              <strong>{stats.auto_repairs} 次</strong>
            </div>
          </div>
        )}
      </section>

      {/* 中央主区：主控智能体对话流 */}
      <section className="studio-chat" aria-label="主智能体对话流">
        <header className="studio-chat-head">
          <div>
            <h1>{activeSession?.title || '多智能体对话工作台'}</h1>
            <p className="subtitle">
              与主控智能体对话以拆解并派发多路并行子智能体；执行途中可随时发消息动态改向或一键放行。
            </p>
          </div>
          <div className="studio-head-actions">
            <span className={`ws-pill ws-${wsStatus}`}>
              {wsStatus === 'open' ? '● 实时连接' : wsStatus === 'connecting' ? '○ 连接中…' : '○ 重连中…'}
            </span>
            <button
              id="btn-quick-dispatch"
              type="button"
              className="btn btn-sm"
              onClick={() => setQuickModal((v) => !v)}
            >
              ⚡ 直派子智能体
            </button>
          </div>
        </header>

        {error && (
          <div className="studio-err-wrap">
            <ErrorBox error={error} onRetry={boot} />
          </div>
        )}

        {quickModal && (
          <form className="quick-dispatch-bar" onSubmit={(e) => void handleQuickDispatch(e)}>
            <div className="quick-dispatch-row">
              <select value={quickKind} onChange={(e) => setQuickKind(e.target.value as AgentKind)}>
                {(Object.keys(AGENT_KIND_META) as AgentKind[]).map((k) => (
                  <option key={k} value={k}>
                    {AGENT_KIND_META[k].icon} {AGENT_KIND_META[k].label}
                  </option>
                ))}
              </select>
              <input
                type="text"
                placeholder="子智能体名称（可选）"
                value={quickName}
                onChange={(e) => setQuickName(e.target.value)}
              />
              <input
                type="text"
                placeholder="起始 URL（可选，如 https://example.com）"
                value={quickUrl}
                onChange={(e) => setQuickUrl(e.target.value)}
              />
            </div>
            <div className="quick-dispatch-row">
              <input
                type="text"
                className="quick-instr-input"
                placeholder="输入给该子智能体的直接执行目标（立即投入后台并行池，无需等待）…"
                value={quickInstr}
                onChange={(e) => setQuickInstr(e.target.value)}
                required
              />
              <button type="submit" className="btn btn-primary btn-sm">
                立即并行启动
              </button>
              <button type="button" className="btn btn-sm" onClick={() => setQuickModal(false)}>
                收起
              </button>
            </div>
          </form>
        )}

        <div className="studio-messages">
          {messages.length === 0 ? (
            <div className="studio-welcome">
              <div className="studio-welcome-hero">
                <h2>你好，我是 cuaflow 主控智能体</h2>
                <p className="muted">
                  直接用自然语言交代目标：日常探索与多网页操作将默认派发<strong>直连浏览器 / 桌面子智能体</strong>多路并行完成；仅当你需要沉淀可重复复用的脚本时才会启动录制智能体。
                </p>
              </div>
              <div className="starter-grid">
                {STARTER_PROMPTS.map((item) => (
                  <button
                    key={item.title}
                    type="button"
                    className="starter-card"
                    onClick={() => void sendMessage(item.prompt)}
                  >
                    <div className="starter-card-head">
                      <strong>{item.title}</strong>
                      <span className="starter-tag">{item.badge}</span>
                    </div>
                    <p>{item.desc}</p>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((m) => {
              const isUser = m.role === 'user';
              const isApproval = m.meta?.kind === 'approval_request';
              const dispatched = m.meta?.dispatched_workers || [];
              return (
                <div key={m.id} className={`chat-row ${isUser ? 'is-user' : 'is-assistant'}`}>
                  <div className="chat-avatar" aria-hidden="true">
                    {isUser ? '你' : m.sender === 'subagent' ? '子' : '主'}
                  </div>
                  <div className="chat-bubble-wrap">
                    <div className="chat-meta muted small">
                      <span>{isUser ? '用户' : m.sender === 'subagent' ? '子智能体协同请求' : '主控智能体'}</span>
                      <span>{fmtTime(m.created_at)}</span>
                    </div>
                    {isApproval ? (
                      <ApprovalCard
                        msg={m}
                        worker={m.meta?.worker_id ? workerMap.get(m.meta.worker_id) : undefined}
                        onResolve={handleApproveWorker}
                      />
                    ) : (
                      <div className="chat-bubble">{m.content}</div>
                    )}
                    {dispatched.length > 0 && (
                      <div className="dispatched-strip">
                        <span className="muted small">已并行派发：</span>
                        {dispatched.map((dw) => {
                          const live = workerMap.get(dw.id);
                          const km = AGENT_KIND_META[dw.agent_kind] || { icon: '🤖', label: dw.agent_kind };
                          return (
                            <button
                              key={dw.id}
                              type="button"
                              className="dispatched-chip"
                              onClick={() => focusWorker(dw.id)}
                            >
                              <span>{km.icon}</span>
                              <strong>{dw.name}</strong>
                              {live && (
                                <Badge tone={statusTone(live.status)}>
                                  {WORKER_STATUS_LABEL[live.status] ?? live.status}
                                </Badge>
                              )}
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </div>
                </div>
              );
            })
          )}

          {activeSession?.status === 'thinking' && (
            <div className="chat-row is-assistant">
              <div className="chat-avatar" aria-hidden="true">主</div>
              <div className="chat-bubble chat-thinking">
                <span className="pulse-dot is-live" />
                主控智能体正在分析并调度子智能体…
              </div>
            </div>
          )}
          <div ref={streamEndRef} />
        </div>

        <form
          className="studio-composer"
          onSubmit={(e) => {
            e.preventDefault();
            void sendMessage();
          }}
        >
          <textarea
            id="chat-composer-input"
            rows={2}
            placeholder={
              activeWorkersCount > 0
                ? `当前有 ${activeWorkersCount} 个子智能体正在并行运行… 可随时输入中途调整指令（如“顺便抓取发布时间” / “批准放行”）或继续派发新任务`
                : '输入任务目标与主控智能体对话（Enter 发送，Shift+Enter 换行）…'
            }
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onComposerKeyDown}
          />
          <div className="studio-composer-foot">
            <span className="muted small">
              {activeWorkersCount > 0
                ? `⚡ ${activeWorkersCount} 个子智能体后台并发执行中，对话输入不阻塞`
                : '默认使用直连多智能体并行执行；需要保存可重复脚本时直接说明即可'}
            </span>
            <button
              id="btn-send-chat"
              type="submit"
              className="btn btn-primary"
              disabled={sending || !input.trim()}
            >
              {sending ? '发送中…' : '发送指令'}
            </button>
          </div>
        </form>
      </section>

      {/* 右侧栏：并行子智能体实时观测与干预面板 */}
      <aside className="studio-fleet" aria-label="并行子智能体池">
        <div className="studio-fleet-head">
          <div>
            <h2>并行子智能体池</h2>
            <span className="muted small">
              运行中 {activeWorkersCount} / 共 {workers.length}
            </span>
          </div>
          <div className="fleet-scope-tabs">
            <button
              type="button"
              className={`scope-btn ${!scopeAllWorkers ? 'is-active' : ''}`}
              onClick={() => setScopeAllWorkers(false)}
            >
              当前对话
            </button>
            <button
              type="button"
              className={`scope-btn ${scopeAllWorkers ? 'is-active' : ''}`}
              onClick={() => setScopeAllWorkers(true)}
            >
              全局
            </button>
          </div>
        </div>

        <div className="studio-fleet-list">
          {workers.length === 0 ? (
            <div className="fleet-empty">
              <div className="fleet-empty-title">暂无运行中的子智能体</div>
              <p className="muted small">
                在中间对话框向主控智能体下达任务后，派发的浏览器探索、桌面直连、脚本监督与录制子智能体将在此实时并行展示画面，并支持随时注入<strong>中途调整</strong>或<strong>放行确认</strong>。
              </p>
            </div>
          ) : (
            workers.map((w) => (
              <WorkerCard
                key={w.id}
                worker={w}
                highlighted={highlightWorker === w.id}
                onSteer={handleSteerWorker}
                onApprove={handleApproveWorker}
                onAbort={handleAbortWorker}
              />
            ))
          )}
        </div>
      </aside>
    </div>
  );
}
