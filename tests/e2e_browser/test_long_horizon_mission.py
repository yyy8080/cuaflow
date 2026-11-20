"""End-to-end real-Chromium tests for long-horizon autonomous mission planning, batch skill compilation,
token savings, browser hygiene (JS dialogs + tab pruning), L1.5 Sentinel + Tier-2 Fallback recovery,
and checkpoint resume."""

from __future__ import annotations

import http.server
import json
import textwrap
import threading
import urllib.parse
from pathlib import Path

import pytest

from cuaflow.config import Settings
from cuaflow.flow.flow import load_flow
from cuaflow.llm.base import FakeLLM
from cuaflow.services import Services

pytestmark = pytest.mark.browser

ORDERS = [
    {"id": "ORD-101", "amount": 100},
    {"id": "ORD-102", "amount": 200},
    {"id": "ORD-103", "amount": 300},
    {"id": "ORD-104", "amount": 400},
    {"id": "ORD-105", "amount": 500},
    {"id": "ORD-106", "amount": 600},
]


class PortalState:
    def __init__(self) -> None:
        self.logged_in = False
        self.verified: set[str] = set()
        self.order_hits: dict[str, int] = {}
        self.dashboard_500 = False
        self.require_login_for_dash = True


def start_portal(state: PortalState):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            return

        def _html(self, body: str, status: int = 200, headers: dict[str, str] | None = None):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            cookie = self.headers.get("Cookie") or ""
            return "sid=ok" in cookie and state.logged_in

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8")
            form = urllib.parse.parse_qs(raw)
            if self.path == "/login":
                if form.get("user", [""])[0] == "admin" and form.get("pw", [""])[0] == "pass123":
                    state.logged_in = True
                    self.send_response(302)
                    self.send_header("Set-Cookie", "sid=ok; Path=/")
                    self.send_header("Location", "/orders?page=1")
                    self.end_headers()
                    return
                self._html("<h1>登录失败</h1>", status=401)
                return
            if self.path.startswith("/verify/"):
                oid = self.path.split("/verify/", 1)[1]
                state.verified.add(oid)
                self._html(f"<html><body><p id='done'>已核验 {oid}</p></body></html>")
                return
            self._html("not found", 404)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            if path == "/login":
                self._html(
                    "<!doctype html><html><head><title>登录后台</title></head><body>"
                    "<h1>系统登录</h1>"
                    "<form method='POST' action='/login'>"
                    "<label>账号 <input name='user' aria-label='账号'></label>"
                    "<label>密码 <input type='password' name='pw' aria-label='密码'></label>"
                    "<button type='submit'>登录</button></form></body></html>"
                )
                return

            if path == "/dashboard":
                if state.dashboard_500:
                    self._html("<html><head><title>500 Error</title></head><body>Internal Server Error</body></html>",
                               status=500)
                    return
                if state.require_login_for_dash and not self._authed():
                    self.send_response(302)
                    self.send_header("Location", "/login?expired=1")
                    self.end_headers()
                    return
                self._html("<!doctype html><html><head><title>经营看板</title></head>"
                           "<body><h1>经营看板</h1><div id='metric'>revenue=9800</div></body></html>")
                return

            if path == "/orders":
                if not self._authed():
                    self.send_response(302)
                    self.send_header("Location", "/login")
                    self.end_headers()
                    return
                page = int(qs.get("page", ["1"])[0])
                chunk = ORDERS[:3] if page == 1 else ORDERS[3:]
                rows = "".join(
                    f"<tr><td>{o['id']}</td><td><a class='order-link' href='/order/{o['id']}'>查看 {o['id']}</a></td></tr>"
                    for o in chunk
                )
                next_link = "<a id='next' href='/orders?page=2'>下一页</a>" if page == 1 else ""
                self._html(
                    f"<!doctype html><html><head><title>订单列表 P{page}</title></head><body>"
                    f"<h1>订单列表 第 {page} 页</h1><table><thead><tr><th>订单号</th><th>操作</th></tr></thead>"
                    f"<tbody>{rows}</tbody></table>{next_link}</body></html>"
                )
                return

            if path.startswith("/order/"):
                oid = path.split("/order/", 1)[1]
                state.order_hits[oid] = state.order_hits.get(oid, 0) + 1
                order = next((o for o in ORDERS if o["id"] == oid), None)
                if not order:
                    self._html("not found", 404)
                    return
                idx = int(oid.split("-")[1]) - 101
                # ORD-103 triggers a JS alert + stray popup tab to test browser hygiene!
                quirk_js = ""
                if oid == "ORD-103":
                    quirk_js = "<script>window.open('about:blank'); alert('库存提醒: ORD-103');</script>"
                # ORD-104+ switches to v2 UI: blocking cookie overlay + button renamed "确认核验"
                btn_label = "核验通过" if idx < 3 else "确认核验"
                overlay = ""
                if idx >= 3:
                    overlay = (
                        "<div id='cookie-modal' role='dialog' style='position:fixed;inset:0;background:rgba(0,0,0,.6);"
                        "display:flex;align-items:center;justify-content:center'>"
                        "<div style='background:#fff;padding:20px'>请确认隐私条款 "
                        "<button onclick=\"document.getElementById('cookie-modal').remove()\">接受</button></div></div>"
                    )
                self._html(
                    f"<!doctype html><html><head><title>订单 {oid}</title></head><body>"
                    f"<h1>订单详情 <span id='oid'>{oid}</span></h1>"
                    f"<p>金额: <span id='amt'>{order['amount']}</span></p>"
                    f"<button id='verify-btn' onclick=\"fetch('/verify/{oid}', {{method:'POST'}})"
                    f".then(()=>document.getElementById('st').textContent='已核验')\">{btn_label}</button>"
                    f"<p id='st'>待核验</p>{overlay}{quirk_js}</body></html>"
                )
                return

            self._html("not found", 404)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture()
def session_file(tmp_path: Path):
    path = tmp_path / "browser" / "session.json"
    yield path
    from cuaflow.platform.browser import PersistentBrowser

    try:
        PersistentBrowser.connect(session_file=path, launch_if_missing=False).shutdown()
    except Exception:  # noqa: BLE001
        pass


UNIT_PLAN = {
    "name": "核验单笔订单",
    "description": "打开订单详情点击核验并读取金额",
    "intent_template": "打开 {order_url} 完成核验并读取金额",
    "side_effects": False,
    "start_url": "",
    "params": {"order_url": {"type": "str", "description": "订单详情URL", "example": "__FIRST_URL__"}},
    "output": {"id": {"type": "str"}, "amount": {"type": "int"}},
    "steps": [
        {"id": "open_and_verify", "title": "打开订单并点击核验", "expect": "状态变为已核验"},
        {"id": "extract_amount", "title": "提取订单号与金额", "expect": "得到订单号和金额"},
    ],
}

UNIT_STEP_1_V1 = textwrap.dedent('''
    @flow.step("打开订单并点击核验", expect="状态变为已核验")
    def open_and_verify(c: Ctx):
        page = c.browser.page()
        page.set_default_timeout(3000)
        page.goto(c.params.order_url)
        page.get_by_role("button", name="核验通过").click()
        expect.text(page.locator("#st"), "已核验", timeout=3)
''')

UNIT_STEP_2 = textwrap.dedent('''
    @flow.step("提取订单号与金额", expect="得到订单号和金额")
    def extract_amount(c: Ctx):
        page = c.browser.page()
        c.output["id"] = page.locator("#oid").inner_text().strip()
        c.output["amount"] = int(page.locator("#amt").inner_text().strip())
        expect.that(c.output["amount"] > 0, "amount positive")
''')

UNIT_STEP_1_V2_REPAIRED = textwrap.dedent('''
    @flow.step("打开订单并点击核验", expect="状态变为已核验")
    def open_and_verify(c: Ctx):
        import re
        page = c.browser.page()
        page.set_default_timeout(3000)
        page.goto(c.params.order_url)
        c.browser.dismiss_overlays(page)
        page.get_by_role("button", name=re.compile("核验通过|确认核验")).click()
        expect.text(page.locator("#st"), "已核验", timeout=3)
''')


def test_long_horizon_paginated_batch_mission_saves_tokens_and_self_heals(tmp_path: Path, session_file: Path):
    state = PortalState()
    srv, base_url = start_portal(state)
    try:
        plan_args = {
            "goal": "登录后台收集所有分页订单并批量核验汇总",
            "phases": [
                {"id": "collect_orders", "title": "登录并翻页收集全部订单链接", "kind": "explore", "status": "running"},
                {"id": "batch_verify", "title": "编译单笔脚本并批量核验全部订单", "kind": "batch", "status": "pending"},
                {"id": "summarize", "title": "汇总核验金额", "kind": "synthesize", "status": "pending"},
            ],
        }
        explore_code = textwrap.dedent(f'''
            page = c.browser.page()
            page.goto("{base_url}/login")
            page.get_by_label("账号").fill("admin")
            page.get_by_label("密码").fill("pass123")
            page.get_by_role("button", name="登录").click()
            c.browser.wait_stable(page, timeout=5, idle_ms=100)
            urls = [x["href"] for x in c.browser.extract_links("a.order-link", page)]
            page.locator("#next").click()
            c.browser.wait_stable(page, timeout=5, idle_ms=100)
            urls.extend(x["href"] for x in c.browser.extract_links("a.order-link", page))
            c.state["order_urls"] = urls
            print("collected:", len(urls))
        ''')

        planner_turns = {"n": 0}

        def planner_fn(messages, tools):
            planner_turns["n"] += 1
            n = planner_turns["n"]
            if n == 1:
                return {"tool": "update_plan", "args": plan_args}
            if n == 2:
                return {"tool": "python", "args": {"code": explore_code}}
            if n == 3:
                return {
                    "tool": "run_batch",
                    "args": {
                        "batch_id": "orders_batch",
                        "items_state_key": "order_urls",
                        "item_param": "order_url",
                        "unit_instruction": "打开 {order_url} 完成核验并提取订单号和金额",
                        "output_state_key": "verified_orders",
                        "phase_id": "batch_verify",
                    },
                }
            if n == 4:
                done_phases = [
                    {**p, "status": "done"} for p in plan_args["phases"]
                ]
                return {"tool": "update_plan", "args": {"goal": plan_args["goal"], "phases": done_phases}}
            return {
                "tool": "python",
                "args": {"code": "c.state['total_amount'] = sum(x['amount'] for x in c.state['verified_orders']); print(c.state['total_amount'])"},
            } if n == 5 else {
                "tool": "finish",
                "args": {
                    "final_response": "全部 6 笔订单已核验，总金额 2100",
                    "output": {"count": 6, "total_amount": 2100},
                },
            }

        unit_plan = json.loads(json.dumps(UNIT_PLAN).replace("__FIRST_URL__", f"{base_url}/order/ORD-101"))
        author_llm = FakeLLM([
            {"tool": "plan_flow", "args": unit_plan},
            {"tool": "try_step", "args": {"source": UNIT_STEP_1_V1}},
            {"tool": "try_step", "args": {"source": UNIT_STEP_2}},
            {"tool": "finish", "args": {"final_response": "单笔核验脚本录制完成"}},
        ])
        repairer_llm = FakeLLM([
            {"tool": "observe", "args": {}},
            {"tool": "try_step", "args": {"source": UNIT_STEP_1_V2_REPAIRED, "summary": "dismiss cookie overlay and match 确认核验"}},
        ])
        ok_judge = FakeLLM(lambda m, t: {"passed": True, "reason": "ok"})

        s = Settings()
        s.planner.tab_limit = 3
        svc = Services(
            root=tmp_path / "lib",
            llm_override={
                "planner": FakeLLM(planner_fn),
                "author": author_llm,
                "repairer": repairer_llm,
                "vision_judge": ok_judge,
                "supervisor": ok_judge,
                "matcher": FakeLLM([]),
            },
            runner_opts={"desktop": "fake", "headless": True, "browser_session": str(session_file)},
            settings_file=tmp_path / "s.json",
            start_worker=False,
        )
        svc.update_settings(s)

        # Long-horizon instruction automatically routes to MissionPlanner!
        task = svc.orchestrator.submit_task(
            "登录后台，遍历所有页收集订单链接，逐个打开订单完成核验并汇总总金额",
            start_url=f"{base_url}/login",
            enqueue=False,
        )
        task = svc.orchestrator.process_task(task["id"], unattended=True)
        assert task["status"] == "succeeded", task
        assert state.verified == {o["id"] for o in ORDERS}
        assert len(author_llm.calls) == 4  # Author compiled ONLY ONCE (item 0), NOT 6 times!

        flows = svc.store.list_flows()
        assert len(flows) == 1
        compiled = flows[0]
        assert compiled["current_version"] == 2  # v1 auto-healed to v2 on ORD-104!

        usage = task["usage"]
        # 5 items ran via compiled script * 2 steps/item (minus 1 step repaired on ORD-104) >= 9 zero-exploration script steps!
        assert usage["script_steps"] >= 9
        assert usage["estimated_tokens_saved"] >= 9 * 1800
        assert task["checkpoint"]["mission_state"]["total_amount"] == 2100

        # Verify leaked popup tab from ORD-103 was pruned automatically
        from cuaflow.platform.browser import PersistentBrowser

        with PersistentBrowser.connect(session_file=session_file, headless=True) as br:
            assert len(br.pages()) <= 3
    finally:
        srv.shutdown()


def test_browser_sentinel_catches_500_and_expired_session_recovered_by_fallback(tmp_path: Path, session_file: Path):
    state = PortalState()
    state.logged_in = True
    state.require_login_for_dash = False
    srv, base_url = start_portal(state)
    try:
        # Naive flow: step 1 navigates without text assertion, step 2 reads #metric if present
        flow_src = textwrap.dedent(f'''
            from cuaflow.flow import Ctx, Flow, expect

            flow = Flow("读取经营看板", output={{"metric": "str"}})

            @flow.step("打开看板页面", expect="进入经营看板页面而非登录页或报错页")
            def open_dash(c: Ctx):
                page = c.browser.page()
                page.set_default_timeout(3000)
                page.goto("{base_url}/dashboard")

            @flow.step("读取指标", expect="提取到 revenue 指标")
            def read_metric(c: Ctx):
                page = c.browser.page()
                expect.visible(page.locator("#metric"), timeout=2)
                c.output["metric"] = page.locator("#metric").inner_text().strip()
        ''')
        p = tmp_path / "dash.py"
        p.write_text(flow_src, encoding="utf-8")

        def judge_fn(messages, tools):
            text = str(messages[-1]["content"])
            if "意外跳转到登录页" in text or "登录后台" in text:
                return {"passed": False, "reason": "Redirected to login page due to expired session"}
            return {"passed": True, "reason": "Dashboard visible"}

        # 1) Establish baseline healthy run -> clears needs_verify to 0
        s = Settings()
        s.supervisor.l2_policy = "on_change"
        s.supervisor.unattended = True
        s.supervisor.max_repair_attempts = 1
        svc = Services(
            root=tmp_path / "lib",
            llm_override={
                "vision_judge": FakeLLM(judge_fn),
                "supervisor": FakeLLM(lambda m, t: {"passed": True, "reason": "ok"}),
                "repairer": FakeLLM([{"tool": "give_up", "args": {"reason": "http 500 server down"}}]),
                "fallback": FakeLLM([{"tool": "give_up", "args": {"reason": "server returning 500"}}]),
            },
            runner_opts={"desktop": "fake", "headless": True, "browser_session": str(session_file)},
            settings_file=tmp_path / "s.json",
            start_worker=False,
        )
        svc.update_settings(s)
        flow = svc.store.create_flow(load_flow(p)[0].meta(), flow_src)

        r_ok = svc.orchestrator.process_run(svc.orchestrator.submit_run(flow["id"], {}, enqueue=False)["id"])
        assert r_ok["status"] == "succeeded" and r_ok["output"] == {"metric": "revenue=9800"}
        assert svc.store.step_stats(flow["id"], "open_dash")["needs_verify"] == 0

        # 2) Server returns HTTP 500 on /dashboard -> L1.5 Sentinel catches doc_status=500 deterministically!
        state.dashboard_500 = True
        r_500 = svc.orchestrator.process_run(svc.orchestrator.submit_run(flow["id"], {}, enqueue=False)["id"])
        assert r_500["status"] == "failed"
        open_checks = r_500["steps"][0]["checks"]
        assert any(c["level"] == "L1.5" and not c["passed"] and "HTTP 500" in c["detail"] for c in open_checks)

        # 3) Server recovers from 500, but session expired (302 -> /login)!
        # L1.5 Sentinel detects login wall -> wakes Adaptive L2 -> Repairer gives up -> Tier-2 FallbackAgent re-authenticates!
        state.dashboard_500 = False
        state.logged_in = False
        state.require_login_for_dash = True
        svc.llms._override["repairer"] = FakeLLM([{"tool": "give_up", "args": {"reason": "session expired, need login"}}])
        relogin_py = textwrap.dedent(f'''
            page = c.browser.page()
            page.get_by_label("账号").fill("admin")
            page.get_by_label("密码").fill("pass123")
            page.get_by_role("button", name="登录").click()
            c.browser.wait_stable(page, timeout=5, idle_ms=100)
            page.goto("{base_url}/dashboard")
        ''')
        svc.llms._override["fallback"] = FakeLLM([
            {"tool": "python", "args": {"code": relogin_py}},
            {"tool": "complete_step", "args": {"summary": "logged back in and opened dashboard"}},
        ])
        r_exp = svc.orchestrator.process_run(svc.orchestrator.submit_run(flow["id"], {}, enqueue=False)["id"])
        assert r_exp["status"] == "succeeded", r_exp.get("error")
        assert r_exp["output"] == {"metric": "revenue=9800"}
        assert r_exp["steps"][0]["status"] == "repaired"
    finally:
        srv.shutdown()


def test_long_horizon_batch_interrupt_and_checkpoint_resume(tmp_path: Path, session_file: Path):
    state = PortalState()
    srv, base_url = start_portal(state)
    try:
        # Seed unit flow for ORD-101..ORD-103 (first 3 items, v1 UI)
        unit_src = textwrap.dedent('''
            from cuaflow.flow import Ctx, Flow, expect

            flow = Flow("读取订单金额", params={"order_url": "str"}, output={"id": "str", "amount": "int"})

            @flow.step("打开并提取", expect="得到金额")
            def open_extract(c: Ctx):
                page = c.browser.page()
                page.goto(c.params.order_url)
                c.output["id"] = page.locator("#oid").inner_text().strip()
                c.output["amount"] = int(page.locator("#amt").inner_text().strip())
                expect.that(c.output["amount"] > 0, "positive")
        ''')
        p = tmp_path / "u.py"
        p.write_text(unit_src, encoding="utf-8")

        urls = [f"{base_url}/order/ORD-101", f"{base_url}/order/ORD-102", f"{base_url}/order/ORD-103"]
        ok_judge = FakeLLM(lambda m, t: {"passed": True, "reason": "ok"})
        s = Settings()
        s.supervisor.l2_policy = "off"
        s.supervisor.l3_enabled = False

        task_holder: dict[str, str] = {}

        def planner_run1(messages, tools):
            # Won't reach finish because cancel fires after item #2 (index 1)
            return {
                "tool": "run_batch",
                "args": {
                    "batch_id": "b1",
                    "items": urls,
                    "item_param": "order_url",
                    "flow_id": task_holder["flow_id"],
                    "output_state_key": "out_items",
                },
            }

        svc = Services(
            root=tmp_path / "lib",
            llm_override={"planner": FakeLLM(planner_run1), "default": ok_judge},
            runner_opts={"desktop": "fake", "headless": True, "browser_session": str(session_file)},
            settings_file=tmp_path / "s.json",
            start_worker=False,
        )
        svc.update_settings(s)
        flow = svc.store.create_flow(load_flow(p)[0].meta(), unit_src)
        task_holder["flow_id"] = flow["id"]

        task = svc.orchestrator.submit_task("批量提取3笔订单", mode="mission", enqueue=False)

        # Cancel task right when batch_progress reaches completed == 2!
        def on_ev(ev):
            if ev["type"] == "batch_progress" and ev["data"].get("completed") == 2:
                svc.orchestrator.abort(task["id"])

        unsub = svc.bus.subscribe(on_ev)
        t_interrupted = svc.orchestrator.process_task(task["id"], unattended=True)
        unsub()

        assert t_interrupted["status"] == "aborted"
        assert t_interrupted["checkpoint"]["batches"]["b1"]["completed_count"] == 2
        assert state.order_hits == {"ORD-101": 1, "ORD-102": 1}

        # Now resume! ORD-101 and ORD-102 must NOT be hit again; only ORD-103 runs!
        resume_step = {"n": 0}

        def planner_resume(messages, tools):
            resume_step["n"] += 1
            if resume_step["n"] == 1:
                assert "Resumed Checkpoint" in str(messages[-1]["content"])
                return {
                    "tool": "run_batch",
                    "args": {
                        "batch_id": "b1",
                        "items": urls,
                        "item_param": "order_url",
                        "output_state_key": "out_items",
                    },
                }
            return {"tool": "finish", "args": {"final_response": "断点续跑完成全部 3 笔"}}

        svc.llms._override["planner"] = FakeLLM(planner_resume)
        svc.orchestrator.resume_task(task["id"], enqueue=False)
        t_done = svc.orchestrator.process_task(task["id"], resume=True, unattended=True)
        assert t_done["status"] == "succeeded", t_done
        assert state.order_hits == {"ORD-101": 1, "ORD-102": 1, "ORD-103": 1}
        assert len(t_done["checkpoint"]["mission_state"]["out_items"]) == 3
    finally:
        srv.shutdown()


def test_stateful_spa_pagination_loop_and_live_browser_observe(tmp_path: Path, session_file: Path):
    """SPA table where 'Next' mutates DOM in-place (no per-page URLs); run_batch(max_iterations=10) stops on has_next=False."""
    spa_html = """<!doctype html><html><head><meta charset="utf-8"><title>SPA Feed</title></head><body>
    <h1>实时数据流</h1><div id="row">ITEM-1</div>
    <button id="next-btn" onclick="
      window._idx = (window._idx || 1) + 1;
      document.getElementById('row').textContent = 'ITEM-' + window._idx;
      if (window._idx >= 4) document.getElementById('next-btn').remove();
    ">下一页</button></body></html>"""
    site_dir = tmp_path / "spa"
    site_dir.mkdir()
    (site_dir / "index.html").write_text(spa_html, encoding="utf-8")

    import functools

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site_dir))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    spa_url = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    try:
        page_flow_src = textwrap.dedent('''
            from cuaflow.flow import Ctx, Flow, expect

            flow = Flow("读取当前页并翻页", params={"page_index": {"type": "int", "default": 0}},
                        output={"item": "str", "has_next": "bool"})

            @flow.step("读取当前条目并点击下一页", expect="读取到条目")
            def step_page(c: Ctx):
                page = c.browser.page()
                item = page.locator("#row").inner_text().strip()
                c.output["item"] = item
                btn = page.locator("#next-btn")
                if btn.count() > 0:
                    btn.click()
                    c.output["has_next"] = True
                else:
                    c.output["has_next"] = False
                expect.that(bool(item), "non-empty item")
        ''')
        p = tmp_path / "pf.py"
        p.write_text(page_flow_src, encoding="utf-8")

        s = Settings()
        s.supervisor.l2_policy = "off"
        s.supervisor.l3_enabled = False
        ok_judge = FakeLLM(lambda m, t: {"passed": True, "reason": "ok"})
        flow_holder: dict[str, str] = {}
        obs_seen = {"aria": ""}
        turn = {"n": 0}

        def planner_fn(messages, tools):
            turn["n"] += 1
            if turn["n"] == 1:
                return {"tool": "python", "args": {"code": f"c.browser.page().goto('{spa_url}')"}}
            if turn["n"] == 2:
                return {
                    "tool": "run_batch",
                    "args": {
                        "batch_id": "spa_pages",
                        "max_iterations": 10,
                        "flow_id": flow_holder["id"],
                        "output_state_key": "pages_out",
                    },
                }
            if turn["n"] == 3:
                # Immediately after _reload_scratch(), observe() must attach to the already-open live browser!
                return {"tool": "observe", "args": {}}
            obs_seen["aria"] = str(messages[-2].get("content", ""))
            return {"tool": "finish", "args": {"final_response": "SPA 翻页完成"}}

        svc = Services(
            root=tmp_path / "lib",
            llm_override={"planner": FakeLLM(planner_fn), "default": ok_judge},
            runner_opts={"desktop": "fake", "headless": True, "browser_session": str(session_file)},
            settings_file=tmp_path / "s.json",
            start_worker=False,
        )
        svc.update_settings(s)
        flow = svc.store.create_flow(load_flow(p)[0].meta(), page_flow_src)
        flow_holder["id"] = flow["id"]

        task = svc.orchestrator.submit_task("遍历 SPA 全部页", mode="mission", enqueue=False)
        task = svc.orchestrator.process_task(task["id"], unattended=True)
        assert task["status"] == "succeeded", task
        items = [x["item"] for x in task["checkpoint"]["mission_state"]["pages_out"]]
        # Stopped automatically after 4 pages (out of max_iterations=10) when has_next became False!
        assert items == ["ITEM-1", "ITEM-2", "ITEM-3", "ITEM-4"]
        # And observe() after _reload_scratch() attached to the live browser and saw ITEM-4!
        assert "ITEM-4" in obs_seen["aria"] and "SPA Feed" in obs_seen["aria"]
    finally:
        srv.shutdown()
