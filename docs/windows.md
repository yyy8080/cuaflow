# 在 Windows 上运行 cuaflow

## 运行前提
- Windows 10 21H2+ / Windows 11，Python 3.11+。
- 必须在**已登录的交互式桌面**里运行 `cuaflow serve` 或 `cuaflow do`：UI 自动化（UIA）、鼠标键盘模拟和截图都依赖可见的桌面会话。远程桌面（RDP）断开后桌面会被锁定，自动化就会失败。需要长时间无人值守时，建议改用控制台会话或开启自动登录。
- 如果 DPI 缩放不是 100%，平台层会自动启用 Per-Monitor DPI 感知，保证坐标和截图对齐。
- 要操作以管理员身份运行的程序，cuaflow 本身也需要以管理员身份启动（受 UIPI 限制）。

## 浏览器
- 默认使用 Playwright 自带的 Chromium（需执行 `playwright install chromium`）。
- 想用系统自带的 Edge，可以设置 `CUAFLOW_BROWSER_CHANNEL=msedge`；也可以用 `CUAFLOW_BROWSER_EXECUTABLE` 指定浏览器路径。
- 浏览器以独立进程启动，通过 CDP 连接，并使用持久化的用户目录（`CUAFLOW_BROWSER_PROFILE`）。所以 Runner 重启后浏览器不会关闭，登录状态也会保留。

## 桌面能力（`c.desktop`）
- 基于 pywinauto（UIA 后端）和 uiautomation：支持查找窗口和控件（`title`、`auto_id`、`control_type`）、点击、输入、读取文本，以及导出控件树（`ui_tree`）供智能体观察。
- 用 mss 截图；坐标点击（pyautogui）只作为最后手段。
- `c.desktop.dismiss_common_dialogs()` 用来处理常见的确认弹窗（“是否保存”“覆盖”等）。
- `c.desktop.excel_count_rows(path)` 用 openpyxl 读取 xlsx 文件，用来校验结果。

## Shell（`c.shell`）
- 使用 PowerShell，命令以 `-EncodedCommand` 方式传递，避免引号转义问题。
- 命中黑名单的命令会被拒绝（退出码 126），超时返回退出码 124。黑名单可以在 Web 控制台的“设置 → 安全”里修改。

## 数据目录
默认使用 `%LOCALAPPDATA%\cuaflow`（可以用 `CUAFLOW_DATA_DIR` 修改），里面包括：
- `cuaflow.db`：流程、版本、运行记录、工单；
- `flows/<id>/versions/vN/flow.py`：每个版本的脚本；
- `runs/<run_id>/`：每一步的截图、`steps.jsonl` 检查点和日志；
- `token`：Web 控制台的访问令牌。

## 手动验收清单
1. `cuaflow doctor`：确认 pywinauto、uiautomation、浏览器和模型 key 都显示 OK。
2. `pytest -m windows tests/platform`：运行记事本冒烟测试。
3. `cuaflow run-file examples/notepad_flow.py -p text=hello -p path=C:\Temp\a.txt`：在不调用模型的情况下直接回放。
4. `cuaflow serve`，然后在控制台下达任务“用记事本把 hello 保存到 C:\Temp\b.txt”：观察录制过程，确认生成了 v1。
5. 再下达一个相似任务（换一个路径）：应当直接匹配到 v1 并回放，不进入录制。
6. 自愈测试：录制一个网页 flow，然后修改页面（比如改按钮文字、加一个弹窗）后再运行。应当看到对应步骤失败、被修复后重试通过，并生成 v2；在“版本”页可以看到 diff，并能回滚。
