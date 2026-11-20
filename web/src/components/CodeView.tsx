import { useMemo } from 'react';

/** 带行号的等宽字体源码查看器，支持按条件高亮指定行。 */
export function CodeView({
  source,
  highlight,
  maxHeight,
}: {
  source?: string;
  highlight?: (lineNo: number, text: string) => boolean;
  maxHeight?: number;
}) {
  const lines = useMemo(() => (source ?? '').replace(/\r\n/g, '\n').split('\n'), [source]);
  if (!source) return <div className="muted small">无源码</div>;
  return (
    <div className="code-view" style={maxHeight ? { maxHeight } : undefined}>
      <table>
        <tbody>
          {lines.map((t, i) => (
            <tr key={i} className={highlight?.(i + 1, t) ? 'hl' : undefined}>
              <td className="ln">{i + 1}</td>
              <td className="code">{t || ' '}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function JsonView({ value, empty = '–' }: { value: unknown; empty?: string }) {
  if (value === undefined || value === null || (typeof value === 'object' && !Object.keys(value as object).length)) {
    return <span className="muted">{empty}</span>;
  }
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}
