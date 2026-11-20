import { useCallback, useEffect, useRef, useState } from 'react';

export type AsyncState<T> = {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  reload: () => Promise<void>;
  setData: (d: T) => void;
};

/**
 * Load data with `fn`, optionally re-polling every `intervalMs` (polling pauses while the tab
 * is hidden). `deps` changes trigger an immediate reload.
 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[], intervalMs?: number): AsyncState<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<Error>();
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const gen = useRef(0);

  const reload = useCallback(async () => {
    const my = ++gen.current;
    try {
      const d = await fnRef.current();
      if (my === gen.current) {
        setData(d);
        setError(undefined);
      }
    } catch (e) {
      if (my === gen.current) setError(e as Error);
    } finally {
      if (my === gen.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    void reload();
    if (!intervalMs) return;
    const t = setInterval(() => {
      if (!document.hidden) void reload();
    }, intervalMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, intervalMs]);

  return { data, error, loading, reload, setData };
}

/** Returns a debounced version of `fn` (trailing edge). */
export function useDebounced(fn: () => void, ms: number): () => void {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const t = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (t.current) clearTimeout(t.current);
    },
    [],
  );
  return useCallback(() => {
    if (t.current) clearTimeout(t.current);
    t.current = setTimeout(() => fnRef.current(), ms);
  }, [ms]);
}

/** Re-render every `ms` (for live durations). */
export function useTick(ms: number, enabled = true): number {
  const [n, setN] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    const t = setInterval(() => setN((x) => x + 1), ms);
    return () => clearInterval(t);
  }, [ms, enabled]);
  return n;
}
