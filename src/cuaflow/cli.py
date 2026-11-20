"""cuaflow command line: serve the console, run tasks/flows, manage the library, diagnose the setup."""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False, help="cuaflow — browser + Windows desktop agent with self-repairing flows")
flows_app = typer.Typer(help="Manage the flow library")
config_app = typer.Typer(help="Configure models and policies")
app.add_typer(flows_app, name="flows")
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


def _console_operator(svc) -> None:
    """Print live events and answer human tickets on the terminal."""

    def on_event(ev: dict[str, Any]) -> None:
        t, d = ev["type"], ev.get("data", {})
        if t == "step_start":
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
        elif t == "ticket_open":
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
    """Start the web console + API server."""
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
def do(instruction: str, start_url: Optional[str] = None, param: list[str] = typer.Option(None, "--param", "-p"),
       force_new: bool = False, headless: Optional[bool] = typer.Option(None, "--headless/--headed")):
    """Run a natural-language task: reuse a matching flow or record a new one."""
    svc = _services(headless=headless)
    _console_operator(svc)
    task = svc.orchestrator.submit_task(instruction, start_url, _parse_params(param), force_new)
    task = svc.orchestrator.process_task(task["id"])
    console.print(f"\n[bold]task {task['status']}[/]: {task.get('final_response') or ''}")
    if task.get("flow_id"):
        console.print(f"flow: {task['flow_id']}")
    raise typer.Exit(0 if task["status"] == "succeeded" else 1)


@app.command()
def run(flow_id: str, param: list[str] = typer.Option(None, "--param", "-p"), version: Optional[int] = None,
        from_step: Optional[str] = None, headless: Optional[bool] = typer.Option(None, "--headless/--headed")):
    """Execute a recorded flow under supervision (with automatic repair)."""
    svc = _services(headless=headless)
    _console_operator(svc)
    r = svc.orchestrator.submit_run(flow_id, _parse_params(param), version=version, from_step=from_step,
                                    enqueue=False)
    r = svc.orchestrator.process_run(r["id"], from_step)
    console.print_json(json.dumps({"status": r["status"], "output": r.get("output"), "error": r.get("error"),
                                   "new_version": r.get("new_version")}, ensure_ascii=False))
    raise typer.Exit(0 if r["status"] == "succeeded" else 1)


@flows_app.command("ls")
def flows_ls():
    svc = _services()
    t = Table("id", "name", "version", "runs", "ok", "params")
    for f in svc.store.list_flows():
        st = f["stats"]
        t.add_row(f["id"], f["name"], f"v{f['current_version']}", str(st["runs"]), str(st["successes"]),
                  ", ".join(f["params_schema"]))
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
                 api_key_env: str = "OPENAI_API_KEY", role: str = typer.Option("default", help="default|author|"
                                                                                               "supervisor|repairer|vision_judge|matcher"),
                 vision: bool = True, tool_mode: str = "native", api: str = "chat"):
    """Set the model for a role."""
    from cuaflow.config import ModelSpec, load_settings, save_settings

    s = load_settings()
    spec = ModelSpec(model=model, base_url=base_url, api_key_env=api_key_env, vision=vision, tool_mode=tool_mode,
                     api=api)
    if role == "default":
        s.models.default = spec
    else:
        s.models.roles[role] = spec
    save_settings(s)
    console.print(f"{role}: {model} @ {base_url} (key from ${api_key_env})")


@config_app.command("show")
def config_show():
    from cuaflow.config import load_settings, settings_path

    console.print(f"# {settings_path()}")
    console.print_json(load_settings().model_dump_json())


@app.command()
def doctor(ping: bool = typer.Option(False, help="send a tiny request to each configured model")):
    """Check the installation: Python, Playwright browser, Windows automation libs, model keys."""
    from cuaflow.config import load_settings
    from cuaflow.platform.base import IS_WINDOWS

    ok = True

    def row(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        console.print(f"{'✅' if good else '❌'} {name} {detail}")

    row("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = p.chromium.executable_path
        row("playwright chromium", Path(exe).exists(), exe)
    except Exception as exc:  # noqa: BLE001
        row("playwright chromium", False, str(exc))
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
        has_key = bool(os.environ.get(spec.api_key_env)) or "localhost" in spec.base_url or "127.0.0.1" in spec.base_url
        row(f"model[{role}] {spec.model}", has_key, f"{spec.base_url} key=${spec.api_key_env}"
                                                    f"{'' if has_key else ' (not set)'}")
        if ping and has_key:
            from cuaflow.llm import build_llm

            try:
                r = build_llm(spec).chat([{"role": "user", "content": "Reply with OK"}], max_tokens=5)
                row(f"  ping {role}", True, r.content[:20])
            except Exception as exc:  # noqa: BLE001
                row(f"  ping {role}", False, str(exc)[:200])
    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
