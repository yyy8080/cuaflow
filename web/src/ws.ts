// WebSocket event stream with auto-reconnect + exponential backoff.
import { useEffect, useRef, useState } from 'react';
import { wsUrl, type CuaEvent } from './api';

export type WsStatus = 'connecting' | 'open' | 'closed';

export type EventSocketOptions = {
  /** Restrict to one run; omit for all events. */
  runId?: string;
  onEvent: (ev: CuaEvent) => void;
  /** Called after every (re)connect + subscribe, e.g. to backfill missed events via REST. */
  onOpen?: () => void;
  onStatus?: (s: WsStatus) => void;
};

export class EventSocket {
  private ws: WebSocket | null = null;
  private retry = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  constructor(private opts: EventSocketOptions) {}

  start(): void {
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.onerror = null;
      this.ws.onmessage = null;
      this.ws.onopen = null;
      try {
        this.ws.close();
      } catch {
        /* ignore */
      }
    }
    this.ws = null;
  }

  private connect(): void {
    if (this.stopped) return;
    this.opts.onStatus?.('connecting');
    let ws: WebSocket;
    try {
      ws = new WebSocket(wsUrl());
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      const sub = this.opts.runId ? { run_id: this.opts.runId } : {};
      ws.send(JSON.stringify({ subscribe: sub }));
      this.opts.onStatus?.('open');
      this.opts.onOpen?.();
    };
    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(typeof msg.data === 'string' ? msg.data : '');
        const list: CuaEvent[] = Array.isArray(data) ? data : [data];
        for (const ev of list) if (ev && typeof ev.seq === 'number' && ev.type) this.opts.onEvent(ev);
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onerror = () => {
      /* onclose follows */
    };
    ws.onclose = () => {
      this.ws = null;
      this.opts.onStatus?.('closed');
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    const delay = Math.min(15000, 500 * 2 ** this.retry) + Math.random() * 300;
    this.retry++;
    this.timer = setTimeout(() => this.connect(), delay);
  }
}

/**
 * React hook wrapping EventSocket. Handlers are read through a ref so they can change
 * between renders without reconnecting; only `runId` / `enabled` changes reconnect.
 */
export function useEventSocket(opts: EventSocketOptions & { enabled?: boolean }): WsStatus {
  const [status, setStatus] = useState<WsStatus>('connecting');
  const ref = useRef(opts);
  ref.current = opts;
  const enabled = opts.enabled ?? true;
  useEffect(() => {
    if (!enabled) return;
    const sock = new EventSocket({
      runId: opts.runId,
      onEvent: (ev) => ref.current.onEvent(ev),
      onOpen: () => ref.current.onOpen?.(),
      onStatus: (s) => {
        setStatus(s);
        ref.current.onStatus?.(s);
      },
    });
    sock.start();
    return () => sock.stop();
  }, [opts.runId, enabled]);
  return status;
}
