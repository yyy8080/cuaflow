import textwrap
import threading
import time

import pytest

from cuaflow.config import Settings
from cuaflow.llm.base import FakeLLM
from cuaflow.services import Services

FLOW = textwrap.dedent('''
    from cuaflow.flow import Ctx, Flow, expect

    flow = Flow("写文件", params={"word": "str"}, output={"path": "str"}, intent="write {word} to a file")

    @flow.step("创建文件", expect="文件已创建")
    def create(c: Ctx):
        p = c.run_dir / "out.txt"
        p.write_text("", encoding="utf-8")
        c.state["path"] = str(p)

    @flow.step("写入单词", expect="文件包含单词")
    def write_word(c: Ctx):
        # BUG: wrong file name
        (c.run_dir / "wrong.txt").write_text(c.params.word, encoding="utf-8")
        expect.file_contains(c.state["path"], c.params.word, timeout=0.2)

    @flow.step("输出结果", expect="输出路径")
    def report(c: Ctx):
        c.output["path"] = c.state["path"]
''')

FIXED = textwrap.dedent('''
    @flow.step("写入单词", expect="文件包含单词")
    def write_word(c: Ctx):
        from pathlib import Path
        Path(c.state["path"]).write_text(c.params.word, encoding="utf-8")
        expect.file_contains(c.state["path"], c.params.word, timeout=1)
''')


def make_services(tmp_path, repairer_script, settings: Settings | None = None):
    judge = FakeLLM(lambda m, t: {"passed": True, "reason": "looks right"})
    llms = {"repairer": FakeLLM(repairer_script), "vision_judge": judge, "supervisor": judge,
            "default": FakeLLM(lambda m, t: {"passed": True, "reason": "ok"})}
    svc = Services(root=tmp_path / "lib", llm_override=llms, runner_opts={"desktop": "fake"},
                   settings_file=tmp_path / "settings.json", start_worker=False)
    if settings:
        svc.update_settings(settings)
    flow_src = FLOW
    from cuaflow.flow.flow import load_flow
    p = tmp_path / "seed.py"
    p.write_text(flow_src, encoding="utf-8")
    flow_obj, _ = load_flow(p)
    flow = svc.store.create_flow(flow_obj.meta(), flow_src)
    return svc, flow


def test_supervisor_repairs_and_versions(tmp_path):
    script = [
        {"tool": "observe", "args": {}, "content": "Let me look."},
        {"tool": "python", "args": {"code": "print(c.state)"}},
        {"tool": "try_step", "args": {"source": FIXED, "summary": "write to state path"}},
    ]
    svc, flow = make_services(tmp_path, script)
    events = []
    svc.bus.subscribe(events.append)
    run = svc.orchestrator.submit_run(flow["id"], {"word": "hello"}, enqueue=False)
    run = svc.orchestrator.process_run(run["id"])
    assert run["status"] == "succeeded", run.get("error")
    assert run["new_version"] == 2
    assert run["output"]["path"].endswith("out.txt")
    statuses = {s["step_id"]: s["status"] for s in run["steps"]}
    assert statuses == {"create": "passed", "write_word": "repaired", "report": "passed"}
    ww = next(s for s in run["steps"] if s["step_id"] == "write_word")
    assert ww["repairs"][0]["status"] == "succeeded" and "+    Path(c.state" in ww["repairs"][0]["diff"]
    assert any(c["level"] == "L1" and not c["passed"] for c in ww["checks"])
    v2 = svc.store.get_version(flow["id"], 2)
    assert v2["author"] == "repairer" and v2["repaired_step"] == "write_word" and "wrong.txt" not in v2["source"]
    assert svc.store.get_flow(flow["id"])["current_version"] == 2
    types = [e["type"] for e in events]
    for t in ("step_start", "step_end", "check", "repair_start", "repair_end", "version_created", "run_status"):
        assert t in types
    # next run on v2 passes with no repair
    run2 = svc.orchestrator.submit_run(flow["id"], {"word": "again"}, enqueue=False)
    run2 = svc.orchestrator.process_run(run2["id"])
    assert run2["status"] == "succeeded" and run2["new_version"] is None and run2["version"] == 2


def test_supervisor_escalates_to_human(tmp_path):
    script = [{"tool": "give_up", "args": {"reason": "needs a human"}}] * 10
    s = Settings()
    s.supervisor.max_repair_attempts = 1
    svc, flow = make_services(tmp_path, script, s)
    run = svc.orchestrator.submit_run(flow["id"], {"word": "x"}, enqueue=False)

    def operator():
        for _ in range(100):
            open_t = svc.store.list_tickets("open")
            if open_t:
                assert open_t[0]["step_id"] == "write_word"
                svc.tickets.resolve(open_t[0]["id"], "skip")
                return
            time.sleep(0.1)

    th = threading.Thread(target=operator)
    th.start()
    run = svc.orchestrator.process_run(run["id"])
    th.join()
    assert run["status"] == "succeeded"
    assert {s["step_id"]: s["status"] for s in run["steps"]}["write_word"] == "skipped"
    assert "skipped" in (run.get("error") or "")
    assert run["new_version"] is None


def test_l2_judge_failure_triggers_repair(tmp_path):
    # judge says the 'report' step is wrong once, repairer re-applies it
    calls = {"n": 0}

    def judge(m, t):
        text = str(m[-1]["content"])
        if "输出结果" in text and calls["n"] == 0:
            calls["n"] += 1
            return {"passed": False, "reason": "no output visible"}
        return {"passed": True, "reason": "ok"}

    report_src = textwrap.dedent('''
        @flow.step("输出结果", expect="输出路径")
        def report(c: Ctx):
            c.output["path"] = c.state["path"]
            print("path", c.state["path"])
    ''')
    svc, flow = make_services(tmp_path, [{"tool": "try_step", "args": {"source": FIXED}},
                                         {"tool": "try_step", "args": {"source": report_src}}])
    svc.llms._override["vision_judge"] = FakeLLM(judge)
    run = svc.orchestrator.submit_run(flow["id"], {"word": "w"}, enqueue=False)
    run = svc.orchestrator.process_run(run["id"])
    assert run["status"] == "succeeded", run.get("error")
    st = {s["step_id"]: s for s in run["steps"]}
    assert st["report"]["status"] == "repaired"
    assert any(c["level"] == "L2" and not c["passed"] for c in st["report"]["checks"])
    assert svc.store.get_version(flow["id"], 2)["repaired_step"] == "write_word,report"


def test_abort(tmp_path):
    svc, flow = make_services(tmp_path, [])
    run = svc.orchestrator.submit_run(flow["id"], {"word": "x"}, enqueue=False)
    svc.orchestrator.abort(run["id"])
    assert svc.store.get_run(run["id"])["status"] == "aborted"


@pytest.mark.parametrize("policy,expected_l2", [("off", False), ("always", True)])
def test_l2_policy(tmp_path, policy, expected_l2):
    s = Settings()
    s.supervisor.l2_policy = policy
    svc, flow = make_services(tmp_path, [{"tool": "try_step", "args": {"source": FIXED}}], s)
    run = svc.orchestrator.submit_run(flow["id"], {"word": "x"}, enqueue=False)
    run = svc.orchestrator.process_run(run["id"])
    create = next(x for x in run["steps"] if x["step_id"] == "create")
    assert any(c["level"] == "L2" for c in create["checks"]) is expected_l2
