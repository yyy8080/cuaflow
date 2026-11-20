"""Settings (models per role, supervisor policy, safety) persisted as JSON in the data dir."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from cuaflow.platform.paths import data_dir
from cuaflow.platform.shell import DEFAULT_BLOCKED_PATTERNS


class ModelSpec(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o"
    api_key_env: str = "OPENAI_API_KEY"
    vision: bool = True
    max_output_tokens: int = 4096
    api: Literal["chat", "responses"] = "chat"
    tool_mode: Literal["native", "json"] = "native"
    temperature: float | None = None
    request_timeout: float = 180


class ModelsConfig(BaseModel):
    default: ModelSpec = Field(default_factory=ModelSpec)
    roles: dict[str, ModelSpec] = Field(default_factory=dict)

    def for_role(self, role: str) -> ModelSpec:
        return self.roles.get(role) or self.default


class SupervisorConfig(BaseModel):
    l2_policy: Literal["always", "on_change", "sample", "off"] = "on_change"
    sample_rate: float = 0.2
    l3_enabled: bool = True
    max_repair_attempts: int = 3
    repair_turn_limit: int = 30
    auto_rollback: bool = False
    rollback_after_failures: int = 3
    step_timeout_default: float = 120


class SafetyConfig(BaseModel):
    blocked_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKED_PATTERNS))
    confirm_side_effects: bool = False


class AuthorConfig(BaseModel):
    turn_limit: int = 80
    verify_replay: bool = True


class Settings(BaseModel):
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    author: AuthorConfig = Field(default_factory=AuthorConfig)


_lock = threading.Lock()


def settings_path() -> Path:
    return Path(os.environ.get("CUAFLOW_SETTINGS", data_dir() / "settings.json"))


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    with _lock:
        if path.is_file():
            return Settings.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> Settings:
    path = path or settings_path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
    return settings
