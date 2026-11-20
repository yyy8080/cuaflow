"""cuaflow command line: serve the console, run long-horizon missions/flows, schedules, stats, and setup checks."""

from __future__ import annotations

import json
import secrets
import sys
import threading
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False, help="cuaflow — autonomous long-horizon browser + desktop agent with self-healing flows")
flows_app = typer.Typer(help="Manage the compiled flow library")
schedules_app = typer.Typer(help="Manage continuous scheduled executions & monitors")
config_app = typer.Typer(help="Configure models and policies")
app.add_typer(flows_app, name="flows")
app.add_typer(schedules_app, name="schedules")
app.add_typer(config_app, name="config")
console = Console()


def _parse_params(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise typer.BadParameter(f"expected key=value, got {item!r}")
        k, v = item.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _services(start_worker: bool = False, headless: Optional[bool] = None):
    from cuaflow.services import Services

    opts: dict[str, Any] = {}
    if headless is not None:
        opts["headless"] = headless
    return Services(start_worker=start_worker, runner_opts=opts)


def _console_operator(svc, unattended: bool = False) -> None:
    """Print live events (steps, L1.5/L2 checks, batch progress, plan updates) and optionally answer human tickets."""

    def on_event(ev: dict[str, Any]) -> None:
        t, d = ev["type"], ev.get("data", {})
        if t == "plan_updated":
            phases = d.get("phases") or []
            console.print(f"[magenta]🗺 规划更新 ({len(phases)} 阶段): "
                          f"{' -> '.join(p['title'] + '[' + p.get('status', '') + ']' for p in phases)}[/]")
        elif t == "batch_progress":
            console.print(f"[blue]⚡ 批量脚本执行 [{d.get('batch_id')}]: "
                          f"{d.get('completed')}/{d.get('total')} ({d.get('last_status')}) via {d.get('flow_id')}[/]")
        elif t == "step_start":
            console.print(f"[cyan]▶ step {d.get('index', 0) + 1}: {d.get('title')}[/]")
        elif t == "step_end":
            icon = {"passed": "✅", "repaired": "🩹", "failed": "❌", "skipped": "⏭", "repairing": "🔧"}.get(
                d.get("status"), "•")
            console.print(f"  {icon} {d.get('title')} [{d.get('status')}] {d.get('error') or ''}")
        elif t == "check":
            console.print(f"    [{d.get('level')}] {'ok' if d.get('passed') else 'FAIL'} {d.get('detail', '')[:200]}")
        elif t == "agent_log":
            style = {"thought": "dim", "command": "yellow", "observation": "dim"}.get(d.get("kind"), "")
            console.print(f"    [{style}]{d.get('role')}/{d.get('kind')}: {d.get('text', '')[:400]}[/]")
        elif t == "version_created":
            console.print(f"[green]★ new flow version v{d.get('version')} ({d.get('author')}): {d.get('reason')}[/]")
        elif t == "run_status":
            console.print(f"[bold]run {ev.get('run_id')}: {d.get('status')}[/] {d.get('error') or ''}")
        elif t == "ticket_open" and not unattended:
            threading.Thread(target=_answer_ticket, args=(svc, d), daemon=True).start()

    svc.bus.subscribe(on_event)


def _answer_ticket(svc, ticket: dict[str, Any]) -> None:
    console.print(f"\n[bold red]需要人工介入[/]: {ticket.get('reason')}")
    choice = typer.prompt("选择 [c]继续(已手动完成) / [h]提示后重试 / [s]跳过 / [a]终止", default="a")
    action = {"c": "continue", "h": "hint", "s": "skip", "a": "abort"}.get(choice.strip()[:1].lower(), "abort")
    hint = typer.prompt("提示 / 回答") if action == "hint" else None
    if action == "continue" and ticket.get("reason", "").startswith("Author"):
        action, hint = "hint", typer.prompt("回答")
    svc.tickets.resolve(ticket["id"], action, hint)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765, token: Optional[str] = typer.Option(None, envvar="CUAFLOW_TOKEN"),
          allow_remote: bool = typer.Option(False, help="allow binding to a non-loopback address")):
    """Start the web console + API server + continuous scheduler."""
    import uvicorn

    from cuaflow.platform.paths import data_dir
    from cuaflow.server import create_app

    if host not in ("127.0.0.1", "localhost", "::1") and not allow_remote:
        raise typer.BadParameter("refusing to bind to a non-loopback address without --allow-remote")
    tok_file = data_dir() / "token"
    if token is None:
        token = tok_file.read_text(encoding="utf-8").strip() if tok_file.is_file() else secrets.token_urlsafe(24)
        tok_file.write_text(token, encoding="utf-8")
    svc = _services(start_worker=True)
    console.print(f"[bold green]cuaflow console:[/] http://{host}:{port}/?token={token}")
    console.print(f"data dir: {svc.root}")
    uvicorn.run(create_app(svc, token), host=host, port=port, log_level="warning")


@app.command()
def do(
    instruction: str,
    start_url: Optional[str] = None,
    param: list[str] = typer.Option(None, "--param", "-p"),
    force_new: bool = False,
    mode: str = typer.Option("auto", help="auto | mission (long-horizon planner) | single (one flow)"),
    unattended: bool = typer.Option(False, help="autonomous unattended mode (never block on human tickets)"),
    headless: Optional[bool] = typer.Option(None, "--headless/--headed"),
):
    """Run a natural-language task: auto-plan long-horizon missions, reuse compiled flows, or record new ones."""
    svc = _services(headless=headless)
    _console_operator(svc, unattended=unattended)
    task = svc.orchestrator.submit_task(instruction, start_url, _parse_params(param), force_new, mode=mode,
                                        enqueue=False)
    task = svc.orchestrator.process_task(task["id"], unattended=unattended)
    console.print(f"\n[bold]task {task['status']}[/] ({task['id']}): {task.get('final_response') or ''}")
    if task.get("flow_id"):
        console.print(f"flow: {task['flow_id']}")
    u = task.get("usage") or {}
    if u:
        console.print(f"[dim]tokens used: {u.get('total_tokens', 0)} ({u.get('llm_calls', 0)} calls) | "
                      f"script steps: {u.get('script_steps', 0)} | "
                      f"est. tokens saved: {u.get('estimated_tokens_saved', 0)}[/]")
    raise typer.Exit(0 if task["status"] == "succeeded" else 1)


@app.command()
def resume(task_id: str, unattended: bool = typer.Option(False, help="autonomous unattended mode"),
           headless: Optional[bool] = typer.Option(None, "--headless/--headed")):
    """Resume an interrupted or failed long-horizon task from its saved checkpoint."""
    svc = _services(headless=headless)
    _console_operator(svc, unattended=unattended)
    svc.orchestrator.resume_task(task_id, enqueue=False)
    task = svc.orchestrator.process_task(task_id, resume=True, unattended=unattended)
    console.print(f"\n[bold]task {task['status']}[/]: {task.get('final_response') or ''}")
    raise typer.Exit(0 if task["status"] == "succeeded" else 1)


@app.command()
def run(
    flow_id: str,
    param: list[str] = typer.Option(None, "--param", "-p"),
    version: Optional[int] = None,
    from_step: Optional[str] = None,
    unattended: bool = typer.Option(False, help="autonomous unattended mode (never block on human tickets)"),
    headless: Optional[bool] = typer.Option(None, "--headless/--headed"),
):
    """Execute a recorded flow under supervision (with 4-tier self-healing)."""
    svc = _services(headless=headless)
    _console_operator(svc, unattended=unattended)
    r = svc.orchestrator.submit_run(flow_id, _parse_params(param), version=version, from_step=from_step,
                                    enqueue=False, unattended=unattended)
    r = svc.orchestrator.process_run(r["id"], from_step, unattended=unattended)
    console.print_json(json.dumps({"status": r["status"], "output": r.get("output"), "error": r.get("error"),
                                   "new_version": r.get("new_version"), "usage": r.get("usage")}, ensure_ascii=False))
    raise typer.Exit(0 if r["status"] == "succeeded" else 1)


@app.command()
def stats():
    """Show token consumption, script savings, and flow stability telemetry."""
    svc = _services()
    console.print_json(json.dumps(svc.store.global_stats(), ensure_ascii=False))


@flows_app.command("ls")
def flows_ls():
    svc = _services()
    t = Table("id", "name", "version", "runs", "ok", "saved_tokens", "params")
    for f in svc.store.list_flows():
        st = f["stats"]
        t.add_row(f["id"], f["name"], f"v{f['current_version']}", str(st["runs"]), str(st["successes"]),
                  str(st.get("estimated_tokens_saved", 0)), ", ".join(f["params_schema"]))
    console.print(t)


@flows_app.command("show")
def flows_show(flow_id: str, version: Optional[int] = None, diff: bool = False):
    svc = _services()
    f = svc.store.get_flow(flow_id)
    if not f:
        raise typer.BadParameter(f"unknown flow {flow_id}")
    v = svc.store.get_version(flow_id, version or f["current_version"])
    for ver in svc.store.list_versions(flow_id):
        console.print(f"v{ver['version']} [{ver['author']}] {ver['reason']}  health={ver['health']}")
    console.rule(f"v{v['version']}")
    console.print(v["diff"] if diff else v["source"], markup=False, highlight=False)


@flows_app.command("rollback")
def flows_rollback(flow_id: str, version: int):
    svc = _services()
    svc.store.set_current_version(flow_id, version)
    console.print(f"{flow_id} -> v{version}")


@flows_app.command("export")
def flows_export(flow_id: str, out: Path, version: Optional[int] = None):
    """Write the flow source to a file (it can be run standalone with `cuaflow run-file`)."""
    svc = _services()
    out.write_text(svc.store.read_source(flow_id, version), encoding="utf-8")
    console.print(f"written {out}")


@schedules_app.command("ls")
def schedules_ls():
    svc = _services()
    t = Table("id", "name", "kind", "target", "interval(s)", "status", "runs(ok)", "consec_fail", "next_run_at")
    for sc in svc.store.list_schedules():
        target = sc.get("flow_id") or (sc.get("instruction") or "")[:30]
        t.add_row(
            sc["id"], sc["name"], sc["kind"], str(target), str(sc["interval_seconds"]),
            str(sc["status"]), f"{sc['runs_count']}({sc['success_count']})",
            str(sc["consecutive_failures"]), str(sc.get("next_run_at") or "-"),
        )
    console.print(t)


@schedules_app.command("add")
def schedules_add(
    name: str,
    interval: int = typer.Option(300, "--interval", "-i", help="interval in seconds"),
    flow_id: Optional[str] = typer.Option(None, "--flow"),
    instruction: Optional[str] = typer.Option(None, "--task"),
    start_url: Optional[str] = None,
    param: list[str] = typer.Option(None, "--param", "-p"),
    max_runs: Optional[int] = None,
    max_failures: int = 3,
):
    """Create a continuous scheduled flow/task monitor (runs when `cuaflow serve` is active)."""
    svc = _services()
    if not flow_id and not instruction:
        raise typer.BadParameter("specify either --flow <flow_id> or --task <instruction>")
    kind = "flow" if flow_id else "task"
    sc = svc.store.create_schedule(
        name,
        interval,
        kind=kind,
        flow_id=flow_id,
        instruction=instruction,
        start_url=start_url,
        params=_parse_params(param),
        max_runs=max_runs,
        max_consecutive_failures=max_failures,
    )
    console.print_json(json.dumps(sc, ensure_ascii=False))


@schedules_app.command("rm")
def schedules_rm(schedule_id: str):
    svc = _services()
    svc.store.delete_schedule(schedule_id)
    console.print(f"deleted {schedule_id}")


@app.command("run-file")
def run_file(path: Path, param: list[str] = typer.Option(None, "--param", "-p"),
             run_dir: Path = typer.Option(Path("cuaflow_run"), help="artifacts directory")):
    """Run a flow file directly without supervision or LLMs (plain replay)."""
    from cuaflow.flow.client import RunnerClient

    with RunnerClient(log_path=run_dir / "runner.log") as rc:
        meta = rc.load(path.resolve(), _parse_params(param), run_dir.resolve())
        for st in meta["steps"]:
            r = rc.run_step(st["id"], timeout=float(st.get("timeout") or 120))
            console.print(f"{'✅' if r['status'] == 'passed' else '❌'} {st['title']} {r.get('error') or ''}")
            if r["status"] != "passed":
                raise typer.Exit(1)
        console.print_json(json.dumps(rc.finish()["output"], ensure_ascii=False))


@config_app.command("model")
def config_model(model: str, base_url: str = typer.Option(..., help="OpenAI-compatible base URL, e.g. "
                                                                      "https://api.deepseek.com/v1"),
                 api_key: str = typer.Option("", help="optional direct API key stored locally in settings.json"),
                 api_key_env: str = "OPENAI_API_KEY", role: str = typer.Option("default", help="default|planner|author|"
                                                                                               "supervisor|repairer|fallback|vision_judge|matcher"),
                 vision: bool = True, tool_mode: str = "native", api: str = "chat"):
    """Set the model for a role."""
    from cuaflow.config import ModelSpec, load_settings, save_settings

    s = load_settings()
    prev = s.models.default if role == "default" else s.models.roles.get(role)
    eff_key = api_key if api_key != "" else (prev.api_key if prev else "")
    spec = ModelSpec(model=model, base_url=base_url, api_key=eff_key, api_key_env=api_key_env, vision=vision,
                     tool_mode=tool_mode, api=api)
    if role == "default":
        s.models.default = spec
    else:
        s.models.roles[role] = spec
    save_settings(s)
    src_label = "direct key" if eff_key else f"key from ${api_key_env}"
    console.print(f"{role}: {model} @ {base_url} ({src_label})")


@config_app.command("show")
def config_show():
    from cuaflow.config import load_settings, masked_settings_dict, settings_path

    console.print(f"# {settings_path()}")
    console.print_json(json.dumps(masked_settings_dict(load_settings()), ensure_ascii=False))


@app.command("install-browser")
def install_browser():
    """Download standalone Playwright Chromium (optional when system Edge/Chrome is already installed)."""
    from cuaflow.platform.browser.session import install_playwright_chromium

    code = install_playwright_chromium()
    raise typer.Exit(code)


@app.command()
def doctor(ping: bool = typer.Option(False, help="send a tiny request to each configured model")):
    """Check the installation: runtime, browser (Chromium/Edge/Chrome), Windows automation libs, model keys."""
    from cuaflow.config import load_settings
    from cuaflow.platform.base import IS_WINDOWS
    from cuaflow.platform.browser.session import resolve_browser_executable

    ok = True

    def row(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        console.print(f"{'✅' if good else '❌'} {name} {detail}")

    runtime_label = "frozen standalone" if getattr(sys, "frozen", False) else sys.version.split()[0]
    row("python >= 3.11", sys.version_info >= (3, 11), runtime_label)
    try:
        exe, ch = resolve_browser_executable()
        row(f"browser ({ch})", Path(exe).exists(), exe)
    except Exception as exc:  # noqa: BLE001
        row("browser", False, str(exc))
    if IS_WINDOWS:
        for mod in ("pywinauto", "uiautomation", "pyautogui", "mss"):
            try:
                __import__(mod)
                row(f"module {mod}", True)
            except ImportError:
                row(f"module {mod}", False, "pip install -e .[win]")
    else:
        console.print("ℹ️  not Windows: desktop automation uses the in-memory FakeDesktop")
    s = load_settings()
    specs = {"default": s.models.default, **s.models.roles}
    for role, spec in specs.items():
        has_key = bool(spec.resolved_api_key()) or "localhost" in spec.base_url or "127.0.0.1" in spec.base_url
        key_desc = "configured" if spec.api_key.strip() else f"key=${spec.api_key_env}{'' if has_key else ' (not set)'}"
        row(f"model[{role}] {spec.model}", has_key, f"{spec.base_url} {key_desc}")
        if ping and has_key:
            from cuaflow.llm import build_llm

            try:
                r = build_llm(spec).chat([{"role": "user", "content": "Reply with OK"}], max_tokens=5)
                row(f"  ping {role}", True, r.content[:20])
            except Exception as exc:  # noqa: BLE001
                row(f"  ping {role}", False, str(exc)[:200])
    raise typer.Exit(0 if ok else 1)


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "--runner-proc":
        from cuaflow.flow.proc import main as runner_main

        raise SystemExit(runner_main())
    app()


if __name__ == "__main__":
    main()
