// 支持自动重连与指数退避的 WebSocket 事件流客户端。
import { useEffect, useRef, useState } from 'react';
import { wsUrl, type CuaEvent } from './api';

export type WsStatus = 'connecting' | 'open' | 'closed';

export type EventSocketOptions = {
  /** 仅订阅指定运行 ID 的事件；省略则订阅全部事件。 */
  runId?: string;
  onEvent: (ev: CuaEvent) => void;
  /** 每次连接（或重连）并完成订阅后触发，可用于通过 REST 接口补拉断线期间遗漏的事件。 */
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
        /* 忽略异常 */
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
        /* 忽略格式错误的消息帧 */
      }
    };
    ws.onerror = () => {
      /* 随后会触发 onclose */
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
 * 封装 EventSocket 的 React Hook。回调函数通过 ref 读取，因此在重渲染间更新回调不会触发重连；
 * 仅当 `runId` 或 `enabled` 变化时才会重新建立连接。
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
