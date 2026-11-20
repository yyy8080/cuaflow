"""Flow definition: a reusable, parameterized script made of independently patchable steps.

Example::

    from cuaflow.flow import Flow, Ctx, expect

    flow = Flow("Export monthly report", params={"month": "str"}, output={"rows": "int"})

    @flow.step("Open report page", expect="report page with an Export button")
    def open_report(c: Ctx):
        page = c.browser.page()
        page.goto(c.params.start_url)
        expect.visible(page.get_by_role("button", name="Export"))
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class StepDef:
    id: str
    title: str
    func: Callable[..., Any]
    expect: str = ""
    pre: Callable[..., Any] | None = None
    timeout: float = 120
    retries: int = 0
    side_effect: bool = False
    critical: bool = False
    surface: str = "auto"  # "browser" | "desktop" | "auto" -> which screenshot to take

    def spec(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "expect": self.expect, "side_effect": self.side_effect,
                "critical": self.critical, "timeout": self.timeout, "retries": self.retries}


def _norm_schema(schema: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, spec in (schema or {}).items():
        if isinstance(spec, type):
            spec = {"type": spec.__name__}
        elif isinstance(spec, str):
            spec = {"type": spec}
        else:
            spec = dict(spec)
            if isinstance(spec.get("type"), type):
                spec["type"] = spec["type"].__name__
        spec.setdefault("type", "str")
        out[name] = spec
    return out


class Flow:
    def __init__(
        self,
        name: str,
        params: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        description: str = "",
        intent: str = "",
        side_effects: bool = False,
        start_url: str | None = None,
    ):
        self.name = name
        self.params_schema = _norm_schema(params)
        self.output_schema = _norm_schema(output)
        self.description = description
        self.intent = intent
        self.side_effects = side_effects
        self.start_url = start_url
        self.steps: list[StepDef] = []

    # ------------------------------------------------------------------
    def step(
        self,
        title: str,
        expect: str = "",
        pre: Callable[..., Any] | None = None,
        timeout: float = 120,
        retries: int = 0,
        side_effect: bool = False,
        critical: bool = False,
        surface: str = "auto",
        id: str | None = None,  # noqa: A002
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a step. Re-registering an existing id replaces it in place (hot patching)."""

        def deco(func: Callable[..., Any]) -> Callable[..., Any]:
            sid = id or func.__name__
            sd = StepDef(sid, title, func, expect, pre, timeout, retries, side_effect, critical, surface)
            for i, existing in enumerate(self.steps):
                if existing.id == sid:
                    self.steps[i] = sd
                    break
            else:
                self.steps.append(sd)
            func.__cuaflow_step__ = sid  # type: ignore[attr-defined]
            return func

        return deco

    def get(self, step_id: str) -> StepDef:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(f"unknown step {step_id!r}; known: {[s.id for s in self.steps]}")

    def index(self, step_id: str) -> int:
        return [s.id for s in self.steps].index(step_id)

    def meta(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "intent_template": self.intent,
            "params_schema": self.params_schema,
            "output_schema": self.output_schema,
            "side_effects": bool(self.side_effects or any(s.side_effect for s in self.steps)),
            "start_url": self.start_url,
            "steps": [s.spec() for s in self.steps],
        }

    def coerce_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Apply defaults and basic type coercion according to ``params_schema``."""
        out = dict(params or {})
        casters = {"int": int, "float": float, "str": str,
                   "bool": lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")}
        for name, spec in self.params_schema.items():
            if name not in out or out[name] is None:
                if "default" in spec:
                    out[name] = spec["default"]
                elif spec.get("required", True) and name not in out:
                    raise ValueError(f"missing required parameter {name!r}")
                continue
            cast = casters.get(spec.get("type", "str"))
            if cast is not None:
                try:
                    out[name] = cast(out[name])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"parameter {name!r}: cannot convert {out[name]!r} to {spec['type']}") from exc
        if self.start_url and "start_url" not in out:
            out["start_url"] = self.start_url
        return out


def load_flow(path: str | Path) -> tuple[Flow, dict[str, Any]]:
    """Import a flow file; return the ``Flow`` instance and the module namespace."""
    path = Path(path).resolve()
    mod_name = f"cuaflow_flow_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load flow from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    flows = [v for v in vars(module).values() if isinstance(v, Flow)]
    if len(flows) != 1:
        raise ValueError(f"{path} must define exactly one Flow instance (found {len(flows)})")
    return flows[0], vars(module)


@dataclass
class Params:
    """Attribute + item access to parameters (``c.params.month`` or ``c.params['month']``)."""

    _data: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(f"no parameter {name!r}; available: {sorted(self._data)}") from None

    def __getitem__(self, name: str) -> Any:
        return self._data[name]

    def get(self, name: str, default: Any = None) -> Any:
        return self._data.get(name, default)

    def dict(self) -> dict[str, Any]:
        return dict(self._data)
