"""示例：在网页上搜索关键词并读取第一条结果标题（浏览器）。

    cuaflow run-file examples/web_search_flow.py -p query="playwright"
"""

from cuaflow.flow import Ctx, Flow, expect

flow = Flow(
    "Bing 搜索取首条结果",
    params={"query": {"type": "str", "description": "搜索关键词", "example": "playwright"}},
    output={"title": "str"},
    intent="搜索 {query} 并返回第一条结果标题",
    start_url="https://www.bing.com",
)


@flow.step("打开搜索页", expect="页面显示搜索框", timeout=60)
def open_search(c: Ctx):
    page = c.browser.page()
    page.goto(c.params.start_url)
    # 可选的 Cookie 横幅：存在就接受
    for name in ("Accept", "接受", "同意"):
        btn = page.get_by_role("button", name=name)
        if btn.count():
            btn.first.click()
            break
    expect.visible(page.locator("textarea[name=q], input[name=q]").first)


@flow.step("提交搜索", expect="显示搜索结果列表")
def search(c: Ctx):
    page = c.browser.page()
    box = page.locator("textarea[name=q], input[name=q]").first
    box.fill(c.params.query)
    box.press("Enter")
    page.wait_for_load_state("domcontentloaded")
    expect.count(page.locator("#b_results h2"), minimum=1)


@flow.step("读取首条结果", expect="得到第一条结果的标题")
def read_first(c: Ctx):
    title = c.browser.page().locator("#b_results h2").first.inner_text().strip()
    c.output["title"] = title
    expect.that(bool(title), "first result title is empty")
