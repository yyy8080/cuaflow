"""将新任务与流程库进行匹配：直接运行已有流程、适配改造流程、启动多阶段长程任务，或录制新流程。"""

from __future__ import annotations

import json
import re
from typing import Any

from cuaflow.llm.base import LLM, LLMError, parse_json_loose

MATCH_SYSTEM = """You route automation tasks to a library of recorded, parameterized flows.
Given the new task and the catalog, answer with ONE JSON object:
{"verdict": "run" | "adapt" | "mission" | "new", "flow_id": "<id or null>", "params": {<param>: <value>}, "reason": "<short>"}
- "run": the task is a single execution of an existing flow, only parameter values differ, and EVERY required parameter
  of that flow can be filled from the task (convert dates/numbers to the format implied by description/example).
- "adapt": same application/site and a single linear procedure, but steps or parameters need changes.
- "mission": the task is long-horizon, multi-stage, chains multiple flows, or iterates over multiple items/pages/queries.
- "new": a single linear procedure with nothing relevant in the catalog.
Prefer "adapt" or "mission" over a wrong "run". Never invent parameter values that are not stated or clearly implied."""

_LONG_HORIZON_PATTERNS = re.compile(
    r"(逐个|逐条|依次|批量|遍历|翻页|每一页|所有页|全部页|每个|每一项|前\s*\d+\s*(个|条|页|项|篇|家)|"
    r"\bfor\s+each\b|\beach\s+of\b|\ball\s+\d+\b|\bevery\s+page\b|\bpaginate\b|\bpagination\b|\bbatch\b)",
    re.IGNORECASE,
)


def looks_long_horizon(instruction: str) -> bool:
    """快速启发式检测批量、循环、翻页或多阶段指令。"""
    text = (instruction or "").strip()
    if len(text) >= 260:
        return True
    return bool(_LONG_HORIZON_PATTERNS.search(text))


def _catalog(flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"flow_id": f["id"], "name": f["name"], "intent_template": f.get("intent_template"),
             "description": (f.get("description") or "")[:300], "params": f.get("params_schema"),
             "start_url": f.get("start_url"), "side_effects": f.get("side_effects")} for f in flows]


def _missing_params(flow: dict[str, Any], params: dict[str, Any]) -> list[str]:
    missing = []
    for name, spec in (flow.get("params_schema") or {}).items():
        if name in params and params[name] not in (None, ""):
            continue
        if "default" in spec or spec.get("required") is False:
            continue
        missing.append(name)
    return missing


def match_task(llm: LLM | None, flows: list[dict[str, Any]], instruction: str, start_url: str | None = None,
               params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(params or {})
    if not flows or llm is None:
        verdict = "mission" if looks_long_horizon(instruction) else "new"
        return {"verdict": verdict, "flow_id": None, "params": params, "reason": "library is empty"}
    user = json.dumps({"task": instruction, "start_url": start_url, "given_params": params,
                       "catalog": _catalog(flows)}, ensure_ascii=False, indent=1)
    try:
        resp = llm.chat([{"role": "system", "content": MATCH_SYSTEM}, {"role": "user", "content": user}],
                        json_mode=True, max_tokens=800)
        obj = parse_json_loose(resp.content)
    except (LLMError, ValueError, json.JSONDecodeError) as exc:
        return {"verdict": "new", "flow_id": None, "params": params, "reason": f"matcher unavailable: {exc}"}
    verdict = obj.get("verdict") if isinstance(obj, dict) else None
    if verdict == "mission":
        return {"verdict": "mission", "flow_id": obj.get("flow_id"), "params": {**(obj.get("params") or {}), **params},
                "reason": obj.get("reason", "long-horizon mission")}
    by_id = {f["id"]: f for f in flows}
    flow_id = obj.get("flow_id") if isinstance(obj, dict) else None
    if verdict not in ("run", "adapt") or flow_id not in by_id:
        return {"verdict": "new", "flow_id": None, "params": params,
                "reason": (obj or {}).get("reason", "no matching flow") if isinstance(obj, dict) else "no match"}
    filled = {**(obj.get("params") or {}), **params}
    if verdict == "run":
        if start_url and not filled.get("start_url") and by_id[flow_id].get("start_url") != start_url:
            filled["start_url"] = start_url
        missing = _missing_params(by_id[flow_id], filled)
        if missing:
            return {"verdict": "adapt", "flow_id": flow_id, "params": filled,
                    "reason": f"cannot fill parameters {missing}; adapting instead"}
    return {"verdict": verdict, "flow_id": flow_id, "params": filled, "reason": obj.get("reason", "")}
