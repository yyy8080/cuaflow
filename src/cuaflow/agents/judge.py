"""LLM judges (Supervisor L2 per-step visual check and L3 task-level verdict).

Adapted from upstream ``self_reflection.py``: instead of a two-stage per-image score + final
verdict over a whole run, each step is judged against its own natural-language expectation,
which localizes failures to a single step.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cuaflow.llm.base import LLM, LLMError, image_part, parse_json_loose, text_part

STEP_JUDGE_SYSTEM = """You are the Supervisor of an automation script running on a Windows desktop / web browser.
You verify ONE step at a time. You receive the step title, its expected outcome, what the step printed,
and a screenshot taken right after the step finished.

Decide whether the screenshot (and printed output) shows the expected outcome was achieved.
- Judge only this step's expectation, not the whole task.
- Unexpected dialogs/popups/error banners/login walls covering the UI => failed.
- If the evidence is ambiguous but nothing contradicts the expectation, pass.
Reply with ONE JSON object: {"passed": true|false, "reason": "<one or two sentences, same language as the step>"}"""

TASK_JUDGE_SYSTEM = """You are the Supervisor of an automation script. The run has finished. Given the task intent,
the per-step log and the final screenshot(s), decide whether the TASK as a whole was accomplished and the
reported output is plausible. Reply with ONE JSON object: {"passed": true|false, "reason": "<short reason>"}"""


def _verdict(llm: LLM, messages: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        resp = llm.chat(messages, json_mode=True, max_tokens=600)
        obj = parse_json_loose(resp.content)
        if not isinstance(obj, dict) or "passed" not in obj:
            raise ValueError("missing 'passed'")
        return {"passed": bool(obj["passed"]), "reason": str(obj.get("reason", "")).strip()}
    except (LLMError, ValueError, json.JSONDecodeError) as exc:
        return {"passed": None, "reason": f"judge unavailable: {exc}"}


def judge_step(llm: LLM, step: dict[str, Any], result: dict[str, Any], run_dir: Path,
               intent: str = "") -> dict[str, Any]:
    """Return {"passed": bool|None, "reason": str}. ``None`` means the judge could not decide."""
    obs = result.get("observation") or {}
    ctx_lines = [
        f"Overall task: {intent}" if intent else "",
        f"Step: {step.get('title')} (id={step.get('id') or step.get('step_id')})",
        f"Expected outcome: {step.get('expect') or '(none given — infer from the title)'}",
        f"Step printed:\n{(result.get('stdout') or '').strip()[-2000:] or '(nothing)'}",
    ]
    if obs.get("url"):
        ctx_lines.append(f"Browser URL: {obs.get('url')}  title: {obs.get('title')}")
    if obs.get("foreground"):
        ctx_lines.append(f"Foreground window: {obs['foreground'].get('title')!r}")
    if obs.get("modal_dialogs"):
        ctx_lines.append(f"Modal dialogs present: {obs['modal_dialogs']}")
    parts: list[dict[str, Any]] = [text_part("\n".join(x for x in ctx_lines if x))]
    shot = result.get("screenshot")
    if shot and llm.vision:
        p = Path(shot) if Path(shot).is_absolute() else run_dir / shot
        if p.is_file():
            parts.append(image_part(p))
    return _verdict(llm, [{"role": "system", "content": STEP_JUDGE_SYSTEM}, {"role": "user", "content": parts}])


def judge_task(llm: LLM, intent: str, step_records: list[dict[str, Any]], output: dict[str, Any],
               run_dir: Path, max_images: int = 3) -> dict[str, Any]:
    log = "\n".join(
        f"{i + 1}. [{s.get('status')}] {s.get('title')} — expect: {s.get('expect') or '-'}"
        + (f" — error: {s.get('error')}" if s.get("error") else "")
        for i, s in enumerate(step_records))
    parts: list[dict[str, Any]] = [text_part(
        f"Task intent: {intent}\n\nStep log:\n{log}\n\nReported output:\n"
        f"{json.dumps(output, ensure_ascii=False)[:3000]}")]
    if llm.vision:
        shots = [s.get("screenshot") for s in step_records if s.get("screenshot")][-max_images:]
        for shot in shots:
            p = run_dir / shot
            if p.is_file():
                parts.append(image_part(p, max_side=1280))
    return _verdict(llm, [{"role": "system", "content": TASK_JUDGE_SYSTEM}, {"role": "user", "content": parts}])
