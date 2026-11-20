import { useState, type ReactNode } from 'react';
import { artifactUrl } from '../api';

export function ErrorBox({ error, onRetry }: { error?: Error | string; onRetry?: () => void }) {
  if (!error) return null;
  const msg = typeof error === 'string' ? error : error.message;
  return (
    <div className="alert alert-danger">
      <span>⚠️ {msg}</span>
      {onRetry && (
        <button className="btn btn-sm" onClick={onRetry}>
          重试
        </button>
      )}
    </div>
  );
}

export function Loading({ text = '加载中…' }: { text?: string }) {
  return (
    <div className="loading">
      <span className="spinner" /> {text}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className ?? ''}`}>
      {(title || actions) && (
        <header className="card-head">
          <h3>{title}</h3>
          <div className="card-actions">{actions}</div>
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  );
}

/** 运行产物截图组件，点击可打开全尺寸灯箱预览。 */
export function Screenshot({ runId, path, thumb, alt = '截图' }: { runId: string; path?: string; thumb?: boolean; alt?: string }) {
  const [open, setOpen] = useState(false);
  const [failed, setFailed] = useState(false);
  if (!path) return <div className={`shot-empty ${thumb ? 'thumb' : ''}`}>无截图</div>;
  const src = artifactUrl(runId, path);
  if (failed) return <div className={`shot-empty ${thumb ? 'thumb' : ''}`}>截图加载失败</div>;
  return (
    <>
      <img
        className={thumb ? 'shot thumb' : 'shot'}
        src={src}
        alt={alt}
        loading="lazy"
        onClick={() => setOpen(true)}
        onError={() => setFailed(true)}
      />
      {open && (
        <div className="lightbox" onClick={() => setOpen(false)}>
          <img src={src} alt={alt} />
        </div>
      )}
    </>
  );
}
