# cuaflow

**cuaflow** 是一个**对话式多智能体并行协同的浏览器与 Windows 桌面操作工作台**。项目 fork 自 [microsoft/CUAWright](https://github.com/microsoft/CUAWright)（MIT）并深度重构。

### 核心设计理念
1. **主智能体对话调度 + 多子智能体真并行执行（`CoordinatorAgent` + `SubagentPool`）**：
   - 用户主要通过自然语言与**主控智能体**对话；主智能体按需将任务拆解并同时派发给多个专业子智能体并发执行：
     - `explorer`（**默认浏览器直连**）：分配独立隔离的浏览器会话槽位（`BrowserSlotPool`），多智能体真并行操作各自网页，互不抢占标签页，**无录制脚本额外开销**；
     - `desktop`（**原生桌面直连**）：直接操控 Windows UIA 桌面控件与命令行，自动串行互斥物理鼠标键盘；
     - `supervisor`（**低 Token 脚本监督**）：高速回放脚本库已有 `Flow`，配备 L0–L3 监督与四级自愈；
     - `author`（**按需脚本录制**）：仅在明确需要沉淀可重复脚本时启动“边做边编译”；
     - `planner`（**长程批量规划**）：面向跨阶段、大批量循环与断点续跑使命。
   - **中途动态调整与一键放行**：子智能体后台并发运行期间对话输入框永不阻塞，用户可随时发消息实时注入**中途调整指令（Steering）**，或对关键敏感步骤进行**一键放行 / 指导 / 跳过 / 终止**。
2. **单窗口一体化桌面 WebUI（免额外启动独立 Web 控制台）**：
   - 双击 `cuaflow-desktop.exe` 即在进程内静默启动后端并直接弹出**单一原生桌面 WebUI 窗口**（`pywebview` WebView2 / 无边框独立 App 窗口），关闭窗口自动退出，无需再额外管理控制器小窗或浏览器标签页。
3. **录制脚本省 Token + 五层监督四级自愈 + 持续巡检熔断**：
   - 高频流程沉淀为参数化 `Flow` 脚本，配合 `run_batch` 首条编译、后续零探索批量执行，大幅降低大模型 Token 消耗；
   - **L0–L1–L1.5（零 Token 浏览器哨兵）–L2（自适应视觉裁判）–L3** 监督体系 + **Tier 0~3 四级自愈**（零 Token 清障重连 → `Repairer` 单步热修补 → `FallbackAgent` AI 自主接管 → 人工协同工单）+ `Schedules` 定时巡检熔断保护。

```mermaid
flowchart TD
  U["用户自然语言对话 / 中途调整 / 放行确认"] <--> COORD["Coordinator 主控智能体 (一体化单窗口 WebUI)"]
  COORD -- "真并行派发 (独立浏览器槽位池)" --> EXP["Explorer 浏览器直连子智能体 x N"]
  COORD -- "本地 GUI / 命令行" --> DSK["Desktop 桌面直连子智能体"]
  COORD -- "复用已有脚本 (0 探索 Token)" --> SUP["Supervisor 监督执行 + 四级自愈"]
  COORD -- "按需沉淀可复用脚本" --> AU["Author 边做边录制 Flow"]
  COORD -- "超长程分页批量" --> MP["MissionPlanner 检查点续跑 + run_batch"]
```

## 安装（Windows 10/11 / Linux）

### 方式一：Windows 安装包直接安装（推荐，免装 Python / Node.js）

1. 前往 [GitHub Releases](https://github.com/yyy8080/cuaflow/releases) 下载 **`cuaflow-setup-0.3.0-win64.exe`**（或绿色免安装版 `cuaflow-portable-0.3.0-win64.zip`）。
2. 双击运行安装向导（无需管理员权限，自动创建桌面快捷方式并可选加入 `PATH` 与开机自启持续巡检）。
3. 双击桌面 **cuaflow** 图标，直接打开**一体化多智能体对话工作台窗口**；点击左侧 **「⚙️ 设置」** 直接粘贴模型 API Key 保存即可开始与主智能体对话（自动调用系统自带 Microsoft Edge / Chrome，无需额外下载浏览器内核）。

### 方式二：本地一键打包生成安装包

```powershell
# Windows（生成 dist\cuaflow-setup-0.3.0-win64.exe 与便携版 .zip，需本机已装 Python 3.11+、Node.js 与 Inno Setup 6）
powershell -ExecutionPolicy Bypass -File .\packaging\build_windows.ps1
# 如需把独立 Chromium 一并打进全离线包，加 -BundleChromium 参数：
powershell -ExecutionPolicy Bypass -File .\packaging\build_windows.ps1 -BundleChromium
```

### 方式三：源码开发安装

```powershell
# Python 3.11+
git clone https://github.com/yyy8080/cuaflow; cd cuaflow
python -m venv .venv; .venv\Scripts\activate
pip install -e ".[win]"            # Linux 仅浏览器模式：pip install -e .
playwright install chromium        # Windows 下若已有 Edge/Chrome 可省略此步
cuaflow doctor                     # 检查环境
```

> Windows 桌面自动化需在**已登录的交互式桌面会话**中运行，详见 [docs/windows.md](docs/windows.md)。

## 配置模型（兼容 OpenAI Chat Completions 接口）

可在 Web 控制台 **「设置」** 页直接粘贴 API Key 保存，或通过命令行配置：

```powershell
cuaflow config model gpt-4.1 --base-url https://api.openai.com/v1 --api-key "sk-..."
# 或从环境变量读取（支持按角色独立配置 default | planner | author | supervisor | repairer | fallback | vision_judge | matcher）：
cuaflow config model qwen-vl-max --role vision_judge --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 --api-key-env DASHSCOPE_API_KEY
cuaflow doctor --ping
```

## 使用

```powershell
# 1. 启动 Web 控制台 + 后台持续监控调度器
cuaflow serve                      # 打开终端输出的 http://127.0.0.1:8765/?token=...

# 2. 执行自然语言任务（自动路由：命中已有脚本 / 长任务自动规划 + 批量编译脚本 / 单流程录制）
cuaflow do "登录后台，遍历所有分页收集待审订单，逐条核验并汇总金额" --unattended
cuaflow do "导出本月销售报表" --mode auto

# 3. 长任务中断后从检查点无损续跑（已完成的阶段和批处理项不会重复执行）
cuaflow resume <task_id> --unattended

# 4. 查看沉淀脚本、Token 节省统计与持续监控计划
cuaflow stats
cuaflow flows ls
cuaflow run <flow_id> -p q=华东 --unattended
cuaflow schedules add "每5分钟订单巡检" --interval 300 --flow <flow_id>
cuaflow schedules ls
```

## 监督层级与自愈机制

| 层级 | 检查内容 | Token 消耗 |
|---|---|---|
| **L0** | 步骤异常、超时、Runner 子进程崩溃（崩溃自动从 `steps.jsonl` 检查点恢复） | **0** |
| **L1** | 步骤内 `expect.*` 确定性断言 | **0** |
| **L1.5（哨兵）** | 主文档 HTTP 4xx/5xx、网络错误页、未捕获 JS 异常、后台接口报错、意外跳转登录页、告警横幅、浏览器 `alert` 弹窗、白屏、DOM 无变化卡死、人机验证安全拦截 | **0** |
| **L2（视觉/语义）** | 默认 `on_change`（仅新步骤/修补步骤检查，通过后置零开销）；当 **L1.5 哨兵发现可疑信号**或达到定期抽检周期时**自适应唤醒** | 按需 1 次调用 |
| **L3（契约+终验）** | `output_schema` 输出字段非空契约校验（0 Token）+ 任务级最终核验 | 0 或 1 次调用 |

## 安全边界

- 服务默认只监听 `127.0.0.1`，所有 API 与 WebSocket 强制校验访问令牌。
- Shell 命令经过危险模式黑名单拦截。
- **人机验证安全红线**：遇到 CAPTCHA / Turnstile / 人机验证页面时，L1.5 哨兵被动识别后**立即停止自动操作**，绝不尝试绕过或破解，安全移交人工或终止报错。

## 开发与测试

```bash
pip install -e ".[test]" && playwright install chromium
pytest -q                                   # 含真实无头 Chromium 长任务批量编译、L1.5 哨兵、会话过期自主接管、断点续跑端到端测试
cd web && npm ci && npm run build && npx vitest run
```

REST/WebSocket 接口契约见 [docs/api.md](docs/api.md)。

## 许可

MIT。本项目基于 microsoft/CUAWright，详见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
