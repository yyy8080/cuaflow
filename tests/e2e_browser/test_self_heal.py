"""基于真实无头 Chromium 的端到端自愈测试：针对界面发生改版的本地站点验证自动修复。"""

import functools
import http.server
import textwrap
import threading
from pathlib import Path

import pytest

from cuaflow.flow.flow import load_flow
from cuaflow.llm.base import FakeLLM
from cuaflow.services import Services

pytestmark = pytest.mark.browser

V1 = """<!doctype html><html><head><meta charset="utf-8"><title>报表系统</title></head><body>
<h1>月度报表</h1>
<label>关键词 <input id="q" aria-label="关键词"></label>
<button id="go" onclick="document.getElementById('result').textContent='已导出: '+document.getElementById('q').value">导出</button>
<p id="result"></p></body></html>"""

V2 = """<!doctype html><html><head><meta charset="utf-8"><title>报表系统</title>
<style>#cookie{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center}</style>
</head><body>
<h1>月度报表</h1>
<label>关键词 <input id="q" aria-label="关键词"></label>
<button id="dl" onclick="document.getElementById('result').textContent='已导出: '+document.getElementById('q').value">下载</button>
<p id="result"></p>
<div id="cookie"><div style="background:#fff;padding:20px">本网站使用 Cookie <button onclick="document.getElementById('cookie').remove()">接受</button></div></div>
</body></html>"""

FLOW = textwrap.dedent('''
    from cuaflow.flow import Ctx, Flow, expect

    flow = Flow("导出报表", params={"q": {"type": "str", "description": "关键词"}}, output={"result": "str"},
                intent="按关键词 {q} 导出报表", start_url="__URL__")

    @flow.step("打开报表页", expect="报表页显示导出按钮")
    def open_page(c: Ctx):
        page = c.browser.page()
        page.set_default_timeout(3000)
        page.goto(c.params.start_url)
        expect.visible(page.get_by_role("button", name="导出"), timeout=3)

    @flow.step("输入关键词并导出", expect="页面显示已导出")
    def export(c: Ctx):
        page = c.browser.page()
        page.get_by_label("关键词").fill(c.params.q)
        page.get_by_role("button", name="导出").click()
        expect.text(page.locator("#result"), "已导出", timeout=3)

    @flow.step("读取结果", expect="得到导出结果文本")
    def read_result(c: Ctx):
        c.output["result"] = c.browser.page().locator("#result").inner_text()
        expect.contains(c.output["result"], c.params.q)
''')

FIX_OPEN = textwrap.dedent('''
    @flow.step("打开报表页", expect="报表页显示导出按钮")
    def open_page(c: Ctx):
        import re
        page = c.browser.page()
        page.set_default_timeout(3000)
        page.goto(c.params.start_url)
        accept = page.get_by_role("button", name="接受")
        if accept.count():
            accept.click()
        expect.visible(page.get_by_role("button", name=re.compile("导出|下载")), timeout=3)
''')

FIX_EXPORT = textwrap.dedent('''
    @flow.step("输入关键词并导出", expect="页面显示已导出")
    def export(c: Ctx):
        import re
        page = c.browser.page()
        page.get_by_label("关键词").fill(c.params.q)
        page.get_by_role("button", name=re.compile("导出|下载")).click()
        expect.text(page.locator("#result"), "已导出", timeout=3)
''')


@pytest.fixture()
def site(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(V1, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    srv.shutdown()


@pytest.fixture()
def session_file(tmp_path):
    path = tmp_path / "browser" / "session.json"
    yield path
    from cuaflow.platform.browser import PersistentBrowser
    try:
        PersistentBrowser.connect(session_file=path, launch_if_missing=False).shutdown()
    except Exception:  # noqa: BLE001
        pass


def test_browser_flow_self_heals_after_ui_change(tmp_path, site, session_file):
    root, url = site
    repairs = [
        {"tool": "observe", "args": {}},
        {"tool": "try_step", "args": {"source": FIX_OPEN, "summary": "accept cookie banner; button may be 下载"}},
        {"tool": "try_step", "args": {"source": FIX_EXPORT, "summary": "button renamed to 下载"}},
    ]
    ok = FakeLLM(lambda m, t: {"passed": True, "reason": "ok"})
    svc = Services(root=tmp_path / "lib", llm_override={"repairer": FakeLLM(repairs), "default": ok},
                   runner_opts={"desktop": "fake", "headless": True, "browser_session": str(session_file)},
                   settings_file=tmp_path / "s.json", start_worker=False)
    src = FLOW.replace("__URL__", url)
    p = tmp_path / "f.py"
    p.write_text(src, encoding="utf-8")
    flow = svc.store.create_flow(load_flow(p)[0].meta(), src)

    # 1) 初始版站点：原流程无需改动直接通过
    r1 = svc.orchestrator.process_run(svc.orchestrator.submit_run(flow["id"], {"q": "华东"}, enqueue=False)["id"])
    assert r1["status"] == "succeeded", r1.get("error")
    assert r1["output"] == {"result": "已导出: 华东"} and r1["new_version"] is None

    # 2) 站点界面改版（按钮改名 + 新增 Cookie 遮罩）：监督者检测失败并由修复者在线热补丁修复
    (root / "index.html").write_text(V2, encoding="utf-8")
    r2 = svc.orchestrator.process_run(svc.orchestrator.submit_run(flow["id"], {"q": "华南"}, enqueue=False)["id"])
    assert r2["status"] == "succeeded", r2.get("error")
    assert r2["output"] == {"result": "已导出: 华南"}
    st = {s["step_id"]: s["status"] for s in r2["steps"]}
    assert st == {"open_page": "repaired", "export": "repaired", "read_result": "passed"}
    assert r2["new_version"] == 2
    v2 = svc.store.get_version(flow["id"], 2)
    assert v2["repaired_step"] == "open_page,export" and "接受" in v2["source"]

    # 3) 修复后的新版本直接干净跑通，不再触发大模型修复（若仍调用修复脚本则会因耗尽而报错）
    r3 = svc.orchestrator.process_run(svc.orchestrator.submit_run(flow["id"], {"q": "西北"}, enqueue=False)["id"])
    assert r3["status"] == "succeeded" and r3["version"] == 2 and r3["new_version"] is None
    assert r3["output"] == {"result": "已导出: 西北"}
    shot = Path(svc.store.run_dir(r3["id"])) / r3["steps"][0]["screenshot"]
    assert shot.is_file() and shot.stat().st_size > 1000
