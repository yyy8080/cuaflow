import textwrap
import time

import pytest
from fastapi.testclient import TestClient

from cuaflow.flow.flow import load_flow
from cuaflow.llm.base import FakeLLM
from cuaflow.server import create_app
from cuaflow.services import Services

TOKEN = "t0k"
FLOW = textwrap.dedent('''
    from cuaflow.flow import Ctx, Flow, expect
    flow = Flow("api demo", params={"n": {"type": "int", "default": 1}}, output={"n": "int"})

    @flow.step("double", expect="n doubled")
    def double(c: Ctx):
        c.output["n"] = c.params.n * 2
        expect.that(c.output["n"] > 0, "positive")
''')


@pytest.fixture()
def env(tmp_path):
    ok = FakeLLM(lambda m, t: {"passed": True, "reason": "ok"})
    svc = Services(root=tmp_path / "lib", llm_override={"default": ok}, runner_opts={"desktop": "fake"},
                   settings_file=tmp_path / "s.json", start_worker=True)
    p = tmp_path / "f.py"
    p.write_text(FLOW)
    flow = svc.store.create_flow(load_flow(p)[0].meta(), FLOW)
    client = TestClient(create_app(svc, TOKEN))
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return svc, flow, client


def wait_run(client, run_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/runs/{run_id}").json()
        if r["status"] in ("succeeded", "failed", "aborted"):
            return r
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def test_auth_and_health(env):
    _, _, client = env
    assert TestClient(client.app).get("/api/flows").status_code == 401
    assert TestClient(client.app).get("/api/flows?token=wrong").status_code == 401
    assert TestClient(client.app).get(f"/api/flows?token={TOKEN}").status_code == 200
    assert TestClient(client.app).get("/api/health").json()["ok"] is True


def test_flow_run_events_artifacts(env):
    svc, flow, client = env
    flows = client.get("/api/flows").json()
    assert flows[0]["id"] == flow["id"] and flows[0]["params_schema"]["n"]["type"] == "int"
    detail = client.get(f"/api/flows/{flow['id']}").json()
    assert detail["versions"][0]["version"] == 1 and "source" not in detail["versions"][0]
    v1 = client.get(f"/api/flows/{flow['id']}/versions/1").json()
    assert "def double" in v1["source"] and v1["diff"] == ""

    with client.websocket_connect(f"/ws/events?token={TOKEN}") as ws:
        run = client.post(f"/api/flows/{flow['id']}/run", json={"params": {"n": 21}}).json()
        ws.send_json({"subscribe": {"run_id": run["id"]}})
        seen = set()
        deadline = time.time() + 60
        while "run_status:succeeded" not in seen and time.time() < deadline:
            ev = ws.receive_json()
            assert ev.get("run_id") in (None, run["id"])
            seen.add(ev["type"] + (":" + ev["data"].get("status", "") if ev["type"] == "run_status" else ""))
    assert {"step_start", "step_end", "check"} <= seen
    r = wait_run(client, run["id"])
    assert r["status"] == "succeeded" and r["output"] == {"n": 42}
    step = r["steps"][0]
    assert step["status"] == "passed" and step["screenshot"]
    img = client.get(f"/api/runs/{run['id']}/artifacts/{step['screenshot']}")
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
    assert client.get(f"/api/runs/{run['id']}/artifacts/../../cuaflow.db").status_code == 404
    evs = client.get(f"/api/runs/{run['id']}/events").json()
    assert evs and all(e["run_id"] == run["id"] for e in evs)
    assert client.get(f"/api/runs/{run['id']}/events?after_seq={evs[-1]['seq']}").json() == []
    assert client.get(f"/api/runs?flow_id={flow['id']}").json()[0]["id"] == run["id"]
    assert client.get("/api/flows").json()[0]["stats"]["successes"] == 1


def test_rollback_settings_tickets_spa(env):
    svc, flow, client = env
    svc.store.add_version(flow["id"], FLOW.replace("* 2", "* 3"), [{"id": "double", "title": "double"}],
                          author="repairer", reason="test", repaired_step="double")
    assert client.get(f"/api/flows/{flow['id']}").json()["current_version"] == 2
    assert '+    c.output["n"] = c.params.n * 3' in client.get(f"/api/flows/{flow['id']}/versions/2").json()["diff"]
    assert client.post(f"/api/flows/{flow['id']}/rollback", json={"version": 1}).json()["current_version"] == 1
    assert client.post(f"/api/flows/{flow['id']}/rollback", json={"version": 9}).status_code == 404

    s = client.get("/api/settings").json()
    s["supervisor"]["max_repair_attempts"] = 5
    s["models"]["roles"] = {"repairer": {"base_url": "http://x/v1", "model": "m", "api_key_env": "K"}}
    out = client.put("/api/settings", json=s).json()
    assert out["supervisor"]["max_repair_attempts"] == 5 and out["models"]["roles"]["repairer"]["model"] == "m"
    assert svc.get_settings().supervisor.max_repair_attempts == 5

    run = svc.store.create_run("flow", {}, flow_id=flow["id"], version=1)
    t = svc.tickets.open(run["id"], "help", step_id="double", flow_id=flow["id"])
    assert client.get("/api/tickets?status=open").json()[0]["id"] == t["id"]
    assert client.post(f"/api/tickets/{t['id']}/resolve", json={"action": "bogus"}).status_code == 400
    res = client.post(f"/api/tickets/{t['id']}/resolve", json={"action": "hint", "hint": "try X"}).json()
    assert res["status"] == "resolved" and res["resolution"] == {"action": "hint", "hint": "try X"}

    page = TestClient(client.app).get("/runs/some-id")
    assert page.status_code == 200 and "<html" in page.text.lower()
    assert TestClient(client.app).get("/api/nope", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404


def test_task_submission(env):
    svc, flow, client = env
    assert client.post("/api/tasks", json={"instruction": "  "}).status_code == 400
    svc.llms._override["matcher"] = FakeLLM([{"verdict": "run", "flow_id": flow["id"], "params": {"n": 5}}])
    t = client.post("/api/tasks", json={"instruction": "double 5"}).json()
    deadline = time.time() + 60
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{t['id']}").json()
        if t["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.2)
    assert t["status"] == "succeeded" and t["match"]["verdict"] == "run"
    assert client.get(f"/api/runs/{t['run_id']}").json()["output"] == {"n": 10}
    assert client.get("/api/tasks").json()[0]["id"] == t["id"]
