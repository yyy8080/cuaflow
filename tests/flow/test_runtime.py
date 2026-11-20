import json
import textwrap
from pathlib import Path

import pytest

from cuaflow.flow.client import RunnerClient, RunnerTimeout
from cuaflow.flow.engine import latest_checkpoint_state
from cuaflow.flow.patching import (extract_steps, get_step_source, insert_step, render_flow_header,
                                   reorder_steps, replace_step, unified_diff, validate_step_source)

FLOW = textwrap.dedent('''
    from pathlib import Path
    from cuaflow.flow import Ctx, Flow, expect

    flow = Flow("demo", params={"name": "str", "n": {"type": "int", "default": 2}})

    @flow.step("write file", expect="file exists")
    def write(c: Ctx):
        p = c.run_dir / f"{c.params.name}.txt"
        p.write_text("x" * c.params.n)
        c.state["path"] = str(p)
        print("wrote", p)

    @flow.step("check content", expect="content has 3 x")
    def check(c: Ctx):
        expect.file_contains(c.state["path"], "xxx", timeout=0.2)
        c.output["ok"] = True

    @flow.step("boom")
    def boom(c: Ctx):
        raise RuntimeError("kaboom")
''')


@pytest.fixture()
def flow_file(tmp_path: Path) -> Path:
    p = tmp_path / "flow.py"
    p.write_text(FLOW)
    return p


def test_patching_roundtrip(flow_file):
    src = flow_file.read_text()
    assert [s.step_id for s in extract_steps(src)] == ["write", "check", "boom"]
    new_check = get_step_source(src, "check").replace('"xxx"', '"xx"')
    assert validate_step_source(new_check, "check") == "check"
    patched = replace_step(src, "check", new_check)
    assert '"xx"' in get_step_source(patched, "check")
    assert get_step_source(patched, "write") == get_step_source(src, "write")
    assert '-    expect.file_contains(c.state["path"], "xxx"' in unified_diff(src, patched)
    reordered = reorder_steps(patched, ["boom", "write", "check"])
    assert [s.step_id for s in extract_steps(reordered)] == ["boom", "write", "check"]
    with pytest.raises(ValueError):
        validate_step_source("def x(c):\n    pass\n")
    with pytest.raises(ValueError):
        validate_step_source(new_check, "other")


def test_header_and_insert(tmp_path):
    head = render_flow_header("t", params={"q": {"type": "str", "description": "query", "default": None}},
                              side_effects=True)
    src = insert_step(head, '@flow.step("s1")\ndef s1(c):\n    c.output["a"] = 1\n')
    p = tmp_path / "f.py"
    p.write_text(src)
    from cuaflow.flow import load_flow
    flow, _ = load_flow(p)
    assert flow.side_effects is True and [s.id for s in flow.steps] == ["s1"]


def test_runner_patch_retry_and_restart(flow_file, tmp_path):
    run_dir = tmp_path / "run"
    with RunnerClient(log_path=tmp_path / "runner.log") as rc:
        meta = rc.load(flow_file, {"name": "a"}, run_dir, desktop="fake")
        assert [s["id"] for s in meta["steps"]] == ["write", "check", "boom"]
        r1 = rc.run_step("write")
        assert r1["status"] == "passed" and "wrote" in r1["stdout"]
        assert r1["screenshot"] and (run_dir / r1["screenshot"]).is_file()
        r2 = rc.run_step("check")
        assert r2["status"] == "failed" and r2["error_kind"] == "expectation"
        # hot-patch the failing step and retry with live state preserved
        fixed = get_step_source(flow_file.read_text(), "check").replace('"xxx"', '"xx"')
        rc.replace_step(fixed, expected_id="check")
        r3 = rc.run_step("check")
        assert r3["status"] == "passed" and r3["attempt"] == 2
        r4 = rc.run_step("boom")
        assert r4["status"] == "failed" and r4["error_kind"] == "exception" and "kaboom" in r4["error"]
        ex = rc.exec("print(c.state['path'].endswith('a.txt'))")
        assert ex["ok"] and "True" in ex["stdout"]
        assert rc.finish()["output"] == {"ok": True}
        # restart from checkpoint: state restored, patched code is NOT (file unchanged)
        state, passed = latest_checkpoint_state(run_dir)
        assert passed == ["write", "check"] and state["path"].endswith("a.txt")
        rc.restart(state=state)
        assert rc.call("get_state")["path"] == state["path"]
    lines = (run_dir / "steps.jsonl").read_text().splitlines()
    assert len(lines) == 4 and json.loads(lines[1])["status"] == "failed"


def test_runner_timeout_then_restart(tmp_path):
    p = tmp_path / "slow.py"
    p.write_text(textwrap.dedent('''
        import time
        from cuaflow.flow import Flow
        flow = Flow("slow")
        @flow.step("sleep", timeout=1)
        def sleep(c):
            time.sleep(60)
        @flow.step("ok")
        def ok(c):
            c.output["done"] = 1
    '''))
    with RunnerClient() as rc:
        rc.load(p, {}, tmp_path / "run", desktop="fake")
        with pytest.raises(RunnerTimeout):
            rc.call("run_step", timeout=2, step_id="sleep")
        rc.restart(state={})
        assert rc.run_step("ok")["status"] == "passed"


def test_param_coercion(flow_file, tmp_path):
    with RunnerClient() as rc:
        meta = rc.load(flow_file, {"name": "b", "n": "3"}, tmp_path / "r", desktop="fake")
        assert meta["params"] == {"name": "b", "n": 3}
        assert rc.run_step("write")["status"] == "passed"
        assert rc.run_step("check")["status"] == "passed"
