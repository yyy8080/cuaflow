"""Runner child process: ``python -m cuaflow.flow.proc``.

Line-delimited JSON-RPC over stdin/stdout. The original stdout fd is reserved for the protocol;
fd 1 is re-pointed at stderr so stray prints from steps / native libraries cannot corrupt it.

Request:  {"id": 1, "cmd": "run_step", "args": {...}}
Response: {"id": 1, "ok": true, "result": ...} | {"id": 1, "ok": false, "error": "...", "traceback": "..."}
"""

from __future__ import annotations

import json
import os
import sys
import traceback


def _make_factories(args: dict) -> dict:
    kwargs: dict = {}
    desktop = args.get("desktop", "native")
    if desktop == "fake":
        from cuaflow.platform.base import FakeDesktop

        shared = FakeDesktop()
        kwargs["desktop_factory"] = lambda: shared
    session_file = args.get("browser_session")
    if session_file or args.get("headless") is not None:
        def browser_factory():
            from cuaflow.platform.browser import PersistentBrowser

            kw = {}
            if session_file:
                kw["session_file"] = session_file
            if args.get("headless") is not None:
                kw["headless"] = bool(args["headless"])
            return PersistentBrowser.connect(**kw)

        kwargs["browser_factory"] = browser_factory
    return kwargs


def main() -> int:
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = os.fdopen(1, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)

    from cuaflow.flow.engine import FlowEngine

    engine: FlowEngine | None = None

    def reply(msg: dict) -> None:
        proto.write(json.dumps(msg, ensure_ascii=False, default=str) + "\n")
        proto.flush()

    reply({"id": 0, "ok": True, "result": {"ready": True, "pid": os.getpid()}})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            reply({"id": None, "ok": False, "error": "invalid JSON request"})
            continue
        rid, cmd, args = req.get("id"), req.get("cmd"), req.get("args") or {}
        try:
            if cmd == "load":
                if engine is not None:
                    engine.close()
                engine = FlowEngine(args["flow_path"], args.get("params") or {}, args["run_dir"],
                                    state=args.get("state"), ctx_factory_kwargs=_make_factories(args))
                result = engine.describe()
            elif cmd == "shutdown":
                if engine is not None:
                    engine.close()
                reply({"id": rid, "ok": True, "result": {"bye": True}})
                return 0
            elif cmd == "ping":
                result = {"pong": True}
            else:
                if engine is None:
                    raise RuntimeError("no flow loaded")
                if cmd == "run_step":
                    result = engine.run_step(args["step_id"], observe=args.get("observe", True))
                elif cmd == "check_pre":
                    result = engine.check_pre(args["step_id"])
                elif cmd == "replace_step":
                    result = engine.replace_step(args["source"], args.get("expected_id"))
                elif cmd == "exec":
                    result = engine.exec_code(args["code"])
                elif cmd == "observe":
                    result = engine.observation(surface=args.get("surface"), tag=args.get("tag", "obs"),
                                                detail=args.get("detail", True))
                elif cmd == "steps":
                    result = engine.steps
                elif cmd == "set_state":
                    engine.set_state(args.get("state") or {})
                    result = {"ok": True}
                elif cmd == "get_state":
                    result = engine.ctx.state_json()
                elif cmd == "finish":
                    result = engine.finish()
                else:
                    raise ValueError(f"unknown command {cmd!r}")
            reply({"id": rid, "ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001
            reply({"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                   "traceback": traceback.format_exc()[-4000:]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
