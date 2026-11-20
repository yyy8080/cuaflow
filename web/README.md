# cuaflow Web 控制台

面向 `cuaflow serve` 的 React + Vite + TypeScript 单页应用（SPA）。界面语言：简体中文（zh-CN）。API 契约参见 [`../docs/api.md`](../docs/api.md)。

```bash
cd web
npm install
npm run dev        # http://localhost:5173 ；/api 与 /ws 会代理到 http://127.0.0.1:8765
npm run build      # 先进行类型检查，再构建输出到 ../src/cuaflow/server/static（构建前会先清空目录）
npx vitest run     # 运行单元测试与 jsdom 冒烟测试
```

开发模式下，首次用浏览器打开一次 `http://localhost:5173/?token=<cuaflow serve 打印的令牌>`；访问令牌会保存在 `localStorage["cuaflow_token"]` 中，并自动从地址栏移除。

## 目录结构

| 路径 | 说明 |
|---|---|
| `src/api.ts` | 契约类型定义与强类型 `fetch` 客户端（Bearer 鉴权、401 回调钩子、附带 `?token=` 的产物与 WebSocket 地址） |
| `src/ws.ts` | `EventSocket` / `useEventSocket`：连接 `/ws/events`、事件订阅、指数退避自动重连，以及用于补拉历史事件的 `onOpen` 回调 |
| `src/lib/runState.ts` | 将实时事件归并到 `Run` 快照的纯函数 reducer（幂等，按 `seq` 去重） |
| `src/lib/diff.ts` | 供 `DiffView` 使用的 unified diff 解析器 |
| `src/lib/params.ts` | 基于 `params_schema` 的表单值初始化与类型转换校验 |
| `src/pages/*` | 任务 `/`、脚本库 `/flows`、`/flows/:id?v=N`、执行记录 `/runs?flow_id=`、执行监控 `/runs/:id`、人工介入 `/tickets`、设置 `/settings` |
| `src/components/*` | StatusBadge、DiffView、CodeView/JsonView、StepTimeline、EventStream、ParamsForm、RunsTable、common 等通用组件 |

服务端必须对所有未知的非 `/api`、非 `/ws` 路径返回 `index.html`（SPA History 路由回退），以确保 `/runs/<id>` 等深层链接在页面刷新后仍能正常访问。
