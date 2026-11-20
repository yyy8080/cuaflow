"""Match a new task against the flow library: run an existing flow, adapt one, or record a new one.

Condensed from upstream Skill Factory ``retrieve`` / ``decide`` / ``fill`` into one LLM call
plus deterministic validation.
"""

from __future__ import annotations

import json
from typing import Any

from cuaflow.llm.base import LLM, LLMError, parse_json_loose

MATCH_SYSTEM = """You route automation tasks to a library of recorded, parameterized flows.
Given the new task and the catalog, answer with ONE JSON object:
{"verdict": "run" | "adapt" | "new", "flow_id": "<id or null>", "params": {<param>: <value>}, "reason": "<short>"}
- "run": the task is exactly the procedure of an existing flow, only parameter values differ, and EVERY parameter
  of that flow can be filled from the task (use the flow's param names and types; convert dates/numbers to the
  format implied by the description/example).
- "adapt": same application/site and a similar procedure, but steps or parameters need changes.
- "new": nothing relevant.
Prefer "adapt" over a wrong "run". Never invent parameter values that are not stated or clearly implied."""


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
        return {"verdict": "new", "flow_id": None, "params": params, "reason": "library is empty"}
    user = json.dumps({"task": instruction, "start_url": start_url, "given_params": params,
                       "catalog": _catalog(flows)}, ensure_ascii=False, indent=1)
    try:
        resp = llm.chat([{"role": "system", "content": MATCH_SYSTEM}, {"role": "user", "content": user}],
                        json_mode=True, max_tokens=800)
        obj = parse_json_loose(resp.content)
    except (LLMError, ValueError, json.JSONDecodeError) as exc:
        return {"verdict": "new", "flow_id": None, "params": params, "reason": f"matcher unavailable: {exc}"}
    verdict = obj.get("verdict") if isinstance(obj, dict) else None
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
