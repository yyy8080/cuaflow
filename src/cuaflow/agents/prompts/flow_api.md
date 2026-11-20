# Flow script API (cuaflow)

A flow is a Python file with one `Flow` object and step functions registered with `@flow.step(...)`.
Steps run **in order**, in a persistent runner process, sharing `c.state`. Each step must be:
- **synchronous** (Playwright *sync* API), self-contained, and **idempotent where possible**
  (check whether its effect is already present before acting, e.g. "already logged in?");
- ended with at least one deterministic `expect.*` assertion proving its outcome;
- free of hard-coded task values: use `c.params.<name>` for anything that may change between runs.

```python
from cuaflow.flow import Ctx, Flow, expect

flow = Flow(name="...", params={"query": {"type": "str", "description": "..."}}, output={"price": "str"})

@flow.step("Open search page", expect="Search box is visible", timeout=60, retries=0)
def open_search(c: Ctx):
    page = c.browser.page()                  # Playwright sync Page in a persistent Chromium/Edge
    page.goto(c.params.start_url)
    expect.visible(page.get_by_role("searchbox"))
```

`@flow.step(title, expect="", pre=None, timeout=120, retries=0, side_effect=False, critical=False, surface="auto", id=None)`
- `expect`: natural-language expected outcome — a vision Supervisor checks the post-step screenshot against it.
- `pre`: optional `lambda c: bool` precondition (used to decide whether state must be restored before a retry).
- `side_effect=True` for irreversible actions (submit order, send mail, delete) — never retried blindly.

## Context `c`
- `c.params.<name>` / `c.params["name"]` — run parameters (plus `start_url` when the flow has one).
- `c.state` — dict passed between steps (JSON-serializable values only; it is checkpointed).
- `c.output` — dict; the final result of the flow (fill it in the last step(s)).
- `c.browser.page()` — current Playwright page; `c.browser.new_page(url)`, `c.browser.pages()`,
  `c.browser.context()`, `c.browser.aria_snapshot()`.
  - Stability & extraction helpers: `c.browser.wait_stable(page=None, timeout=10, idle_ms=250)`,
    `c.browser.dismiss_overlays(page=None)` (safely closes cookie/consent overlays),
    `c.browser.prune_tabs(keep_max=6)` (closes leaked background tabs),
    `c.browser.extract_table(selector="table")` → `list[dict]`,
    `c.browser.extract_links(selector="a[href]", limit=200)` → `[{"text", "href"}]`.
- `c.desktop` — native desktop (Windows UIA via pywinauto):
  - `c.desktop.launch("notepad.exe")` → pid; `c.desktop.window(title_re=r".*记事本|.*Notepad", timeout=10)` → pywinauto
    `WindowSpecification` (UIA backend): `.child_window(title=..., control_type="Button", auto_id=...)`,
    `.click_input()`, `.type_keys("^s")`, `.set_edit_text(...)`, `.window_text()`, `.exists()`, `.wait("ready")`.
  - `c.desktop.ui_tree(window=None)` text tree of the foreground window; `c.desktop.windows()`; `c.desktop.foreground()`;
    `c.desktop.modal_dialogs()`; `c.desktop.type_text(s)`; `c.desktop.type_keys("{ENTER}")`; `c.desktop.hotkey("ctrl","s")`;
    `c.desktop.click(x, y)`; `c.desktop.activate(win)`; `c.desktop.close_window(win)`.
- `c.shell.run(cmd, timeout=120)` → `ShellResult(exit_code, stdout, stderr)` (PowerShell on Windows, bash elsewhere).
- `c.log(...)` log line; `c.snap("name")` extra screenshot.

## Assertions `expect`
`expect.visible(locator)`, `expect.hidden(locator)`, `expect.text(locator, "substr", exact=False)`,
`expect.count(locator, minimum=1)`, `expect.url(page, regex)`, `expect.title(page, regex)`,
`expect.file_exists(path, timeout=10)`, `expect.file_contains(path, s)`, `expect.window_exists(c.desktop, title_re)`,
`expect.window_gone(c.desktop, title_re)`, `expect.no_modal(c.desktop)`, `expect.that(cond, msg)`,
`expect.equals(a, b)`, `expect.contains(haystack, needle)`, `expect.eventually(fn, msg, timeout)`.

## Robustness rules
- Prefer semantic selectors: `get_by_role/get_by_label/get_by_text` (web) and UIA `title/auto_id/control_type` (desktop)
  over coordinates or brittle CSS/XPath. Use coordinates only as a last resort.
- Wait for conditions (`expect.*`, `locator.wait_for`, `win.wait("ready")`), never fixed long sleeps.
- Handle known optional popups (cookie banners, "save changes?" dialogs) defensively inside the step.
- Keep each step focused on one user-visible sub-goal with one clear expected outcome.
