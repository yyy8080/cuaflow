"""Tools shared by Author and Repairer, plus the step verifier (Supervisor L0/L1/L2 checks)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from cuaflow.agents.judge import judge_step
from cuaflow.agents.loop import Tool, ToolResult
from cuaflow.flow.client import RunnerClient, RunnerDied, RunnerError, RunnerTimeout
from cuaflow.flow.engine import latest_checkpoint_state, now_iso
from cuaflow.llm.base import LLM, tool_spec
from cuaflow.platform.base import IS_WINDOWS, ShellBackend

PROMPTS = Path(__file__).parent / "prompts"


def load_prompt(name: str, **kw: Any) -> str:
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    kw.setdefault("platform", "Windows" if IS_WINDOWS else "Linux")
    kw.setdefault("shell", "PowerShell" if IS_WINDOWS else "bash")
    if "{flow_api}" in text:
        kw.setdefault("flow_api", (PROMPTS / "flow_api.md").read_text(encoding="utf-8"))
    for k, v in kw.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def check(level: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"level": level, "passed": passed, "detail": detail, "ts": now_iso()}


class StepVerifier:
    """Turns a raw runner step result into Supervisor checks and a pass/fail decision."""

    def __init__(self, judge_llm: LLM | None, run_dir: Path, intent: str = ""):
        self.judge_llm = judge_llm
        self.run_dir = Path(run_dir)
        self.intent = intent

    def verify(self, step: dict[str, Any], result: dict[str, Any], use_l2: bool) -> tuple[bool, list[dict]]:
        checks: list[dict[str, Any]] = []
        if result.get("error_kind") == "exception":
            checks.append(check("L0", False, result.get("error") or "exception"))
            return False, checks
        if result.get("error_kind") == "timeout":
            checks.append(check("L0", False, result.get("error") or "step timed out"))
            return False, checks
        checks.append(check("L0", True, f"completed in {result.get('duration_ms', 0)} ms"))
        if result.get("error_kind") == "expectation":
            checks.append(check("L1", False, result.get("error") or "expectation failed"))
            return False, checks
        new_modals = (result.get("observation") or {}).get("new_modal_dialogs") or []
        detail = "assertions passed"
        if new_modals:
            detail += f"; warning: new modal dialog(s) appeared: {new_modals}"
        checks.append(check("L1", True, detail))
        if use_l2 and self.judge_llm is not None:
            verdict = judge_step(self.judge_llm, step, result, self.run_dir, self.intent)
            if verdict["passed"] is None:
                checks.append(check("L2", True, f"skipped ({verdict['reason']})"))
            else:
                checks.append(check("L2", bool(verdict["passed"]), verdict["reason"]))
                if not verdict["passed"]:
                    return False, checks
        return True, checks


def format_step_result(result: dict[str, Any], checks: list[dict[str, Any]], passed: bool) -> str:
    lines = [f"Step {result.get('step_id')!r}: {'PASSED' if passed else 'FAILED'} "
             f"(attempt {result.get('attempt')}, {result.get('duration_ms')} ms)"]
    for c in checks:
        lines.append(f"  [{c['level']}] {'ok' if c['passed'] else 'FAIL'}: {c['detail']}")
    if result.get("traceback") and not passed:
        lines.append("Traceback (tail):\n" + result["traceback"][-2500:])
    if result.get("stdout"):
        lines.append("Step stdout (tail):\n" + result["stdout"][-2000:])
    obs = result.get("observation") or {}
    if obs.get("url"):
        lines.append(f"Browser: {obs['url']} | {obs.get('title')}")
    if obs.get("foreground"):
        lines.append(f"Foreground window: {obs['foreground'].get('title')!r}")
    if obs.get("modal_dialogs"):
        lines.append(f"Modal dialogs: {obs['modal_dialogs']}")
    return "\n".join(lines)


def shot_path(run_dir: Path, rel: str | None) -> list[str]:
    if not rel:
        return []
    p = Path(rel) if Path(rel).is_absolute() else Path(run_dir) / rel
    return [str(p)] if p.is_file() else []


class RunnerGuard:
    """Wraps runner calls; restarts a dead / hung runner from the last checkpoint."""

    def __init__(self, runner: RunnerClient, run_dir: Path):
        self.runner = runner
        self.run_dir = Path(run_dir)

    def call(self, fn: Callable[[], Any]) -> tuple[Any, str | None]:
        try:
            return fn(), None
        except (RunnerTimeout, RunnerDied) as exc:
            state, _ = latest_checkpoint_state(self.run_dir)
            try:
                self.runner.restart(state=state)
                note = f"{exc}. The runner was restarted (browser/desktop state kept; c.state restored from the " \
                       f"last checkpoint)."
            except RunnerError as exc2:
                note = f"{exc}; restart failed: {exc2}"
            return None, note


def common_tools(runner: RunnerClient, run_dir: Path, shell: ShellBackend,
                 ask_user: Callable[[str], str] | None = None) -> list[Tool]:
    guard = RunnerGuard(runner, run_dir)

    def observe(args: dict[str, Any]) -> ToolResult:
        obs, err = guard.call(lambda: runner.observe(detail=True, tag="observe"))
        if err:
            return ToolResult(f"observe failed: {err}")
        lines = []
        if obs.get("url"):
            lines.append(f"Browser URL: {obs['url']}\nTitle: {obs.get('title')}")
        if obs.get("foreground"):
            lines.append(f"Foreground window: {json.dumps(obs['foreground'], ensure_ascii=False)}")
        if obs.get("modal_dialogs"):
            lines.append(f"Modal dialogs: {obs['modal_dialogs']}")
        if obs.get("aria"):
            lines.append(f"ARIA snapshot:\n{obs['aria']}")
        if obs.get("ui_tree"):
            lines.append(f"UIA tree (foreground window):\n{obs['ui_tree']}")
        for k in ("screenshot_error", "browser_error", "desktop_error"):
            if obs.get(k):
                lines.append(f"{k}: {obs[k]}")
        return ToolResult("\n".join(lines) or "(no observation)", images=shot_path(run_dir, obs.get("screenshot")))

    def python(args: dict[str, Any]) -> ToolResult:
        timeout = float(args.get("timeout") or 120)
        res, err = guard.call(lambda: runner.exec(args["code"], timeout=timeout))
        if err:
            return ToolResult(f"python failed: {err}")
        out = res.get("stdout") or ""
        if res.get("ok"):
            return ToolResult(out or "(no output; use print() to see values)")
        return ToolResult(f"{out}\nERROR: {res.get('error')}\n{res.get('traceback', '')}".strip())

    def run_command(args: dict[str, Any]) -> ToolResult:
        res = shell.run(args["command"], timeout=float(args.get("timeout") or 120), cwd=str(run_dir))
        return ToolResult(res.text())

    def view_image(args: dict[str, Any]) -> ToolResult:
        p = Path(args["path"])
        if not p.is_file():
            return ToolResult(f"no such file: {p}")
        return ToolResult(f"Image: {p}", images=[str(p)])

    tools = [
        Tool(tool_spec("observe", "Screenshot + ARIA snapshot (browser) or UIA tree (desktop) of the live state.", {}),
             observe),
        Tool(tool_spec("python", "Execute a Python snippet in the live flow runner (`c`, `expect` in scope). "
                                 "Use print() to output. Not recorded into the flow.",
                       {"code": {"type": "string"}, "timeout": {"type": "integer", "description": "seconds"}},
                       ["code"]), python),
        Tool(tool_spec("run_command", "Run a shell command on the host and return exit code/stdout/stderr.",
                       {"command": {"type": "string"}, "timeout": {"type": "integer"}}, ["command"]), run_command),
        Tool(tool_spec("view_image", "View an image file from disk.", {"path": {"type": "string"}}), view_image),
    ]
    if ask_user is not None:
        tools.append(Tool(tool_spec("ask_user", "Ask the human one concise question and wait for the answer.",
                                    {"question": {"type": "string"}}),
                          lambda a: ToolResult(f"User answered: {ask_user(a['question'])}")))
    return tools
