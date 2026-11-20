"""FakeDesktop 模拟桌面行为以及 Windows 桌面后端中与操作系统无关的逻辑测试。"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from cuaflow.platform import DesktopBackend, FakeDesktop, WindowInfo, default_desktop
from cuaflow.platform.windows import (
    DesktopError,
    WindowsDesktop,
    dismiss_common_dialogs,
    excel_count_rows,
    normalize_button_name,
    wait_window_gone,
)
from cuaflow.platform.windows.input import escape_send_keys, hotkey_to_send_keys
from cuaflow.platform.windows.uia import NodeProps, dump_tree, format_node


def test_fake_desktop_protocol_and_basics(tmp_path):
    d = FakeDesktop()
    assert isinstance(d, DesktopBackend)
    pid = d.launch("notepad.exe", ["a.txt"])
    assert pid >= 1000
    assert [w.title for w in d.windows()] == ["notepad"]
    win = d.window(title_re="note")
    assert win.window_text() == "notepad"
    win.type_keys("hello")
    assert ("type_keys", "notepad", "hello") in d.events
    d.type_text("中文")
    d.hotkey("ctrl", "s")
    d.click(1, 2)
    d.scroll(3, 4, -2)
    assert d.events[-3:] == [("hotkey", ("ctrl", "s")), ("click", 1, 2, "left", False), ("scroll", 3, 4, -2)]
    assert d.foreground().title == "notepad"
    assert "notepad" in d.ui_tree()
    shot = d.screenshot(tmp_path / "s.png")
    assert shot.exists() and shot.read_bytes().startswith(b"\x89PNG")
    d.close_window(win)
    assert d.windows() == []
    with pytest.raises(TimeoutError):
        d.window(title="nope", timeout=0.1)


def test_fake_modal_and_dismiss():
    d = FakeDesktop()
    d.add_window("Main", "app.exe")
    d.add_window("Save changes?", "app.exe", modal=True)
    assert [m.title for m in d.modal_dialogs()] == ["Save changes?"]
    done = dismiss_common_dialogs(d, settle=0)
    assert done == ["'Save changes?' -> closed"]
    assert d.modal_dialogs() == []
    assert dismiss_common_dialogs(d, settle=0) == []


def test_wait_window_gone_on_fake():
    d = FakeDesktop()
    d.add_window("Busy", "app.exe")
    assert wait_window_gone(d, title="Busy", timeout=0.3) is False
    t = threading.Timer(0.3, lambda: d.remove_window("Busy"))
    t.start()
    assert wait_window_gone(d, title_re="^Bu", timeout=3) is True
    w = d.add_window("Other", "x.exe")
    win = d.window(title="Other")
    threading.Timer(0.2, lambda: d.remove_window("Other")).start()
    assert wait_window_gone(d, win, timeout=3) is True
    with pytest.raises(ValueError):
        wait_window_gone(d)
    assert isinstance(w, WindowInfo)


def test_default_desktop_is_fake_off_windows():
    if sys.platform == "win32":
        pytest.skip("native host")
    assert isinstance(default_desktop(), FakeDesktop)
    with pytest.raises(DesktopError):
        WindowsDesktop()


def test_format_node_and_dump_tree():
    line = format_node(NodeProps("Button", name="保存", auto_id="1", class_name="Button",
                                 rect=(1, 2, 3, 4), enabled=True, focused=True, value="v"))
    assert line == "Button name='保存' auto_id='1' class='Button' rect=(1,2,3,4) enabled focused value='v'"
    assert "disabled" in format_node(NodeProps("Edit", enabled=False))

    # 控件树结构：根节点 -> 3 个子节点 -> 每个子节点下各有 3 个孙节点
    tree = {"name": "root", "kids": [{"name": f"c{i}", "kids": [{"name": f"g{i}{j}", "kids": []}
                                                                   for j in range(3)]} for i in range(3)]}
    props = lambda n: NodeProps("Pane", name=n["name"])  # noqa: E731
    kids = lambda n: n["kids"]  # noqa: E731
    full = dump_tree(tree, props, kids, max_depth=8, max_nodes=100)
    assert full.count("\n") == 12
    assert "    Pane name='g00'" in full
    shallow = dump_tree(tree, props, kids, max_depth=1, max_nodes=100)
    assert "g00" not in shallow and "[+3 children beyond max_depth]" in shallow and "max_depth=1" in shallow
    capped = dump_tree(tree, props, kids, max_depth=8, max_nodes=5)
    assert len([ln for ln in capped.splitlines() if ln.strip().startswith("Pane")]) == 5
    assert "truncated" in capped.splitlines()[-1]


def test_input_helpers():
    assert escape_send_keys("a+b {x} 100%") == "a{+}b {{}x{}} 100{%}"
    assert hotkey_to_send_keys("ctrl", "s") == "{VK_CONTROL down}{s down}{s up}{VK_CONTROL up}"
    assert hotkey_to_send_keys("Alt", "F4") == "{VK_MENU down}{VK_F4 down}{VK_F4 up}{VK_MENU up}"
    with pytest.raises(ValueError):
        hotkey_to_send_keys("hyper")


def test_normalize_button_name():
    assert normalize_button_name("否(&N)") == "否"
    assert normalize_button_name("不保存(N)") == "不保存"
    assert normalize_button_name("Don\u2019t Save") == "don't save"
    assert normalize_button_name("&Cancel") == "cancel"
    assert normalize_button_name("确定") == "确定"


def test_excel_count_rows_csv(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("h1,h2\n1,2\n\n3,4\n", encoding="utf-8")
    assert excel_count_rows(p) == 3
    with pytest.raises(FileNotFoundError):
        excel_count_rows(tmp_path / "missing.xlsx")


def test_excel_count_rows_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["a", "b"])
    ws.append([1, 2])
    ws.append([None, None])
    ws.append([3, None])
    wb.save(tmp_path / "x.xlsx")
    assert excel_count_rows(tmp_path / "x.xlsx") == 3


# ---------------------------------------------------------------------------
# Windows 冒烟测试（需要真实的 Windows 桌面会话）
# ---------------------------------------------------------------------------


@pytest.mark.windows
def test_windows_notepad_smoke(tmp_path):
    d = WindowsDesktop()
    d.launch("notepad.exe")
    win = d.window(title_re=r"(Notepad|记事本|无标题|Untitled)", timeout=20)
    d.activate(win)
    time.sleep(0.5)
    d.type_text("hello 中文 cuaflow")
    tree = d.ui_tree(win)
    assert "Edit" in tree or "Document" in tree
    assert any("notepad" in w.process.lower() for w in d.windows())
    shot = d.screenshot(tmp_path / "np.png", window=win)
    assert shot.exists()
    d.close_window(win)
    time.sleep(1.0)
    clicked = d.dismiss_common_dialogs()
    # 经典版记事本会弹出“是否保存更改？”提示；Windows 11 新版记事本则可能静默保留会话内容。
    if clicked:
        assert any(k in c for c in clicked for k in ("Don't", "不保存", "否", "No")), clicked
    assert wait_window_gone(d, win, timeout=10)
