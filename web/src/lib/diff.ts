// Minimal unified-diff parser used by <DiffView>.

export type DiffLineKind = 'add' | 'del' | 'ctx' | 'hunk' | 'file' | 'meta';

export type DiffLine = {
  kind: DiffLineKind;
  text: string;
  oldNo?: number;
  newNo?: number;
};

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

/**
 * Parse a unified diff into typed lines with old/new line numbers.
 * Hunk header counts are used to know when a hunk ends, so that a deleted line whose
 * content starts with "-- " is not mistaken for a `--- file` header.
 */
export function parseUnifiedDiff(diff: string | undefined | null): DiffLine[] {
  if (!diff) return [];
  const lines = diff.replace(/\r\n/g, '\n').split('\n');
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  const out: DiffLine[] = [];
  let oldNo = 0;
  let newNo = 0;
  let oldLeft = 0; // remaining old-side lines in current hunk
  let newLeft = 0; // remaining new-side lines in current hunk
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
    // Lenient fallback for diffs with missing/incorrect hunk counts: still colour +/- lines.
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
