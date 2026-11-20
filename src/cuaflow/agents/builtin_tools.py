"""系统内置高可靠原子化工具集 `builtin_tools`。

为所有智能体循环（`DirectOperator`、`Supervisor` 兜底接管、`FallbackAgent` 等）提供开箱即用、
内置异常自愈与语义定位回退的高层工具，大幅减少手写碎片化 `python` 脚本导致的语法报错与轮数浪费：
- 浏览器高层工具：`browser_goto`、`browser_interact`、`browser_extract`、`browser_tabs`
- 桌面高层工具：`desktop_control`
- 文件、网络与持久记忆工具：`file_ops`、`web_fetch`、`memory_note`
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Callable

import httpx

from cuaflow.agents.loop import Tool, ToolResult
from cuaflow.agents.tools import RunnerGuard
from cuaflow.flow.client import RunnerClient
from cuaflow.llm.base import tool_spec


def _clean_html_to_text(raw_html: str, limit: int = 8000) -> str:
    """剥离 HTML 中的 script/style 标签并还原实体为紧凑可读文本。"""
    cleaned = re.sub(r"<(script|style|noscript|svg)[^>]*>[\s\S]*?</\1>", " ", raw_html, flags=re.IGNORECASE)
    cleaned = re.sub(r"<(br|p|div|li|tr|h[1-6])[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    lines = [" ".join(ln.split()) for ln in cleaned.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text[:limit] + ("\n...[已截断]..." if len(text) > limit else "")


def build_builtin_tools(
    runner: RunnerClient,
    run_dir: Path,
    *,
    on_captcha: Callable[[dict[str, Any]], str | None] | None = None,
    on_observation: Callable[[dict[str, Any]], None] | None = None,
    on_memory_update: Callable[[str, Any], None] | None = None,
) -> list[Tool]:
    """构造绑定到当前实时 `RunnerClient` 与运行目录的全套内置工具列表。"""
    guard = RunnerGuard(runner, run_dir)

    def _run_runner_json(code: str, timeout: float = 60.0) -> tuple[dict[str, Any] | None, str | None]:
        res, err = guard.call(lambda: runner.exec(code, timeout=timeout))
        if err:
            return None, err
        if not res.get("ok"):
            err_txt = str(res.get("error") or "unknown runner error")
            # 若遇到页面跳转引发的上下文销毁或目标关闭，自动触发零 Token 浏览器自愈提示
            if any(k in err_txt for k in ("Execution context was destroyed", "Target page, context or browser has been closed", "TargetClosedError")):
                guard.call(lambda: runner.heal_browser())
                return None, f"页面刚刚发生跳转或标签页刷新（{err_txt}），浏览器连接已自动恢复，请重新读取或继续下一步。"
            return None, f"{err_txt}\n{str(res.get('traceback') or '')[-1200:]}".strip()
        out = (res.get("stdout") or "").strip()
        if not out:
            return {}, None
        last_line = out.splitlines()[-1]
        try:
            parsed = json.loads(last_line)
            if isinstance(parsed, dict):
                return parsed, None
        except json.JSONDecodeError:
            pass
        return {"raw": out}, None

    def _handle_post_browser_health(payload: dict[str, Any]) -> str | None:
        if on_observation and (payload.get("url") or payload.get("screenshot")):
            on_observation(payload)
        health = payload.get("health") or {}
        if isinstance(health, dict) and health.get("captcha_detected"):
            if on_captcha is not None:
                return on_captcha(payload)
            return "检测到页面存在人机验证（CAPTCHA），已触发安全边界停止自动操作，请勿尝试绕过。"
        return None

    # ------------------------------------------------------------------ 1. 浏览器导航
    def browser_goto(args: dict[str, Any]) -> ToolResult:
        raw_url = str(args.get("url") or "").strip()
        if not raw_url:
            return ToolResult("参数错误：url 不能为空。")
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw_url):
            raw_url = "https://" + raw_url
        new_tab = bool(args.get("new_tab", False))
        code = f"""
import json, time
url = {raw_url!r}
br = c.browser
page = br.new_page() if {new_tab!r} else br.page()
for _attempt in range(2):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        break
    except Exception as _e:
        if _attempt == 0 and ("ERR_ABORTED" in str(_e) or "frame was detached" in str(_e).lower()):
            time.sleep(0.4)
            continue
        raise
br.wait_stable(page, timeout=6.0)
dismissed = br.dismiss_overlays(page)
health = br.page_health(page)
headings = page.evaluate('''() => Array.from(document.querySelectorAll('h1, h2, h3')).map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 10)''')
print(json.dumps({{
    "url": page.url,
    "title": page.title(),
    "dismissed_overlays": dismissed,
    "headings": headings,
    "health": health,
}}, ensure_ascii=False))
"""
        data, err = _run_runner_json(code, timeout=65.0)
        if err or data is None:
            return ToolResult(f"browser_goto 失败：{err}")
        cap_msg = _handle_post_browser_health(data)
        if cap_msg:
            return ToolResult(cap_msg)
        lines = [
            f"已打开页面：{data.get('title') or '(无标题)'}",
            f"当前 URL：{data.get('url')}",
        ]
        if data.get("dismissed_overlays"):
            lines.append(f"已自动关闭遮罩弹窗：{data['dismissed_overlays']}")
        if data.get("headings"):
            lines.append(f"页面主要标题：{data['headings']}")
        return ToolResult("\n".join(lines))

    # ------------------------------------------------------------------ 2. 浏览器智能语义交互
    def browser_interact(args: dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "").strip().lower()
        target = str(args.get("target") or "").strip()
        value = str(args.get("value") or "")
        key = str(args.get("key") or "Enter")
        index = max(0, int(args.get("index") or 0))
        pixels = int(args.get("pixels") or 650)

        valid_actions = {
            "click", "fill", "press", "select", "hover",
            "scroll_down", "scroll_up", "back", "reload", "dismiss_popups",
        }
        if action not in valid_actions:
            return ToolResult(f"不支持的 action={action!r}，可选值：{sorted(valid_actions)}")

        code = f"""
import json, re
br = c.browser
page = br.page()
act = {action!r}
target = {target!r}
val = {value!r}
key_name = {key!r}
idx = {index!r}
px = {pixels!r}

def _resolve_locator(p, t, for_input=False):
    if not t:
        return None
    if t.startswith(("css=", "xpath=", "text=", "role=")) or t.startswith(("#", ".", "//", "[")):
        loc = p.locator(t)
        if loc.count() > 0:
            return loc
    roles = ("textbox", "searchbox", "combobox") if for_input else ("button", "link", "menuitem", "tab", "checkbox", "radio", "textbox")
    for r in roles:
        try:
            loc = p.get_by_role(r, name=t)
            if loc.count() > 0:
                return loc
        except Exception:
            pass
    for getter in (p.get_by_label, p.get_by_placeholder, lambda x: p.get_by_text(x, exact=True), lambda x: p.get_by_text(x, exact=False)):
        try:
            loc = getter(t)
            if loc.count() > 0:
                return loc
        except Exception:
            pass
    loc = p.locator(t)
    return loc

note = ""
if act == "scroll_down":
    page.mouse.wheel(0, abs(px))
    note = f"向下滚动 {{abs(px)}}px"
elif act == "scroll_up":
    page.mouse.wheel(0, -abs(px))
    note = f"向上滚动 {{abs(px)}}px"
elif act == "back":
    page.go_back(wait_until="domcontentloaded", timeout=15000)
    note = "已后退上一页"
elif act == "reload":
    page.reload(wait_until="domcontentloaded", timeout=30000)
    note = "已刷新页面"
elif act == "dismiss_popups":
    closed = br.dismiss_overlays(page)
    note = f"尝试关闭遮罩弹窗：{{closed}}"
elif act == "press" and not target:
    page.keyboard.press(key_name)
    note = f"已按下按键 {{key_name}}"
else:
    if not target:
        raise ValueError(f"动作 {{act}} 需要提供 target（文本名称、占位符或 CSS 选择器）")
    loc = _resolve_locator(page, target, for_input=(act in ("fill", "select")))
    cnt = loc.count() if loc is not None else 0
    if cnt == 0:
        raise LookupError(f"未在页面找到匹配 {{target!r}} 的元素，请先用 browser_extract(mode='elements') 或 observe 查看可用控件")
    el = loc.nth(min(idx, cnt - 1))
    try:
        el.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    if act == "click":
        try:
            el.click(timeout=8000)
        except Exception:
            el.click(force=True, timeout=5000)
        note = f"已点击 {{target!r}}"
    elif act == "fill":
        el.fill(val, timeout=8000)
        if key_name and key_name.lower() not in ("", "none"):
            el.press(key_name)
            note = f"已在 {{target!r}} 输入 {{val!r}} 并按下 {{key_name}}"
        else:
            note = f"已在 {{target!r}} 输入 {{val!r}}"
    elif act == "press":
        el.press(key_name, timeout=6000)
        note = f"已在 {{target!r}} 按下 {{key_name}}"
    elif act == "select":
        try:
            el.select_option(label=val, timeout=6000)
        except Exception:
            el.select_option(value=val, timeout=6000)
        note = f"已在 {{target!r}} 选择 {{val!r}}"
    elif act == "hover":
        el.hover(timeout=6000)
        note = f"已悬停 {{target!r}}"

br.wait_stable(page, timeout=4.0, idle_ms=200)
active = br.page()
health = br.page_health(active)
print(json.dumps({{
    "note": note,
    "url": active.url,
    "title": active.title(),
    "open_tabs": len(br.pages()),
    "health": health,
}}, ensure_ascii=False))
"""
        # 对 fill 动作默认不自动按回车，除非调用方显式传了 key
        if action == "fill" and "key" not in args:
            code = code.replace(f"key_name = {key!r}", "key_name = ''")

        data, err = _run_runner_json(code, timeout=45.0)
        if err or data is None:
            return ToolResult(f"browser_interact({action}, {target!r}) 未成功：{err}")
        cap_msg = _handle_post_browser_health(data)
        if cap_msg:
            return ToolResult(cap_msg)
        return ToolResult(
            f"{data.get('note')}\n当前页面：{data.get('title')} | {data.get('url')}（打开标签页数：{data.get('open_tabs')}）"
        )

    # ------------------------------------------------------------------ 3. 浏览器结构化提取
    def browser_extract(args: dict[str, Any]) -> ToolResult:
        mode = str(args.get("mode") or "summary").strip().lower()
        selector = str(args.get("selector") or "").strip()
        max_items = max(1, min(300, int(args.get("max_items") or 40)))
        code = f"""
import json
br = c.browser
page = br.page()
mode = {mode!r}
sel = {selector!r}
limit = {max_items!r}

if mode == "links":
    data = br.extract_links(sel or "a[href]", page=page, limit=limit)
elif mode == "table":
    data = br.extract_table(sel or "table", page=page, max_rows=limit)
elif mode == "elements":
    data = page.evaluate('''(lim) => {{
        const out = [];
        const nodes = document.querySelectorAll('a[href], button, input, textarea, select, [role="button"], [role="tab"], [role="link"]');
        for (const el of nodes) {{
            const r = el.getBoundingClientRect();
            if (r.width <= 2 || r.height <= 2) continue;
            const st = window.getComputedStyle(el);
            if (st.visibility === 'hidden' || st.display === 'none') continue;
            const tag = el.tagName.toLowerCase();
            const label = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || el.value || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
            const id = el.id ? `#${{el.id}}` : '';
            out.push({{ tag, type: el.getAttribute('type') || '', label, selector_hint: id || tag }});
            if (out.length >= lim) break;
        }}
        return out;
    }}''', limit)
else:
    target_sel = sel or "body"
    text = page.evaluate('''(s) => {{
        const root = document.querySelector(s) || document.body;
        return root ? (root.innerText || '').trim() : '';
    }}''', target_sel)
    headings = page.evaluate('''() => Array.from(document.querySelectorAll('h1, h2, h3')).map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 15)''')
    data = {{"url": page.url, "title": page.title(), "headings": headings, "text": text[:6500]}}

print(json.dumps({{"mode": mode, "url": page.url, "title": page.title(), "result": data}}, ensure_ascii=False))
"""
        data, err = _run_runner_json(code, timeout=35.0)
        if err or data is None:
            return ToolResult(f"browser_extract 失败：{err}")
        return ToolResult(json.dumps(data, ensure_ascii=False, indent=2)[:9000])

    # ------------------------------------------------------------------ 4. 浏览器多标签页管理
    def browser_tabs(args: dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "list").strip().lower()
        tab_index = int(args.get("tab_index") or 0)
        code = f"""
import json
br = c.browser
act = {action!r}
idx = {tab_index!r}
if act == "close_others":
    closed = br.prune_tabs(1)
elif act == "switch":
    ps = br.pages()
    if 0 <= idx < len(ps):
        br.set_active(ps[idx])
    else:
        raise IndexError(f"tab_index={{idx}} 超出范围（共 {{len(ps)}} 个标签页）")
active = br.page()
tabs = [{{"index": i, "title": p.title(), "url": p.url, "active": p == active}} for i, p in enumerate(br.pages())]
print(json.dumps({{"tabs": tabs}}, ensure_ascii=False))
"""
        data, err = _run_runner_json(code, timeout=25.0)
        if err or data is None:
            return ToolResult(f"browser_tabs 失败：{err}")
        return ToolResult(json.dumps(data, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------------ 5. 原生桌面控制工具
    def desktop_control(args: dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "list_windows").strip().lower()
        title_re = str(args.get("title_re") or "").strip()
        text = str(args.get("text") or "")
        keys = str(args.get("keys") or "")
        x = args.get("x")
        y = args.get("y")
        button = str(args.get("button") or "left")
        double = bool(args.get("double", False))
        command = str(args.get("command") or "").strip()

        code = f"""
import json
d = c.desktop
act = {action!r}
tre = {title_re!r}
if act == "list_windows":
    wins = [w.to_dict() for w in d.windows()]
    fg = d.foreground()
    modals = [m.to_dict() for m in d.modal_dialogs()]
    print(json.dumps({{"foreground": fg.to_dict() if fg else None, "windows": wins, "modals": modals}}, ensure_ascii=False))
elif act == "focus_window":
    w = d.window(title_re=tre, timeout=8)
    d.activate(w)
    print(json.dumps({{"ok": True, "action": "focused", "title_re": tre}}, ensure_ascii=False))
elif act == "close_window":
    w = d.window(title_re=tre, timeout=5)
    d.close_window(w)
    print(json.dumps({{"ok": True, "action": "closed", "title_re": tre}}, ensure_ascii=False))
elif act == "ui_tree":
    w = d.window(title_re=tre, timeout=5) if tre else None
    tree = d.ui_tree(w, max_depth=7, max_nodes=280)
    print(json.dumps({{"ui_tree": tree[:7000]}}, ensure_ascii=False))
elif act == "click":
    d.click(int({x!r}), int({y!r}), button={button!r}, double={double!r})
    print(json.dumps({{"ok": True, "clicked": [{x!r}, {y!r}]}}, ensure_ascii=False))
elif act == "type_text":
    d.type_text({text!r})
    print(json.dumps({{"ok": True, "typed_len": len({text!r})}}, ensure_ascii=False))
elif act == "hotkey":
    parts = [k.strip() for k in {keys!r}.replace("+", " ").split() if k.strip()]
    d.hotkey(*parts)
    print(json.dumps({{"ok": True, "hotkey": parts}}, ensure_ascii=False))
elif act == "type_keys":
    d.type_keys({keys!r})
    print(json.dumps({{"ok": True, "type_keys": {keys!r}}}, ensure_ascii=False))
elif act == "launch":
    pid = d.launch({command!r})
    print(json.dumps({{"ok": True, "pid": pid, "command": {command!r}}}, ensure_ascii=False))
else:
    raise ValueError(f"未知桌面动作: {{act!r}}")
"""
        data, err = _run_runner_json(code, timeout=30.0)
        if err or data is None:
            return ToolResult(f"desktop_control({action}) 失败：{err}")
        return ToolResult(json.dumps(data, ensure_ascii=False, indent=2)[:8000])

    # ------------------------------------------------------------------ 6. 本地文件读写管理
    def file_ops(args: dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "read").strip().lower()
        raw_path = str(args.get("path") or ".").strip()
        p = Path(raw_path).expanduser()
        if not p.is_absolute():
            p = (run_dir / p).resolve()

        try:
            if action == "exists":
                return ToolResult(json.dumps({"path": str(p), "exists": p.exists(), "is_file": p.is_file(), "is_dir": p.is_dir()}, ensure_ascii=False))
            if action == "list_dir":
                if not p.is_dir():
                    return ToolResult(f"目录不存在：{p}")
                entries = [
                    {"name": item.name, "type": "dir" if item.is_dir() else "file", "size": item.stat().st_size if item.is_file() else 0}
                    for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:200]
                ]
                return ToolResult(json.dumps({"dir": str(p), "entries": entries}, ensure_ascii=False, indent=2))
            if action == "read":
                if not p.is_file():
                    return ToolResult(f"文件不存在：{p}")
                txt = p.read_text(encoding="utf-8", errors="replace")
                if len(txt) > 10000:
                    txt = txt[:7500] + "\n...[文件较长，中间已省略]...\n" + txt[-2000:]
                return ToolResult(f"文件 {p} 内容：\n{txt}")
            if action in ("write", "append"):
                content = str(args.get("content") or "")
                p.parent.mkdir(parents=True, exist_ok=True)
                if action == "write":
                    p.write_text(content, encoding="utf-8")
                else:
                    with p.open("a", encoding="utf-8") as fh:
                        fh.write(content)
                return ToolResult(f"已成功{('写入' if action == 'write' else '追加')}文件：{p}（共 {len(content)} 字符）")
            return ToolResult(f"未知 file_ops action={action!r}（支持 read | write | append | list_dir | exists）")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(f"file_ops({action}, {p}) 异常：{exc}")

    # ------------------------------------------------------------------ 7. 轻量 HTTP 网页/接口抓取
    def web_fetch(args: dict[str, Any]) -> ToolResult:
        url = str(args.get("url") or "").strip()
        if not url:
            return ToolResult("参数错误：url 不能为空。")
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
            url = "https://" + url
        max_chars = max(500, min(20000, int(args.get("max_chars") or 8000)))
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=20.0,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) cuaflow/0.3"},
            ) as client:
                r = client.get(url)
                ctype = (r.headers.get("content-type") or "").lower()
                if "json" in ctype:
                    try:
                        pretty = json.dumps(r.json(), ensure_ascii=False, indent=2)
                        return ToolResult(f"HTTP {r.status_code} ({url}) JSON:\n{pretty[:max_chars]}")
                    except Exception:  # noqa: BLE001
                        pass
                body = _clean_html_to_text(r.text, limit=max_chars) if "html" in ctype or "<html" in r.text[:500].lower() else r.text[:max_chars]
                return ToolResult(f"HTTP {r.status_code} ({r.url}):\n{body}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(f"web_fetch 请求失败（如遇动态渲染站点请改用 browser_goto）：{exc}")

    # ------------------------------------------------------------------ 8. 抗压缩工作记忆存取
    def memory_note(args: dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "save").strip().lower()
        key = str(args.get("key") or "").strip()
        val = args.get("value")
        if action == "save":
            if not key:
                return ToolResult("保存记忆时必须提供 key。")
            guard.call(lambda: runner.update_output({key: val}))
            if on_memory_update is not None:
                on_memory_update(key, val)
            return ToolResult(f"已将关键结果写入持久记忆与输出字典：{key} = {json.dumps(val, ensure_ascii=False, default=str)[:400]}")
        fin, err = guard.call(lambda: runner.finish())
        if err or not isinstance(fin, dict):
            return ToolResult(f"读取记忆失败：{err}")
        out_map = fin.get("output") or {}
        if action == "get" and key:
            return ToolResult(json.dumps({key: out_map.get(key)}, ensure_ascii=False, indent=2))
        return ToolResult(json.dumps(out_map, ensure_ascii=False, indent=2))

    return [
        Tool(
            tool_spec(
                "browser_goto",
                "在独立浏览器中打开指定网址，自动等待页面稳定、关闭常见遮罩弹窗并返回标题与主要结构（推荐优先使用）。",
                {
                    "url": {"type": "string", "description": "目标网址（如 https://example.com）"},
                    "new_tab": {"type": "boolean", "description": "是否在新标签页打开，默认 false"},
                },
                ["url"],
            ),
            browser_goto,
        ),
        Tool(
            tool_spec(
                "browser_interact",
                "高可靠浏览器语义交互工具：自动按按钮文字、输入框标签/占位符或选择器完成点击、输入、按键、下拉选择、滚动与弹窗清理。",
                {
                    "action": {
                        "type": "string",
                        "enum": [
                            "click", "fill", "press", "select", "hover",
                            "scroll_down", "scroll_up", "back", "reload", "dismiss_popups",
                        ],
                    },
                    "target": {"type": "string", "description": "目标元素的可见文字、aria-label、placeholder 或 CSS/XPath 选择器"},
                    "value": {"type": "string", "description": "当 action 为 fill 或 select 时填入的文本值"},
                    "key": {"type": "string", "description": "当 action 为 press（或 fill 后需立即触发按键如 Enter）时的按键名"},
                    "index": {"type": "integer", "description": "匹配多个同类元素时的序号，默认 0"},
                    "pixels": {"type": "integer", "description": "滚动像素距离，默认 650"},
                },
                ["action"],
            ),
            browser_interact,
        ),
        Tool(
            tool_spec(
                "browser_extract",
                "一键提取当前网页正文摘要、超链接列表、结构化表格数据或可见可交互控件清单。",
                {
                    "mode": {
                        "type": "string",
                        "enum": ["summary", "links", "table", "elements"],
                        "description": "summary(正文与标题) | links(链接列表) | table(表格行字典) | elements(页面按钮与输入框清单)",
                    },
                    "selector": {"type": "string", "description": "可选范围 CSS 选择器"},
                    "max_items": {"type": "integer", "description": "最大返回条目数，默认 40"},
                },
                ["mode"],
            ),
            browser_extract,
        ),
        Tool(
            tool_spec(
                "browser_tabs",
                "查看、切换或清理浏览器打开的多个标签页。",
                {
                    "action": {"type": "string", "enum": ["list", "switch", "close_others"]},
                    "tab_index": {"type": "integer", "description": "切换目标标签页序号（从 0 开始）"},
                },
                ["action"],
            ),
            browser_tabs,
        ),
        Tool(
            tool_spec(
                "desktop_control",
                "原生操作系统桌面窗口与键鼠交互工具：列举/置顶/关闭窗口、读取 UIA 控件树、点击坐标、输入文本、发送快捷键（如 ctrl+s）或启动本地程序。",
                {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list_windows", "focus_window", "close_window", "ui_tree",
                            "click", "type_text", "hotkey", "type_keys", "launch",
                        ],
                    },
                    "title_re": {"type": "string", "description": "匹配目标窗口标题的正则表达式或关键字"},
                    "text": {"type": "string", "description": "type_text 输入文本"},
                    "keys": {"type": "string", "description": "hotkey（如 'ctrl+c'）或 pywinauto 按键串（如 '{ENTER}'）"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "double": {"type": "boolean"},
                    "command": {"type": "string", "description": "launch 启动的程序路径或命令"},
                },
                ["action"],
            ),
            desktop_control,
        ),
        Tool(
            tool_spec(
                "file_ops",
                "跨平台 UTF-8 安全本地文件读写与目录浏览工具（避免命令行转义与编码乱码）。",
                {
                    "action": {"type": "string", "enum": ["read", "write", "append", "list_dir", "exists"]},
                    "path": {"type": "string", "description": "文件路径或目录路径（相对路径基于当前运行目录）"},
                    "content": {"type": "string", "description": "write 或 append 写入的文本内容"},
                },
                ["action", "path"],
            ),
            file_ops,
        ),
        Tool(
            tool_spec(
                "web_fetch",
                "极速 HTTP 请求与网页正文/JSON 提取工具（适合无需浏览器渲染的公开网页或 API 接口查询）。",
                {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "description": "返回最大字符数，默认 8000"},
                },
                ["url"],
            ),
            web_fetch,
        ),
        Tool(
            tool_spec(
                "memory_note",
                "将阶段性收集到的关键数据或结论写入持久化工作记忆与输出字典，即使经历多次上下文压缩也绝不丢失。",
                {
                    "action": {"type": "string", "enum": ["save", "get", "list"]},
                    "key": {"type": "string", "description": "记忆键名"},
                    "value": {"description": "任意可 JSON 序列化的值（字符串、数字、数组或对象）"},
                },
                ["action"],
            ),
            memory_note,
        ),
    ]
