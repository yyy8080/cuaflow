import { describe, expect, it } from 'vitest';
import { diffStats, parseUnifiedDiff } from './diff';

const SAMPLE = `--- a/flow.py
+++ b/flow.py
@@ -10,4 +10,5 @@ def step_open(ctx):
     page = ctx.page
-    page.click("#old")
+    page.click("#new")
+    page.wait_for_load_state()
     return True
-- trailing junk
`;

describe('parseUnifiedDiff', () => {
  it('returns [] for empty input', () => {
    expect(parseUnifiedDiff('')).toEqual([]);
    expect(parseUnifiedDiff(undefined)).toEqual([]);
  });

  it('classifies lines and numbers them', () => {
    const lines = parseUnifiedDiff(SAMPLE);
    expect(lines.map((l) => l.kind)).toEqual(['file', 'file', 'hunk', 'ctx', 'del', 'add', 'add', 'ctx', 'del']);
    const ctx = lines[3];
    expect(ctx).toMatchObject({ oldNo: 10, newNo: 10, text: '    page = ctx.page' });
    expect(lines[4]).toMatchObject({ kind: 'del', oldNo: 11, text: '    page.click("#old")' });
    expect(lines[5]).toMatchObject({ kind: 'add', newNo: 11 });
    expect(lines[6]).toMatchObject({ kind: 'add', newNo: 12 });
    expect(lines[7]).toMatchObject({ kind: 'ctx', oldNo: 12, newNo: 13 });
  });

  it('does not mistake a deleted "-- x" line inside a hunk for a file header', () => {
    const d = `@@ -1,2 +1,1 @@\n--- comment\n keep\n`;
    const lines = parseUnifiedDiff(d);
    expect(lines.map((l) => l.kind)).toEqual(['hunk', 'del', 'ctx']);
    expect(lines[1].text).toBe('-- comment');
  });

  it('handles multiple hunks and CRLF', () => {
    const d = '@@ -1 +1 @@\r\n-a\r\n+b\r\n@@ -5,0 +6,1 @@\r\n+c\r\n';
    const lines = parseUnifiedDiff(d);
    expect(lines.map((l) => l.kind)).toEqual(['hunk', 'del', 'add', 'hunk', 'add']);
    expect(lines[4]).toMatchObject({ newNo: 6, text: 'c' });
  });

  it('counts stats', () => {
    expect(diffStats(parseUnifiedDiff(SAMPLE))).toEqual({ added: 2, removed: 2 });
  });
});
