import json
import textwrap

from cuaflow.llm.base import FakeLLM
from cuaflow.services import Services

STEP1 = textwrap.dedent('''
    @flow.step("创建笔记文件", expect="文件存在")
    def create_note(c: Ctx):
        p = c.run_dir / f"{c.params.title}.md"
        p.write_text("", encoding="utf-8")
        c.state["path"] = str(p)
        expect.file_exists(p, min_size=0)
''')
STEP2_BAD = textwrap.dedent('''
    @flow.step("写入内容", expect="文件包含正文")
    def write_body(c: Ctx):
        expect.file_contains(c.state["path"], c.params.body, timeout=0.2)
''')
STEP2 = textwrap.dedent('''
    @flow.step("写入内容", expect="文件包含正文")
    def write_body(c: Ctx):
        from pathlib import Path
        Path(c.state["path"]).write_text(c.params.body, encoding="utf-8")
        expect.file_contains(c.state["path"], c.params.body)
        c.output["path"] = c.state["path"]
''')
PLAN = {
    "name": "写笔记", "description": "创建 markdown 笔记", "intent_template": "写一篇标题为{title}的笔记: {body}",
    "side_effects": False, "start_url": "",
    "params": {"title": {"type": "str", "description": "标题", "example": "日报"},
               "body": {"type": "str", "description": "正文", "example": "今天很好"}},
    "output": {"path": {"type": "str"}},
    "steps": [{"id": "create_note", "title": "创建笔记文件", "expect": "文件存在"},
              {"id": "write_body", "title": "写入内容", "expect": "文件包含正文"}],
}


def services(tmp_path, author_script, matcher_script=None):
    ok = FakeLLM(lambda m, t: {"passed": True, "reason": "ok"})
    llms = {"author": FakeLLM(author_script), "vision_judge": ok, "supervisor": ok,
            "repairer": FakeLLM([]), "matcher": FakeLLM(matcher_script or [])}
    return Services(root=tmp_path / "lib", llm_override=llms, runner_opts={"desktop": "fake"},
                    settings_file=tmp_path / "s.json", start_worker=False)


def test_author_records_then_task_reuses_flow(tmp_path):
    author_script = [
        {"tool": "finish", "args": {"final_response": "too early"}},           # gated: no plan
        {"tool": "plan_flow", "args": PLAN},
        {"tool": "try_step", "args": {"source": STEP1}},
        {"tool": "finish", "args": {"final_response": "too early"}},           # gated: write_body missing
        {"tool": "try_step", "args": {"source": STEP2_BAD}},                   # fails -> not committed
        {"tool": "observe", "args": {}},
        {"tool": "try_step", "args": {"source": STEP2}},
        {"tool": "finish", "args": {"final_response": "笔记已写好"}},
    ]
    svc = services(tmp_path, author_script, matcher_script=[
        {"verdict": "run", "flow_id": "__FLOW__", "params": {"title": "周报", "body": "本周完成"}, "reason": "same"}])
    task = svc.orchestrator.submit_task("写一篇标题为日报的笔记，内容是今天很好")
    task = svc.orchestrator.process_task(task["id"])
    assert task["status"] == "succeeded", task
    assert task["final_response"] == "笔记已写好"
    assert task["match"]["verdict"] == "new"
    flows = svc.store.list_flows()
    assert len(flows) == 1
    flow = flows[0]
    assert flow["name"] == "写笔记" and set(flow["params_schema"]) == {"title", "body"}
    src = svc.store.read_source(flow["id"], 1)
    assert "STEP2_BAD" not in src and 'write_text(c.params.body' in src
    runs = svc.store.list_runs()
    assert [r["kind"] for r in runs] == ["author"]  # default verify_replay=False avoids duplicate execution & tokens
    author_run = runs[0]
    st = {s["step_id"]: s for s in author_run["steps"]}
    assert st["write_body"]["status"] == "passed" and st["write_body"]["attempts"] == 2
    assert author_run["usage"]["llm_calls"] > 0

    # a similar task is routed to the existing flow with new params (no Author involved -> saves tokens!)
    svc.llms._override["matcher"].script[0]["flow_id"] = flow["id"]
    t2 = svc.orchestrator.submit_task("写一篇标题为周报的笔记：本周完成")
    t2 = svc.orchestrator.process_task(t2["id"])
    assert t2["status"] == "succeeded" and t2["match"]["verdict"] == "run"
    run = svc.store.get_run(t2["run_id"])
    assert run["kind"] == "flow" and run["output"]["path"].endswith("周报.md")
    assert run["usage"]["script_steps"] == 2 and run["usage"]["estimated_tokens_saved"] > 0


def test_author_optional_verify_replay(tmp_path):
    author_script = [
        {"tool": "plan_flow", "args": PLAN},
        {"tool": "try_step", "args": {"source": STEP1}},
        {"tool": "try_step", "args": {"source": STEP2}},
        {"tool": "finish", "args": {"final_response": "ok"}},
    ]
    svc = services(tmp_path, author_script)
    s = svc.get_settings()
    s.author.verify_replay = True
    svc.update_settings(s)
    task = svc.orchestrator.submit_task("写一篇标题为日报的笔记，内容是今天很好")
    task = svc.orchestrator.process_task(task["id"])
    assert task["status"] == "succeeded"
    assert sorted(r["kind"] for r in svc.store.list_runs()) == ["author", "flow"]


def test_matcher_demotes_run_when_params_missing(tmp_path):
    from cuaflow.library.match import match_task
    flows = [{"id": "f1", "name": "n", "intent_template": "x {a}", "params_schema": {"a": {"type": "str"}},
              "side_effects": False}]
    llm = FakeLLM([{"verdict": "run", "flow_id": "f1", "params": {}, "reason": "r"}])
    assert match_task(llm, flows, "do x")["verdict"] == "adapt"
    llm = FakeLLM([{"verdict": "run", "flow_id": "nope", "params": {}}])
    assert match_task(llm, flows, "do x")["verdict"] == "new"
    assert match_task(FakeLLM([]), [], "do x")["verdict"] == "new"
    llm = FakeLLM(["not json at all"])
    assert match_task(llm, flows, "do x")["verdict"] == "new"


def test_author_failure_marks_task_failed(tmp_path):
    svc = services(tmp_path, [{"content": "thinking"}] * 0 + ["no tools"] * 5)
    task = svc.orchestrator.submit_task("anything", force_new=True)
    task = svc.orchestrator.process_task(task["id"])
    assert task["status"] == "failed" and "no_tool_calls" in task["final_response"]
    assert json.dumps(svc.store.list_flows()) == "[]"
