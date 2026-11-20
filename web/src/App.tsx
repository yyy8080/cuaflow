import { useEffect, useState } from 'react';
import { NavLink, Route, Routes } from 'react-router-dom';
import { api, getToken, onUnauthorized } from './api';
import { useAsync } from './lib/hooks';
import { ChatStudioPage } from './pages/ChatStudioPage';
import { TasksPage } from './pages/TasksPage';
import { FlowsPage } from './pages/FlowsPage';
import { FlowDetailPage } from './pages/FlowDetailPage';
import { RunsPage } from './pages/RunsPage';
import { RunPage } from './pages/RunPage';
import { SchedulesPage } from './pages/SchedulesPage';
import { TicketsPage } from './pages/TicketsPage';
import { SettingsPage } from './pages/SettingsPage';
import { TICKETS_CHANGED } from './lib/bus';

function Unauthorized() {
  const hasToken = !!getToken();
  return (
    <div className="auth-screen">
      <div className="auth-card">
        <div className="auth-logo">🔒</div>
        <h2>需要访问令牌</h2>
        <p>{hasToken ? '当前保存的访问令牌无效或已过期。' : '尚未提供访问令牌。'}</p>
        <p>
          请直接从桌面快捷方式启动 <code>cuaflow</code>，或打开服务端输出的完整鉴权地址（含 <code>?token=…</code> 查询参数）。令牌会自动保存在本机中。
        </p>
        <button className="btn btn-primary" onClick={() => window.location.reload()}>
          重新加载工作台
        </button>
      </div>
    </div>
  );
}

function TicketBadge() {
  const { data, reload } = useAsync(() => api.listTickets('open'), [], 5000);
  useEffect(() => {
    const h = () => void reload();
    window.addEventListener(TICKETS_CHANGED, h);
    return () => window.removeEventListener(TICKETS_CHANGED, h);
  }, [reload]);
  const n = data?.length ?? 0;
  return n > 0 ? <span className="nav-count">{n}</span> : null;
}

function ActiveWorkerBadge() {
  const { data } = useAsync(() => api.listWorkers({ active_only: true }), [], 4000);
  const n = data?.length ?? 0;
  return n > 0 ? <span className="nav-count nav-count-info">{n}</span> : null;
}

const THEME_KEY = 'cuaflow_theme';

function ThemeToggle() {
  const [theme, setTheme] = useState<string>(() => localStorage.getItem(THEME_KEY) || 'dark');
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);
  return (
    <button className="theme-toggle" title="切换深浅色主题" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>
      {theme === 'dark' ? '🌙' : '☀️'}
    </button>
  );
}

export function App() {
  const [unauth, setUnauth] = useState(false);
  useEffect(() => onUnauthorized(() => setUnauth(true)), []);
  if (unauth) return <Unauthorized />;

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-logo">🌊</span>
          <div>
            <div className="brand-name">cuaflow</div>
            <div className="brand-sub">多智能体工作台</div>
          </div>
        </div>
        <nav>
          <NavLink to="/" end>
            <span className="nav-ico">💬</span>对话工作台
            <ActiveWorkerBadge />
          </NavLink>
          <NavLink to="/tasks">
            <span className="nav-ico">📋</span>批量长任务
          </NavLink>
          <NavLink to="/flows">
            <span className="nav-ico">📚</span>脚本库
          </NavLink>
          <NavLink to="/schedules">
            <span className="nav-ico">⏱️</span>持续监控
          </NavLink>
          <NavLink to="/runs">
            <span className="nav-ico">▶️</span>执行记录
          </NavLink>
          <NavLink to="/tickets">
            <span className="nav-ico">🙋</span>审批工单
            <TicketBadge />
          </NavLink>
          <NavLink to="/settings">
            <span className="nav-ico">⚙️</span>设置
          </NavLink>
        </nav>
        <div className="sidebar-foot">
          <ThemeToggle />
        </div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<ChatStudioPage />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/flows" element={<FlowsPage />} />
          <Route path="/flows/:id" element={<FlowDetailPage />} />
          <Route path="/schedules" element={<SchedulesPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/runs/:id" element={<RunPage />} />
          <Route path="/tickets" element={<TicketsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<div className="page"><h1>页面不存在</h1></div>} />
        </Routes>
      </main>
    </div>
  );
}
