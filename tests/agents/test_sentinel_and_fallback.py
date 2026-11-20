"""Tests for L1.5 Sentinel, Adaptive L2 escalation, Tier-2 Autonomous Fallback, output contract, checkpoint resume, and continuous schedules."""

from __future__ import annotations

import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from cuaflow.agents.sentinel import inspect_step, verify_output_contract
from cuaflow.config import Settings
from cuaflow.flow.client import RunnerClient
from cuaflow.flow.flow import load_flow
from cuaflow.llm.base import FakeLLM
from cuaflow.server import create_app
from cuaflow.services import Services


def test_l15_sentinel_blocks_http_500_and_captcha():
    step = {"id": "open_dash", "title": "打开仪表盘", "expect": "显示今日统计"}

    # Main-frame HTTP 500 -> immediate deterministic failure (0 tokens)
    r500 = {
        "step_id": "open_dash",
        "surfaces": ["browser"],
        "observation": {
            "url": "http://127.0.0.1/dash",
            "page_health": {"url": "http://127.0.0.1/dash", "is_error_page": False, "captcha_detected": False},
            "browser_telemetry": {"doc_status": 502, "doc_url": "http://127.0.0.1/dash"},
        },
    }
    sr = inspect_step(step, r500)
    assert not sr.passed and sr.blocker_kind == "http_error" and "502" in sr.detail

    # Passive CAPTCHA detection -> immediate safe stop (blocker_kind="captcha")
    rcap = {
        "step_id": "open_dash",
        "surfaces": ["browser"],
        "observation": {
            "url": "http://127.0.0.1/challenge",
            "page_health": {"url": "http://127.0.0.1/challenge", "is_error_page": False, "captcha_detected": True},
            "browser_telemetry": {"doc_status": 200},
        },
    }
    sr_cap = inspect_step(step, rcap)
    assert not sr_cap.passed and sr_cap.blocker_kind == "captcha"


def test_l15_adaptive_l2_wakes_judge_on_anomaly(tmp_path: Path):
    p = tmp_path / "f.py"
    flow_src = textwrap.dedent('''
        from cuaflow.flow import Ctx, Flow

        flow = Flow("modal anomaly demo", params={"modal": {"type": "bool", "default": False}}, output={"ok": "bool"})

        @flow.step("执行步骤", expect="界面正常无错误弹窗")
        def act(c: Ctx):
            if c.params.modal:
                c.desktop.add_window("System Error", modal=True)
            c.output["ok"] = True
    ''')
    p.write_text(flow_src, encoding="utf-8")

    judge_calls = []

    def judge_fn(messages, tools):
        text = str(messages[-1]["content"])
        judge_calls.append(text)
        if "出现新的桌面模态弹窗" in text:
            return {"passed": False, "reason": "System Error modal blocked UI"}
        return {"passed": True, "reason": "clean"}

    s = Settings()
    s.supervisor.l2_policy = "on_change"
    s.supervisor.l3_enabled = False
    s.supervisor.unattended = True
    s.supervisor.max_repair_attempts = 0
    s.supervisor.autonomous_fallback = False

    svc = Services(
        root=tmp_path / "lib",
        llm_override={"vision_judge": FakeLLM(judge_fn), "default": FakeLLM(lambda m, t: {"passed": True, "reason": "ok"})},
        runner_opts={"desktop": "fake"},
        settings_file=tmp_path / "s.json",
        start_worker=False,
    )
    svc.update_settings(s)
    flow = svc.store.create_flow(load_flow(p)[0].meta(), flow_src)

    # Run 1: clean -> L2 runs once because needs_verify=1 initially, then clears needs_verify=0
    r1 = svc.orchestrator.process_run(svc.orchestrator.submit_run(flow["id"], {"modal": False}, enqueue=False)["id"])
    assert r1["status"] == "succeeded"
    assert len(judge_calls) == 1
    assert svc.store.step_stats(flow["id"], "act")["needs_verify"] == 0

    # Run 2: clean -> needs_verify==0 so L2 is NOT called (0 tokens!)
    r2 = svc.orchestrator.process_run(svc.orchestrator.submit_run(flow["id"], {"modal": False}, enqueue=False)["id"])
    assert r2["status"] == "succeeded"
    assert len(judge_calls) == 1

    # Run 3: new unexpected modal appears -> L1.5 Sentinel flags anomaly -> Adaptive L2 wakes up & catches failure!
    r3 = svc.orchestrator.process_run(svc.orchestrator.submit_run(flow["id"], {"modal": True}, enqueue=False)["id"])
    assert r3["status"] == "failed"
    assert len(judge_calls) == 2
    checks = r3["steps"][0]["checks"]
    assert any(c["level"] == "L1.5" and "模态弹窗" in c["detail"] for c in checks)
    assert any(c["level"] == "L2" and not c["passed"] and "[adaptive-trigger]" in c["detail"] for c in checks)


def test_supervisor_tier2_autonomous_fallback_recovers_run(tmp_path: Path):
    flow_src = textwrap.dedent('''
        from pathlib import Path
        from cuaflow.flow import Ctx, Flow, expect

        flow = Flow("多步恢复测试", params={"token": "str"}, output={"secret": "str"})

        @flow.step("准备目录", expect="目录就绪")
        def prep(c: Ctx):
            c.state["target"] = str(c.run_dir / "auth_data.txt")

        @flow.step("读取受保护数据", expect="成功拿到数据写入 state")
        def fetch_protected(c: Ctx):
            p = Path(c.state["target"])
            expect.file_exists(p, timeout=0.1)
            c.state["secret"] = p.read_text(encoding="utf-8")

        @flow.step("汇总输出", expect="输出 secret")
        def finish_out(c: Ctx):
            c.output["secret"] = c.state["secret"]
    ''')
    p = tmp_path / "f.py"
    p.write_text(flow_src, encoding="utf-8")

    # Repairer gives up (pretending single-step patch can't generate external auth token file)
    repairer_llm = FakeLLM([{"tool": "give_up", "args": {"reason": "requires interactive multi-step auth detour"}}])
    # Tier-2 FallbackAgent interactively creates the missing auth file, updates c.state["secret"], and completes step!
    fallback_llm = FakeLLM([
        {"tool": "python", "args": {"code": "from pathlib import Path; Path(c.state['target']).write_text('SEC-' + c.params.token, encoding='utf-8')"}},
        {"tool": "complete_step", "args": {"summary": "generated auth_data.txt and populated secret",
                                           "state_updates": {"secret": "SEC-42"}}},
    ])
    ok_judge = FakeLLM(lambda m, t: {"passed": True, "reason": "ok"})
    s = Settings()
    s.supervisor.max_repair_attempts = 1
    s.supervisor.autonomous_fallback = True
    s.supervisor.unattended = True

    svc = Services(
        root=tmp_path / "lib",
        llm_override={"repairer": repairer_llm, "fallback": fallback_llm, "vision_judge": ok_judge, "supervisor": ok_judge},
        runner_opts={"desktop": "fake"},
        settings_file=tmp_path / "s.json",
        start_worker=False,
    )
    svc.update_settings(s)
    flow = svc.store.create_flow(load_flow(p)[0].meta(), flow_src)

    run = svc.orchestrator.submit_run(flow["id"], {"token": "42"}, enqueue=False)
    run = svc.orchestrator.process_run(run["id"])
    assert run["status"] == "succeeded", run.get("error")
    assert run["output"] == {"secret": "SEC-42"}
    step_map = {st["step_id"]: st for st in run["steps"]}
    assert step_map["fetch_protected"]["status"] == "repaired"
    assert any("[AI自主接管]" in (r.get("summary") or "") for r in step_map["fetch_protected"]["repairs"])
    assert svc.store.list_tickets("open") == []


def test_output_contract_and_checkpoint_resume(tmp_path: Path):
    ok, msg = verify_output_contract({"title": ""}, {"title": {"type": "str"}, "opt": {"type": "str", "required": False}})
    assert not ok and "title" in msg

    flow_src = textwrap.dedent('''
        from pathlib import Path
        from cuaflow.flow import Ctx, Flow

        flow = Flow("checkpoint resume", output={"val": "str"})

        @flow.step("step_one", expect="wrote marker 1")
        def step_one(c: Ctx):
            marker = Path(c.run_dir) / "step1_count.txt"
            n = int(marker.read_text(encoding="utf-8")) + 1 if marker.exists() else 1
            marker.write_text(str(n), encoding="utf-8")
            c.state["v"] = f"from_s1_{n}"

        @flow.step("step_two", expect="produced output")
        def step_two(c: Ctx):
            c.output["val"] = c.state["v"] + "_done"
    ''')
    p = tmp_path / "r.py"
    p.write_text(flow_src, encoding="utf-8")
    ok_judge = FakeLLM(lambda m, t: {"passed": True, "reason": "ok"})
    svc = Services(root=tmp_path / "lib", llm_override={"default": ok_judge}, runner_opts={"desktop": "fake"},
                   settings_file=tmp_path / "s.json", start_worker=False)
    flow = svc.store.create_flow(load_flow(p)[0].meta(), flow_src)
    run = svc.orchestrator.submit_run(flow["id"], {}, enqueue=False)
    run_dir = svc.store.run_dir(run["id"])

    # Simulate partial execution where step_one succeeded before crash
    with RunnerClient(log_path=run_dir / "runner.log") as rc:
        rc.load(p, {}, run_dir, desktop="fake")
        res1 = rc.run_step("step_one")
        assert res1["status"] == "passed"

    # Resume from checkpoint -> step_one is NOT re-executed (count stays 1), state is restored, step_two finishes!
    finished = svc.orchestrator.process_run(run["id"], resume_checkpoint=True)
    assert finished["status"] == "succeeded"
    assert finished["output"] == {"val": "from_s1_1_done"}
    assert (run_dir / "step1_count.txt").read_text(encoding="utf-8") == "1"


def test_continuous_schedules_and_circuit_breaker(tmp_path: Path):
    flow_src = textwrap.dedent('''
        from pathlib import Path
        from cuaflow.flow import Ctx, Flow, expect

        flow = Flow("monitor", params={"flag_file": "str"}, output={"ok": "bool"})

        @flow.step("check flag", expect="flag is healthy")
        def check_flag(c: Ctx):
            broken = Path(c.params.flag_file).exists()
            expect.that(not broken, "target service down")
            c.output["ok"] = True
    ''')
    p = tmp_path / "m.py"
    p.write_text(flow_src, encoding="utf-8")
    flag = tmp_path / "down.flag"

    s = Settings()
    s.supervisor.l2_policy = "off"
    s.supervisor.l3_enabled = False
    s.supervisor.max_repair_attempts = 0
    s.supervisor.autonomous_fallback = False

    svc = Services(root=tmp_path / "lib", llm_override={"default": FakeLLM([])}, runner_opts={"desktop": "fake"},
                   settings_file=tmp_path / "s.json", start_worker=False)
    svc.update_settings(s)
    flow = svc.store.create_flow(load_flow(p)[0].meta(), flow_src)
    client = TestClient(create_app(svc, "tok"))
    client.headers["Authorization"] = "Bearer tok"

    sc = client.post("/api/schedules", json={
        "name": "服务健康巡检",
        "interval_seconds": 10,
        "kind": "flow",
        "flow_id": flow["id"],
        "params": {"flag_file": str(flag)},
        "max_consecutive_failures": 2,
    }).json()
    assert sc["status"] == "active"

    # Healthy tick #1
    u1 = svc.orchestrator.process_schedule(sc["id"])
    assert u1["runs_count"] == 1 and u1["success_count"] == 1 and u1["status"] == "active"

    # Outage: 2 consecutive failures trip the circuit breaker!
    flag.write_text("down", encoding="utf-8")
    u2 = svc.orchestrator.process_schedule(sc["id"])
    assert u2["consecutive_failures"] == 1 and u2["status"] == "active"
    u3 = svc.orchestrator.process_schedule(sc["id"])
    assert u3["consecutive_failures"] == 2 and u3["status"] == "tripped" and u3["enabled"] is False

    stats = client.get("/api/stats").json()
    assert stats["schedules_tripped"] == 1 and stats["script_steps"] >= 1

    # Reset breaker via API
    reset = client.patch(f"/api/schedules/{sc['id']}", json={"reset_breaker": True}).json()
    assert reset["status"] == "active" and reset["consecutive_failures"] == 0 and reset["enabled"] is True
