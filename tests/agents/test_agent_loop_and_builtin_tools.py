"""测试完善后的生产级 Agent Loop 系统：自动滚动压缩、上下文超限自愈、跨厂商协议异常兼容、内置工具集以及 Supervisor 实时纠偏与兜底接管。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from cuaflow.agents.loop import Tool, ToolAgent, ToolResult, _find_safe_tail_start
from cuaflow.llm.base import ContextOverflowError, FakeLLM, LLMResponse, parse_tool_args_resilient, tool_spec
from cuaflow.llm.chat_completions import ChatCompletionsLLM
from cuaflow.services import Services


def test_resilient_tool_args_and_chat_completions_compat(tmp_path: Path):
    """验证宽松工具参数解析、native 模式内联 <tool_call> 兜底恢复以及非多模态模型 400 自动降级重试。"""
    # 1. 尾随逗号、代码块包裹与 Python 单引号字典修复
    assert parse_tool_args_resilient('```json\n{"action": "click", "target": "提交",}\n```') == {
        "action": "click",
        "target": "提交",
    }
    assert parse_tool_args_resilient("{'action': 'read', 'path': 'notes.txt'}") == {
        "action": "read",
        "path": "notes.txt",
    }

    # 2. 模型不支持 image_url 返回 400 时自动降级 vision=False 重试，并识别正文中 <tool_call>
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = req.read().decode("utf-8")
        if calls["n"] == 1:
            assert "image_url" in body
            return httpx.Response(400, json={"error": {"message": "Invalid content: image_url is not supported by this model"}})
        assert "image_url" not in body
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '我来读取文件\n<tool_call>{"name": "file_ops", "arguments": {"action": "read", "path": "a.txt"}}</tool_call>',
                        }
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 15},
            },
        )

    llm = ChatCompletionsLLM(
        model="custom-text-llm",
        base_url="https://mock.local/v1",
        api_key="sk-test",
        vision=True,
        transport=httpx.MockTransport(handler),
    )
    tools = [tool_spec("file_ops", "file ops", {"action": {"type": "string"}, "path": {"type": "string"}})]
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "读取 a.txt"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    ]
    resp = llm.chat(msgs, tools=tools)
    assert llm.vision is False
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "file_ops"
    assert resp.tool_calls[0].arguments == {"action": "read", "path": "a.txt"}


def test_rolling_compaction_safe_boundary_and_context_overflow_recovery():
    """验证滚动压缩绝不切出孤立 tool 消息，且遇到 ContextOverflowError 时能自动紧急压缩并无损重试成功。"""
    sample_msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 2000},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "ok"},
    ]
    # max_tail=3 原本会落在索引 3（孤立 tool 消息），安全边界查找必须推进到索引 4（assistant）
    idx = _find_safe_tail_start(sample_msgs, min_prefix=2, max_tail=3)
    assert sample_msgs[idx]["role"] == "assistant"

    state = {"turn": 0, "overflow_triggered": False}

    class OverflowOnceLLM:
        vision = False

        def chat(self, messages, tools=None, json_mode=False, max_tokens=None):
            from cuaflow.llm.base import ToolCall

            if not tools:
                return LLMResponse(content="交接摘要：已完成前序准备，立即调用 done_tool 收尾。")
            state["turn"] += 1
            if state["turn"] == 1:
                return LLMResponse(tool_calls=[ToolCall(id="call_noop", name="noop", arguments={})])
            if state["turn"] == 2 and not state["overflow_triggered"]:
                state["overflow_triggered"] = True
                raise ContextOverflowError("HTTP 400: maximum context length exceeded (131072 tokens)")
            return LLMResponse(tool_calls=[ToolCall(id="call_done", name="done_tool", arguments={"ans": "RECOVERED"})])

    agent = ToolAgent(
        OverflowOnceLLM(),  # type: ignore[arg-type]
        "sys",
        [
            Tool(tool_spec("noop", "noop", {}), lambda a: ToolResult("A" * 3000)),
            Tool(tool_spec("done_tool", "done", {"ans": {"type": "string"}}), lambda a: ToolResult("ok", done=True, final=a)),
        ],
    )
    agent.pin_note("key1", "PERSISTED_VALUE")
    # 先预塞一条早期工具调用使消息数足够触发安全压缩
    res = agent.run("执行测试", turn_limit=6)
    assert res.done is True
    assert state["overflow_triggered"] is True
    assert res.final == {"ans": "RECOVERED"}
    assert "PERSISTED_VALUE" in agent.messages[2]["content"]


def test_builtin_tools_and_watchdog_supervisor_autonomous_rescue(tmp_path: Path):
    """验证内置工具（file_ops / memory_note / desktop_control）、连续报错唤醒监督者纠偏，以及无人值守超限后 Supervisor 自主兜底完成任务！"""
    watchdog_seen = {"called": False}
    op_calls = {"n": 0}

    def operator_fn(messages: list[dict[str, Any]], tools: list[dict] | None):
        if not tools:
            return "阶段摘要：已保存 note_k=42，但尚未写入最终 report.txt。"
        op_calls["n"] += 1
        n = op_calls["n"]
        full_txt = "\n".join(str(m.get("content", "")) for m in messages)
        if "监督者 Supervisor 实时巡检与环境自愈指导" in full_txt:
            watchdog_seen["called"] = True
        if n == 1:
            return {"tool": "memory_note", "args": {"action": "save", "key": "note_k", "value": 42}}
        if n == 2:
            return {"tool": "desktop_control", "args": {"action": "list_windows"}}
        # 故意连续触发 3 次文件读取不存在报错以唤醒看门狗，并耗尽 operator 自身轮数预算（turn_limit=5）而不调用 finish！
        return {"tool": "file_ops", "args": {"action": "read", "path": "missing_file.txt"}}

    def supervisor_advice_fn(messages: list[dict[str, Any]], tools: list[dict] | None):
        return "missing_file.txt 不存在，请改用 file_ops(action='write') 直接生成目标报告文件。"

    rescue_step = {"n": 0}

    def fallback_rescue_fn(messages: list[dict[str, Any]], tools: list[dict] | None):
        if not tools:
            return "兜底总结完成"
        rescue_step["n"] += 1
        if rescue_step["n"] == 1:
            return {
                "tool": "file_ops",
                "args": {"action": "write", "path": "final_report.txt", "content": "由监督者兜底生成的报告: 42"},
            }
        return {
            "tool": "complete_rescue",
            "args": {
                "accomplished": True,
                "summary": "监督者已成功接管现场并生成 final_report.txt",
                "output": {"report_file": "final_report.txt"},
            },
        }

    svc = Services(
        root=tmp_path / "lib",
        llm_override={
            "operator": FakeLLM(operator_fn),
            "supervisor": FakeLLM(supervisor_advice_fn),
            "fallback": FakeLLM(fallback_rescue_fn),
        },
        runner_opts={"desktop": "fake"},
        settings_file=tmp_path / "settings.json",
        start_worker=False,
    )
    try:
        run = svc.store.create_run("direct", {}, steps=[])
        # 显式设极小 turn_limit=5 且 unattended=True：旧架构必然报 operator did not finish: turn_limit，
        # 新架构会先触发看门狗唤醒 Supervisor 纠偏，随后在耗尽 5 轮时由 Supervisor.rescue_direct_task 自主兜底成功！
        res = svc.operator.execute(
            run["id"],
            "收集指标并写入 final_report.txt",
            unattended=True,
            turn_limit=5,
        )
        assert watchdog_seen["called"] is True, "连续 3 次报错应自动触发 Supervisor 实时看门狗纠偏"
        assert res["status"] == "succeeded", res
        assert res["output"]["note_k"] == 42
        assert res["output"]["report_file"] == "final_report.txt"
        assert "监督者已成功接管现场" in res["output"]["summary"]
        report_path = svc.store.run_dir(run["id"]) / "final_report.txt"
        assert report_path.read_text(encoding="utf-8") == "由监督者兜底生成的报告: 42"
    finally:
        svc.close()


def test_builtin_browser_tools_real_chromium_e2e(tmp_path: Path):
    """在真实 Chromium 浏览器上端到端验证 browser_goto、browser_interact（按中文按钮/占位符定位）与 browser_extract（表格提取）。"""
    import http.server
    import socketserver
    import threading

    html_page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>内置工具验收台</title></head>
<body>
  <h1>订单查询中心</h1>
  <input placeholder="输入客户姓名" id="q" />
  <button onclick="document.getElementById('res').innerText='查询结果：' + document.getElementById('q').value">立即查询</button>
  <p id="res">待查询</p>
  <table>
    <thead><tr><th>单号</th><th>金额</th></tr></thead>
    <tbody><tr><td>ORD-01</td><td>¥1280</td></tr><tr><td>ORD-02</td><td>¥3600</td></tr></tbody>
  </table>
</body></html>"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            data = html_page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    step = {"n": 0}

    def op_fn(messages: list[dict[str, Any]], tools: list[dict] | None):
        step["n"] += 1
        n = step["n"]
        if n == 1:
            return {"tool": "browser_goto", "args": {"url": f"http://127.0.0.1:{port}/"}}
        if n == 2:
            return {"tool": "browser_interact", "args": {"action": "fill", "target": "输入客户姓名", "value": "张三"}}
        if n == 3:
            return {"tool": "browser_interact", "args": {"action": "click", "target": "立即查询"}}
        if n == 4:
            return {"tool": "browser_extract", "args": {"mode": "table"}}
        if n == 5:
            return {"tool": "browser_extract", "args": {"mode": "summary"}}
        last_tool_txt = str(messages[-1].get("content") or "")
        return {
            "tool": "finish",
            "args": {
                "summary": "已通过内置浏览器工具完成查询与表格提取",
                "output": {"verified_zhangsan": "查询结果：张三" in last_tool_txt, "raw_tail": last_tool_txt[:300]},
            },
        }

    svc = Services(
        root=tmp_path / "lib",
        llm_override={"operator": FakeLLM(op_fn)},
        runner_opts={"desktop": "fake", "headless": True, "browser_session": str(tmp_path / "br" / "session.json")},
        settings_file=tmp_path / "settings.json",
        start_worker=False,
    )
    try:
        run = svc.store.create_run("direct", {}, steps=[])
        res = svc.operator.execute(run["id"], "打开订单查询中心，搜索张三并提取表格", unattended=True)
        assert res["status"] == "succeeded", res
        assert res["output"]["verified_zhangsan"] is True
    finally:
        from cuaflow.platform.browser import PersistentBrowser

        PersistentBrowser(tmp_path / "br" / "session.json").shutdown()
        svc.close()
        srv.shutdown()
