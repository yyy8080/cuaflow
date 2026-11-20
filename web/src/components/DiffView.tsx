import { useMemo } from 'react';
import { diffStats, parseUnifiedDiff } from '../lib/diff';

/** 带增删着色与新旧行号展示的 unified diff 查看器。 */
export function DiffView({ diff, empty = '无差异', maxHeight }: { diff?: string | null; empty?: string; maxHeight?: number }) {
  const lines = useMemo(() => parseUnifiedDiff(diff), [diff]);
  if (!lines.length) return <div className="muted small">{empty}</div>;
  const { added, removed } = diffStats(lines);
  return (
    <div className="diff">
      <div className="diff-head">
        <span className="diff-add-count">+{added}</span>
        <span className="diff-del-count">−{removed}</span>
      </div>
      <div className="diff-body" style={maxHeight ? { maxHeight } : undefined}>
        <table>
          <tbody>
            {lines.map((l, i) => (
              <tr key={i} className={`dl dl-${l.kind}`}>
                <td className="ln">{l.oldNo ?? ''}</td>
                <td className="ln">{l.newNo ?? ''}</td>
                <td className="sign">{l.kind === 'add' ? '+' : l.kind === 'del' ? '−' : ''}</td>
                <td className="code">{l.text || ' '}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
