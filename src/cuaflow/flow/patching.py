"""流程文件的 AST 级编辑：提取、替换或插入单个步骤函数。"""

from __future__ import annotations

import ast
import difflib
import json
import textwrap
from dataclasses import dataclass


@dataclass
class StepSource:
    step_id: str
    func_name: str
    start: int  # 从 1 开始计数的行号，指向首行装饰器
    end: int  # 从 1 开始计数的结束行号（含）
    text: str


def _is_step_decorator(dec: ast.expr) -> bool:
    return (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "step")


def _step_id(node: ast.FunctionDef) -> str | None:
    for dec in node.decorator_list:
        if _is_step_decorator(dec):
            for kw in dec.keywords:  # type: ignore[union-attr]
                if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
            return node.name
    return None


def extract_steps(source: str) -> list[StepSource]:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sid = _step_id(node)  # type: ignore[arg-type]
            if sid is None:
                continue
            start = min([d.lineno for d in node.decorator_list] + [node.lineno])
            end = node.end_lineno or node.lineno
            out.append(StepSource(sid, node.name, start, end, "".join(lines[start - 1 : end])))
    return out


def get_step_source(source: str, step_id: str) -> str:
    for s in extract_steps(source):
        if s.step_id == step_id:
            return s.text
    raise KeyError(f"step {step_id!r} not found in flow source")


def validate_step_source(step_source: str, expected_id: str | None = None) -> str:
    """校验单个带装饰器的步骤函数源码并返回其步骤 ID；校验失败时抛出 `ValueError`。"""
    step_source = textwrap.dedent(step_source).strip("\n") + "\n"
    try:
        tree = ast.parse(step_source)
    except SyntaxError as exc:
        raise ValueError(f"syntax error in step source: {exc}") from None
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    steps = [f for f in funcs if _step_id(f) is not None]  # type: ignore[arg-type]
    if len(steps) != 1:
        raise ValueError("step source must contain exactly one function decorated with @flow.step(...)")
    others = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Import,
                                                         ast.ImportFrom, ast.Expr))]
    if others:
        raise ValueError("step source may only contain imports, helper functions and the step function")
    sid = _step_id(steps[0])  # type: ignore[arg-type]
    if isinstance(steps[0], ast.AsyncFunctionDef):
        raise ValueError("steps must be synchronous functions (use the Playwright sync API)")
    if expected_id is not None and sid != expected_id:
        raise ValueError(f"step id mismatch: expected {expected_id!r}, got {sid!r} (keep the function name / id)")
    return sid  # type: ignore[return-value]


def replace_step(source: str, step_id: str, step_source: str) -> str:
    """将步骤 ``step_id`` 替换为 ``step_source``；若该步骤尚不存在则追加。"""
    step_source = textwrap.dedent(step_source).strip("\n") + "\n"
    lines = source.splitlines(keepends=True)
    for s in extract_steps(source):
        if s.step_id == step_id:
            new = lines[: s.start - 1] + [step_source] + lines[s.end :]
            return "".join(new)
    return insert_step(source, step_source)


def insert_step(source: str, step_source: str, after: str | None = None) -> str:
    step_source = textwrap.dedent(step_source).strip("\n") + "\n"
    lines = source.splitlines(keepends=True)
    steps = extract_steps(source)
    if after is not None:
        for s in steps:
            if s.step_id == after:
                return "".join(lines[: s.end] + ["\n\n", step_source] + lines[s.end :])
    # 若末尾存在 `if __name__ == "__main__":` 代码块，则插入到该代码块之前
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.If) and "__name__" in ast.unparse(node.test):
            idx = node.lineno - 1
            return "".join(lines[:idx]).rstrip("\n") + "\n\n\n" + step_source + "\n\n" + "".join(lines[idx:])
    return source.rstrip("\n") + "\n\n\n" + step_source


def remove_step(source: str, step_id: str) -> str:
    lines = source.splitlines(keepends=True)
    for s in extract_steps(source):
        if s.step_id == step_id:
            return "".join(lines[: s.start - 1] + lines[s.end :])
    return source


def reorder_steps(source: str, order: list[str]) -> str:
    """重写源码使各步骤函数按 ``order`` 顺序排列（步骤按定义顺序注册）。"""
    steps = {s.step_id: s for s in extract_steps(source)}
    for sid in order:
        if sid not in steps:
            raise KeyError(sid)
    lines = source.splitlines(keepends=True)
    first = min(s.start for s in steps.values())
    body_wo = source
    for sid in sorted(steps, key=lambda k: -steps[k].start):
        body_wo = remove_step(body_wo, sid)
    head_lines = body_wo.splitlines(keepends=True)
    head = "".join(lines[: first - 1]).rstrip("\n") + "\n\n\n"
    tail = "".join(head_lines[first - 1 :]).strip("\n")
    text = head + "\n\n".join(steps[s].text.strip("\n") + "\n" for s in order)
    return text + ("\n\n" + tail + "\n" if tail else "")


def unified_diff(old: str, new: str, name: str = "flow.py") -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"a/{name}", tofile=f"b/{name}"))


def render_flow_header(
    name: str,
    description: str = "",
    intent: str = "",
    params: dict | None = None,
    output: dict | None = None,
    side_effects: bool = False,
    start_url: str | None = None,
) -> str:
    """生成新流程文件的模块头部（包含导入语句与 `Flow(...)` 声明）。"""

    def lit(value) -> str:
        return json.dumps(value, ensure_ascii=False, indent=4).replace("true", "True").replace(
            "false", "False").replace("null", "None")

    return (
        f'"""{name}\n\n{description}\n\n由 cuaflow Author 智能体自动生成。"""\n\n'
        "from cuaflow.flow import Ctx, Flow, expect\n\n"
        "flow = Flow(\n"
        f"    name={name!r},\n"
        f"    description={description!r},\n"
        f"    intent={intent!r},\n"
        f"    params={textwrap.indent(lit(params or {}), '    ').lstrip()},\n"
        f"    output={textwrap.indent(lit(output or {}), '    ').lstrip()},\n"
        f"    side_effects={side_effects!r},\n"
        f"    start_url={start_url!r},\n"
        ")\n"
    )
