"""全局配置管理（各角色模型、监督器策略、安全策略等），以 JSON 格式持久化保存在数据目录中。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from cuaflow.platform.paths import data_dir
from cuaflow.platform.shell import DEFAULT_BLOCKED_PATTERNS


MASK_PREFIX = "••••••••"


class ModelSpec(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o"
    api_key: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    vision: bool = True
    max_output_tokens: int = 4096
    api: Literal["chat", "responses"] = "chat"
    tool_mode: Literal["native", "json"] = "native"
    temperature: float | None = None
    request_timeout: float = 180

    def resolved_api_key(self) -> str:
        return self.api_key.strip() or os.environ.get(self.api_key_env, "")


class ModelsConfig(BaseModel):
    default: ModelSpec = Field(default_factory=ModelSpec)
    roles: dict[str, ModelSpec] = Field(default_factory=dict)

    def for_role(self, role: str) -> ModelSpec:
        return self.roles.get(role) or self.default


class SupervisorConfig(BaseModel):
    l15_sentinel: bool = True
    l2_policy: Literal["always", "on_change", "sample", "off"] = "on_change"
    adaptive_l2: bool = True
    audit_every_n_runs: int = 10
    sample_rate: float = 0.2
    l3_enabled: bool = True
    tier0_heal: bool = True
    max_repair_attempts: int = 3
    repair_turn_limit: int = 30
    autonomous_fallback: bool = True
    fallback_turn_limit: int = 25
    unattended: bool = False
    auto_rollback: bool = False
    rollback_after_failures: int = 3
    step_timeout_default: float = 120


class SafetyConfig(BaseModel):
    blocked_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKED_PATTERNS))
    confirm_side_effects: bool = False


class AuthorConfig(BaseModel):
    turn_limit: int = 80
    verify_replay: bool = False


class PlannerConfig(BaseModel):
    enabled: bool = True
    turn_limit: int = 60
    sub_author_turn_limit: int = 50
    max_batch_items: int = 200
    tab_limit: int = 6
    tokens_saved_per_step: int = 1800
    auto_resume: bool = False


class CoordinatorConfig(BaseModel):
    max_parallel_workers: int = 4
    turn_limit: int = 24
    operator_turn_limit: int = 120
    max_auto_continues: int = 2
    auto_report: bool = True


class Settings(BaseModel):
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    author: AuthorConfig = Field(default_factory=AuthorConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    coordinator: CoordinatorConfig = Field(default_factory=CoordinatorConfig)


_lock = threading.Lock()


def settings_path() -> Path:
    return Path(os.environ.get("CUAFLOW_SETTINGS", data_dir() / "settings.json"))


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    with _lock:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            # 平滑迁移 v0.2.0 落盘的旧版过低默认步数（35 轮 / 12 轮），避免升级后仍沿用旧上限导致半途中断
            coord = raw.get("coordinator")
            if isinstance(coord, dict) and "max_auto_continues" not in coord:
                if coord.get("operator_turn_limit") == 35:
                    coord["operator_turn_limit"] = 120
                if coord.get("turn_limit") == 12:
                    coord["turn_limit"] = 24
            return Settings.model_validate(raw)
    return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> Settings:
    path = path or settings_path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
    return settings


def _mask_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        return ""
    return f"{MASK_PREFIX}{k[-4:]}" if len(k) > 6 else MASK_PREFIX


def masked_settings_dict(s: Settings) -> dict[str, Any]:
    d = s.model_dump()
    models = d.get("models") or {}
    if isinstance(models.get("default"), dict):
        models["default"]["api_key"] = _mask_key(models["default"].get("api_key", ""))
    for rspec in (models.get("roles") or {}).values():
        if isinstance(rspec, dict):
            rspec["api_key"] = _mask_key(rspec.get("api_key", ""))
    return d


def restore_masked_keys(incoming: dict[str, Any], current: Settings) -> dict[str, Any]:
    out = dict(incoming)
    in_models = out.get("models")
    if not isinstance(in_models, dict):
        return out
    in_models = dict(in_models)
    out["models"] = in_models
    if isinstance(in_models.get("default"), dict):
        def_d = dict(in_models["default"])
        if str(def_d.get("api_key") or "").startswith(MASK_PREFIX):
            def_d["api_key"] = current.models.default.api_key
        in_models["default"] = def_d
    if isinstance(in_models.get("roles"), dict):
        roles_d: dict[str, Any] = {}
        for role, rspec in in_models["roles"].items():
            if isinstance(rspec, dict):
                rcopy = dict(rspec)
                if str(rcopy.get("api_key") or "").startswith(MASK_PREFIX):
                    prev = current.models.roles.get(role)
                    rcopy["api_key"] = prev.api_key if prev else ""
                roles_d[role] = rcopy
            else:
                roles_d[role] = rspec
        in_models["roles"] = roles_d
    return out
