// 供 <DiffView> 使用的轻量级 unified diff 解析器。

export type DiffLineKind = 'add' | 'del' | 'ctx' | 'hunk' | 'file' | 'meta';

export type DiffLine = {
  kind: DiffLineKind;
  text: string;
  oldNo?: number;
  newNo?: number;
};

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

/**
 * 将 unified diff 解析为带类型与新旧行号的行数组。
 * 通过变更块头部（hunk header）的行数计数判断块边界，避免块内以 "-- " 开头的删除行被误判为 `--- file` 文件头。
 */
export function parseUnifiedDiff(diff: string | undefined | null): DiffLine[] {
  if (!diff) return [];
  const lines = diff.replace(/\r\n/g, '\n').split('\n');
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  const out: DiffLine[] = [];
  let oldNo = 0;
  let newNo = 0;
  let oldLeft = 0; // 当前变更块剩余的旧文件行数
  let newLeft = 0; // 当前变更块剩余的新文件行数
  for (const line of lines) {
    const inHunk = oldLeft > 0 || newLeft > 0;
    if (inHunk) {
      if (line.startsWith('+')) {
        out.push({ kind: 'add', text: line.slice(1), newNo: newNo++ });
        newLeft--;
      } else if (line.startsWith('-')) {
        out.push({ kind: 'del', text: line.slice(1), oldNo: oldNo++ });
        oldLeft--;
      } else if (line.startsWith('\\')) {
        out.push({ kind: 'meta', text: line });
      } else {
        out.push({ kind: 'ctx', text: line.startsWith(' ') ? line.slice(1) : line, oldNo: oldNo++, newNo: newNo++ });
        oldLeft--;
        newLeft--;
      }
      continue;
    }
    const m = HUNK_RE.exec(line);
    if (m) {
      oldNo = parseInt(m[1], 10);
      newNo = parseInt(m[3], 10);
      oldLeft = m[2] !== undefined ? parseInt(m[2], 10) : 1;
      newLeft = m[4] !== undefined ? parseInt(m[4], 10) : 1;
      out.push({ kind: 'hunk', text: line });
      continue;
    }
    if (line.startsWith('--- ') || line.startsWith('+++ ')) {
      out.push({ kind: 'file', text: line });
      continue;
    }
    if (line.startsWith('\\')) {
      out.push({ kind: 'meta', text: line });
      continue;
    }
    // 针对缺失或行数计数不准确的 diff 的宽松兜底处理：依然对 +/- 行进行着色分类。
    if (line.startsWith('+')) out.push({ kind: 'add', text: line.slice(1) });
    else if (line.startsWith('-')) out.push({ kind: 'del', text: line.slice(1) });
    else out.push({ kind: 'meta', text: line });
  }
  return out;
}

export function diffStats(lines: DiffLine[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const l of lines) {
    if (l.kind === 'add') added++;
    else if (l.kind === 'del') removed++;
  }
  return { added, removed };
}
