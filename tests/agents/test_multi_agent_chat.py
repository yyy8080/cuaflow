"""测试对话式主控智能体 `CoordinatorAgent`、多子智能体真并行池 `SubagentPool`、中途转向与放行审批，以及单窗口桌面外壳。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from cuaflow.desktop import _open_standalone_webui
from cuaflow.llm.base import FakeLLM
from cuaflow.server import create_app
from cuaflow.services import Services


def _wait_until(pred, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def test_coordinator_dispatches_parallel_explorers_without_recording_flows(tmp_path: Path):
    """验证主智能体一次派发多个直连子智能体真并行执行、隔离浏览器槽位、不产生多余录制脚本并自动汇总汇报。"""
    barrier = threading.Barrier(2, timeout=10.0)

    def coord_fn(messages: list[dict[str, Any]], tools: list[dict] | None):
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        has_tool_result = messages[-1]["role"] == "tool"
        if "系统回调通知：本轮派发的并行子智能体已全部执行完毕" in str(last_user):
            return "汇总完毕：站点 A 返回 ALPHA_100，站点 B 返回 BETA_200。"
        if not has_tool_result:
            return {
                "tool": "dispatch_subagents",
                "args": {
                    "tasks": [
                        {
                            "name": "站点A调研",
                            "agent_kind": "explorer",
                            "instruction": "提取 ALPHA_100 指标",
                            "start_url": "https://a.example.com",
                        },
                        {
                            "name": "站点B调研",
                            "agent_kind": "explorer",
                            "instruction": "提取 BETA_200 指标",
                            "start_url": "https://b.example.com",
                        },
                    ]
                },
            }
        return "已并行派出站点A与站点B两个探索子智能体，请稍候。"

    def operator_fn(messages: list[dict[str, Any]], tools: list[dict] | None):
        first_prompt = str(messages[1]["content"])
        tag = "ALPHA_100" if "ALPHA_100" in first_prompt else "BETA_200"
        if messages[-1]["role"] != "tool":
            # 两个子智能体必须同时到达屏障才能继续，严格证明线程池真并行而非串行排队
            barrier.wait()
            return {"tool": "python", "args": {"code": f"c.output['val'] = {tag!r}\nprint({tag!r})"}}
        return {"tool": "finish", "args": {"summary": f"已完成提取 {tag}", "output": {"metric": tag}}}

    llms = {
        "coordinator": FakeLLM(coord_fn),
        "operator": FakeLLM(operator_fn),
    }
    svc = Services(
        root=tmp_path / "lib",
        llm_override=llms,
        runner_opts={"desktop": "fake"},
        settings_file=tmp_path / "settings.json",
        start_worker=True,
    )
    try:
        client = TestClient(create_app(svc, token="tok"))
        headers = {"Authorization": "Bearer tok"}

        chat = client.post("/api/chats", headers=headers, json={"title": "新对话"}).json()
        sid = chat["id"]

        r = client.post(
            f"/api/chats/{sid}/messages",
            headers=headers,
            json={"content": "同时帮我查一下站点A和站点B的核心指标", "wait": True},
        )
        assert r.status_code == 200
        assert "并行派出" in r.json()["reply"]["content"]

        # 等待两个并行子智能体全部执行成功，且主智能体自动回传汇总消息
        ok = _wait_until(
            lambda: len(svc.store.list_workers(session_id=sid)) == 2
            and all(w["status"] == "succeeded" for w in svc.store.list_workers(session_id=sid))
            and any("汇总完毕" in m["content"] for m in svc.store.list_chat_messages(sid)),
            timeout=20.0,
        )
        assert ok, f"workers={svc.store.list_workers(session_id=sid)}, msgs={svc.store.list_chat_messages(sid)}"

        # 直连探索子智能体绝不会在脚本库里生成多余的录制脚本
        assert svc.store.list_flows() == []

        # 验证并发执行期间分配了独立的 slot_1 浏览器槽位目录
        assert (tmp_path / "lib" / "browser_slots" / "slot_1").is_dir()
    finally:
        svc.close()


def test_mid_flight_steering_dynamically_adjusts_running_subagent(tmp_path: Path):
    """验证子智能体运行途中，用户通过主智能体对话下达中途调整指令可实时改变子智能体后续行为。"""
    step1_entered = threading.Event()
    allow_step2 = threading.Event()

    def coord_fn(messages: list[dict[str, Any]], tools: list[dict] | None):
        last_user = str(next((m["content"] for m in reversed(messages) if m["role"] == "user"), ""))
        if messages[-1]["role"] == "tool":
            return "已向子智能体实时注入调整要求。"
        if "改成提取 STEERED_999" in last_user:
            return {
                "tool": "steer_subagent",
                "args": {"worker_id": "all", "instruction": "立即改成提取 STEERED_999"},
            }
        if "系统回调通知" in last_user:
            return "调整后的任务已完成。"
        return {
            "tool": "dispatch_subagents",
            "args": {
                "tasks": [
                    {"name": "动态调整测试员", "agent_kind": "explorer", "instruction": "初始提取 ORIG_111"}
                ]
            },
        }

    def operator_fn(messages: list[dict[str, Any]], tools: list[dict] | None):
        full_text = "\n".join(str(m.get("content", "")) for m in messages)
        if "STEERED_999" in full_text:
            return {"tool": "finish", "args": {"summary": "已按中途调整提取 STEERED_999", "output": {"code": "STEERED_999"}}}
        if messages[-1]["role"] != "tool":
            return {"tool": "python", "args": {"code": "print('step1')"}}
        # 第 1 步工具执行完后等待主智能体注入中途转向指令
        step1_entered.set()
        allow_step2.wait(timeout=8.0)
        # 若第一轮返回普通思考文本触发下一轮迭代，ToolAgent 会在下一轮顶部抽取并注入 steering 指令
        return {"tool": "python", "args": {"code": "print('checking steering')"}}

    svc = Services(
        root=tmp_path / "lib",
        llm_override={"coordinator": FakeLLM(coord_fn), "operator": FakeLLM(operator_fn)},
        runner_opts={"desktop": "fake"},
        settings_file=tmp_path / "settings.json",
        start_worker=True,
    )
    try:
        sess = svc.store.create_chat_session("中途调整会话")
        sid = sess["id"]
        svc.coordinator.send_user_message(sid, "开始抓取初始指标", wait=True)

        assert step1_entered.wait(timeout=10.0), "子智能体未进入执行阶段"

        # 在子智能体运行途中发送对话调整指令
        svc.coordinator.send_user_message(sid, "中途调整：改成提取 STEERED_999", wait=True)
        allow_step2.set()

        ok = _wait_until(
            lambda: all(w["status"] == "succeeded" for w in svc.store.list_workers(session_id=sid)),
            timeout=15.0,
        )
        assert ok
        w = svc.store.list_workers(session_id=sid)[0]
        assert w["output"]["code"] == "STEERED_999"
        assert len(w["steering_log"]) == 1
        assert "STEERED_999" in w["steering_log"][0]["instruction"]
    finally:
        svc.close()


def test_subagent_approval_gate_and_conversational_release(tmp_path: Path):
    """验证子智能体关键操作调用 request_approval 挂起后，对话中推送放行卡片且可通过主智能体放行继续。"""

    def coord_fn(messages: list[dict[str, Any]], tools: list[dict] | None):
        last_user = str(next((m["content"] for m in reversed(messages) if m["role"] == "user"), ""))
        if messages[-1]["role"] == "tool":
            return "操作已执行。"
        if "批准放行" in last_user:
            return {
                "tool": "approve_subagent",
                "args": {"worker_id": "all", "action": "continue", "hint": "确认无误，请提交"},
            }
        if "系统回调通知" in last_user:
            return "审批放行后表单已顺利提交完成。"
        return {
            "tool": "dispatch_subagents",
            "args": [{"name": "表单提交员", "agent_kind": "explorer", "instruction": "填写并在提交前请求放行"}],
        }

    # 修正上面 args 结构：tasks 为列表
    def coord_fn_fixed(messages: list[dict[str, Any]], tools: list[dict] | None):
        res = coord_fn(messages, tools)
        if isinstance(res, dict) and res.get("tool") == "dispatch_subagents" and isinstance(res["args"], list):
            return {"tool": "dispatch_subagents", "args": {"tasks": res["args"]}}
        return res

    def operator_fn(messages: list[dict[str, Any]], tools: list[dict] | None):
        last = messages[-1]
        if last["role"] != "tool":
            return {"tool": "request_approval", "args": {"reason": "即将点击最终提交订单按钮，请确认放行"}}
        return {"tool": "finish", "args": {"summary": "放行后已完成订单提交", "output": {"submitted": True}}}

    svc = Services(
        root=tmp_path / "lib",
        llm_override={"coordinator": FakeLLM(coord_fn_fixed), "operator": FakeLLM(operator_fn)},
        runner_opts={"desktop": "fake"},
        settings_file=tmp_path / "settings.json",
        start_worker=True,
    )
    try:
        sess = svc.store.create_chat_session("审批放行测试")
        sid = sess["id"]
        svc.coordinator.send_user_message(sid, "帮我填写订单并在提交前等我放行", wait=True)

        # 等待子智能体进入 waiting_approval 状态并在聊天流产生放行卡片
        ok_wait = _wait_until(
            lambda: any(w["status"] == "waiting_approval" for w in svc.store.list_workers(session_id=sid))
            and any((m.get("meta") or {}).get("kind") == "approval_request" for m in svc.store.list_chat_messages(sid)),
            timeout=15.0,
        )
        assert ok_wait, "子智能体未进入 waiting_approval 状态或未推送对话放行卡片"

        # 用户直接在主智能体对话中说“批准放行”
        svc.coordinator.send_user_message(sid, "批准放行，继续执行吧", wait=True)

        ok_done = _wait_until(
            lambda: all(w["status"] == "succeeded" for w in svc.store.list_workers(session_id=sid)),
            timeout=15.0,
        )
        assert ok_done
        w = svc.store.list_workers(session_id=sid)[0]
        assert w["output"]["submitted"] is True
    finally:
        svc.close()


def test_single_window_standalone_webui_launcher_uses_app_shell(monkeypatch, tmp_path: Path):
    """验证桌面启动器在无 pywebview 时自动以独立无地址栏 `--app=...` 窗口启动 WebUI，不弹出多余控制器。"""
    spawned: dict[str, Any] = {}

    class DummyAppProc:
        def __init__(self, cmd, **kwargs):
            spawned["cmd"] = cmd

        def wait(self):
            time.sleep(0.05)
            return 0

    monkeypatch.setattr("cuaflow.desktop._find_app_shell_browser", lambda: "/fake/msedge")
    monkeypatch.setattr("subprocess.Popen", DummyAppProc)

    mode = _open_standalone_webui("http://127.0.0.1:8765/?token=abc", tmp_path / "data", wait=False)
    assert mode == "app_window_detached"
    assert spawned["cmd"][0] == "/fake/msedge"
    assert "--app=http://127.0.0.1:8765/?token=abc" in spawned["cmd"]
    assert any(a.startswith("--user-data-dir=") and "webui_shell" in a for a in spawned["cmd"])


def test_legacy_settings_auto_migrates_old_35_operator_turn_limit(tmp_path: Path):
    """验证旧版 v0.2.0 落盘的 operator_turn_limit=35 在升级加载时会自动迁移至 120，避免沿用旧上限报错。"""
    import json
    from cuaflow.config import load_settings

    sf = tmp_path / "legacy_settings.json"
    sf.write_text(
        json.dumps(
            {
                "models": {"default": {"api_key": "sk-keep-me", "model": "gpt-4o"}},
                "coordinator": {"max_parallel_workers": 4, "turn_limit": 12, "operator_turn_limit": 35},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings(sf)
    assert loaded.models.default.api_key == "sk-keep-me"
    assert loaded.coordinator.operator_turn_limit == 120
    assert loaded.coordinator.turn_limit == 24
    assert loaded.coordinator.max_auto_continues == 2


def test_operator_auto_compacts_and_continues_across_segments_without_failing(tmp_path: Path):
    """验证直连子智能体单阶段步数耗尽时，会自动压缩上下文并无缝跨分段续跑完成长任务，不会触发 turn_limit 失败。"""
    step_counter = {"n": 0}

    def operator_fn(messages: list[dict[str, Any]], tools: list[dict] | None):
        # 无工具列表时说明正在生成分段上下文压缩摘要
        if not tools:
            return f"已完成前 {step_counter['n']} 步计数，c.state['step'] 已更新，继续累加至 8 后结束。"
        step_counter["n"] += 1
        cur = step_counter["n"]
        if cur < 8:
            return {
                "tool": "python",
                "args": {"code": f"c.state['step'] = {cur}\nc.output['last_step'] = {cur}\nprint('step', {cur})"},
            }
        return {
            "tool": "finish",
            "args": {"summary": f"跨分段自动续跑完成共 {cur} 轮", "output": {"total_steps": cur}},
        }

    svc = Services(
        root=tmp_path / "lib",
        llm_override={"operator": FakeLLM(operator_fn)},
        runner_opts={"desktop": "fake"},
        settings_file=tmp_path / "settings.json",
        start_worker=False,
    )
    try:
        s = svc.get_settings()
        s.coordinator.operator_turn_limit = 5
        s.coordinator.max_auto_continues = 2
        svc.update_settings(s)

        run = svc.store.create_run("direct", {}, steps=[])
        res = svc.operator.execute(run["id"], "连续执行 8 轮操作并保持实时 runner 状态", unattended=True)
        assert res["status"] == "succeeded", res
        assert res["output"]["total_steps"] == 8
        # 验证跨分段压缩续跑过程中底层 RunnerClient 会话并未中断重建
        assert res["output"]["last_step"] == 7
    finally:
        svc.close()


def test_operator_interactive_turn_limit_pauses_for_release_then_continues(tmp_path: Path):
    """验证交互模式下即使自动续跑段数也耗尽，不会报错丢失现场，而是挂起等待用户放行继续追加轮数直至成功。"""
    step_counter = {"n": 0}

    def operator_fn(messages: list[dict[str, Any]], tools: list[dict] | None):
        if not tools:
            return "阶段性进展：已完成前 5 步，还差最后 1 步收尾。"
        step_counter["n"] += 1
        cur = step_counter["n"]
        if cur <= 5:
            return {"tool": "python", "args": {"code": f"c.output['progress'] = {cur}\nprint({cur})"}}
        return {"tool": "finish", "args": {"summary": "用户放行追加步数后顺利完成", "output": {"done_at": cur}}}

    svc = Services(
        root=tmp_path / "lib",
        llm_override={"operator": FakeLLM(operator_fn)},
        runner_opts={"desktop": "fake"},
        settings_file=tmp_path / "settings.json",
        start_worker=True,
    )
    try:
        s = svc.get_settings()
        s.coordinator.operator_turn_limit = 5
        s.coordinator.max_auto_continues = 0
        svc.update_settings(s)

        sess = svc.store.create_chat_session("超限续跑测试")
        sid = sess["id"]
        spawned = svc.pool.dispatch(
            sid,
            [{"name": "长流程探索员", "agent_kind": "explorer", "instruction": "执行 6 步长流程任务"}],
        )
        wid = spawned[0]["id"]

        # 满 5 轮后应进入 waiting_approval 挂起等待，而非直接报 turn_limit 失败
        ok_wait = _wait_until(
            lambda: (svc.store.get_worker(wid) or {}).get("status") == "waiting_approval",
            timeout=15.0,
        )
        assert ok_wait, f"worker={svc.store.get_worker(wid)}"

        # 用户点击放行继续追加步数
        svc.pool.approve(wid, action="continue")

        ok_done = _wait_until(
            lambda: (svc.store.get_worker(wid) or {}).get("status") == "succeeded",
            timeout=15.0,
        )
        assert ok_done, f"worker={svc.store.get_worker(wid)}"
        w = svc.store.get_worker(wid)
        assert w is not None
        assert w["output"]["progress"] == 5
        assert w["output"]["done_at"] == 6
    finally:
        svc.close()
