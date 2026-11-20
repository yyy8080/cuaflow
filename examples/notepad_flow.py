"""示例：用记事本写入一段文字并保存到指定路径（Windows）。

直接回放（不经监督者）：
    cuaflow run-file examples/notepad_flow.py -p text="你好 cuaflow" -p path="C:\\Temp\\hello.txt"
"""

from cuaflow.flow import Ctx, Flow, expect

flow = Flow(
    "记事本保存文本",
    params={
        "text": {"type": "str", "description": "要写入的文字", "example": "你好 cuaflow"},
        "path": {"type": "str", "description": "保存路径", "example": r"C:\Temp\hello.txt"},
    },
    output={"path": "str"},
    intent="用记事本把 {text} 保存到 {path}",
)

NOTEPAD = r".*(记事本|Notepad)"


@flow.step("打开记事本", expect="出现空白的记事本窗口", timeout=30)
def open_notepad(c: Ctx):
    c.desktop.launch("notepad.exe")
    win = c.desktop.window(title_re=NOTEPAD, timeout=15)
    win.wait("ready", timeout=15)
    c.state["opened"] = True
    expect.window_exists(c.desktop, NOTEPAD)


@flow.step("输入文字", expect="记事本编辑区显示要写入的文字")
def type_text(c: Ctx):
    win = c.desktop.window(title_re=NOTEPAD, timeout=10)
    c.desktop.activate(win)
    # Win11 新版记事本是 Document 控件，旧版是 Edit 控件
    edit = win.child_window(control_type="Document")
    if not edit.exists(timeout=1):
        edit = win.child_window(control_type="Edit")
    edit.set_edit_text(c.params.text)
    expect.contains(edit.window_text(), c.params.text)


@flow.step("另存为指定路径", expect="文件已保存，标题栏显示文件名", side_effect=True)
def save_as(c: Ctx):
    import os

    os.makedirs(os.path.dirname(c.params.path) or ".", exist_ok=True)
    win = c.desktop.window(title_re=NOTEPAD, timeout=10)
    c.desktop.activate(win)
    c.desktop.hotkey("ctrl", "shift", "s")
    dlg = c.desktop.window(title_re=r".*(另存为|Save As).*", timeout=10)
    dlg.child_window(control_type="Edit", found_index=0).set_edit_text(c.params.path)
    c.desktop.type_keys("{ENTER}")
    # 覆盖确认对话框（文件已存在时）
    c.desktop.dismiss_common_dialogs()
    expect.file_contains(c.params.path, c.params.text)
    c.output["path"] = c.params.path
