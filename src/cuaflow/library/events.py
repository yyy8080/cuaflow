"""事件总线：持久化事件并将其扇出广播给各订阅者（WebSocket、CLI 输出、测试）。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable

from cuaflow.library.store import Store

Subscriber = Callable[[dict[str, Any]], None]


class EventBus:
    def __init__(self, store: Store | None = None):
        self.store = store
        self._subs: list[Subscriber] = []
        self._lock = threading.Lock()
        self._seq = 0

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)

        return unsubscribe

    def publish(self, type: str, data: dict[str, Any] | None = None, **ids: Any) -> dict[str, Any]:  # noqa: A002
        ev: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "type": type,
                              "data": data or {}}
        for k in ("run_id", "task_id", "flow_id", "step_id"):
            if ids.get(k) is not None:
                ev[k] = ids[k]
        if self.store is not None:
            ev = self.store.add_event(ev)
        else:
            with self._lock:
                self._seq += 1
                ev["seq"] = self._seq
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 - 绝不能因订阅者异常中断事件发布方
                pass
        return ev
