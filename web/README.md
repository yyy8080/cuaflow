# cuaflow web console

React + Vite + TypeScript SPA for `cuaflow serve`. UI language: zh-CN. API contract: [`../docs/api.md`](../docs/api.md).

```bash
cd web
npm install
npm run dev        # http://localhost:5173 ; /api and /ws are proxied to http://127.0.0.1:8765
npm run build      # type-checks, then builds into ../src/cuaflow/server/static (emptied first)
npx vitest run     # unit + jsdom smoke tests
```

In dev, open `http://localhost:5173/?token=<token printed by cuaflow serve>` once; the token is
stored in `localStorage["cuaflow_token"]` and removed from the address bar.

## Layout

| Path | Purpose |
|---|---|
| `src/api.ts` | Contract types + typed fetch client (Bearer auth, 401 hook, artifact/ws URLs with `?token=`) |
| `src/ws.ts` | `EventSocket` / `useEventSocket`: `/ws/events`, subscribe, auto-reconnect with backoff, `onOpen` for backfill |
| `src/lib/runState.ts` | Pure reducer folding events into a `Run` (idempotent, de-duplicated by `seq`) |
| `src/lib/diff.ts` | Unified-diff parser for `DiffView` |
| `src/lib/params.ts` | `params_schema` form value initialisation / coercion |
| `src/pages/*` | 任务 `/`, 脚本库 `/flows`, `/flows/:id?v=N`, 执行记录 `/runs?flow_id=`, 执行监控 `/runs/:id`, 人工介入 `/tickets`, 设置 `/settings` |
| `src/components/*` | StatusBadge, DiffView, CodeView/JsonView, StepTimeline, EventStream, ParamsForm, RunsTable, common |

The server must serve `index.html` for unknown non-`/api`, non-`/ws` paths (SPA history fallback) so deep links
such as `/runs/<id>` work after a reload.
