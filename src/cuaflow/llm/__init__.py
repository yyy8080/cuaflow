"""LLM 工厂：为指定角色（`planner` / `author` / `supervisor` / `repairer` / `fallback` / `vision_judge` / `matcher`）构建客户端。"""

from __future__ import annotations

from typing import Any, Callable

from cuaflow.config import ModelSpec, Settings
from cuaflow.llm.base import (
    LLM,
    FakeLLM,
    LLMError,
    LLMResponse,
    ToolCall,
    UsageMeter,
    image_part,
    record_active_llm_usage,
    text_part,
    tool_spec,
    usage_scope,
)
from cuaflow.llm.chat_completions import ChatCompletionsLLM
from cuaflow.llm.responses import ResponsesLLM

ROLES = ("coordinator", "operator", "planner", "author", "supervisor", "repairer", "fallback", "vision_judge", "matcher")

_ROLE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "coordinator": ("planner", "author", "default"),
    "operator": ("author", "default"),
    "planner": ("author", "default"),
    "fallback": ("author", "repairer", "default"),
}

__all__ = [
    "LLM", "FakeLLM", "LLMError", "LLMResponse", "ToolCall", "UsageMeter", "usage_scope",
    "image_part", "text_part", "tool_spec",
    "ChatCompletionsLLM", "ResponsesLLM", "build_llm", "LLMProvider", "ROLES",
]


def build_llm(spec: ModelSpec) -> LLM:
    kwargs = dict(model=spec.model, base_url=spec.base_url, api_key=spec.api_key or None,
                  api_key_env=spec.api_key_env, vision=spec.vision,
                  max_output_tokens=spec.max_output_tokens, request_timeout=spec.request_timeout)
    if spec.api == "responses":
        return ResponsesLLM(**kwargs)
    return ChatCompletionsLLM(tool_mode=spec.tool_mode, temperature=spec.temperature, **kwargs)


class _MeteredLLM:
    """轻量封装，将各角色的 LLM 用量记录到当前激活的 :func:`usage_scope` 中。"""

    def __init__(self, inner: LLM, role: str):
        self._inner = inner
        self._role = role

    @property
    def vision(self) -> bool:
        return bool(getattr(self._inner, "vision", True))

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             json_mode: bool = False, max_tokens: int | None = None) -> LLMResponse:
        resp = self._inner.chat(messages, tools=tools, json_mode=json_mode, max_tokens=max_tokens)
        record_active_llm_usage(self._role, messages, resp)
        return resp

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class LLMProvider:
    """将角色名解析为带用量统计的 LLM。测试可注入 ``override``（角色 -> LLM 字典，或所有角色共用的单个 LLM）。"""

    def __init__(self, settings_getter: Callable[[], Settings], override: dict[str, LLM] | LLM | None = None):
        self._settings = settings_getter
        self._override = override

    def has_role(self, role: str) -> bool:
        """判断 ``role`` 是否已显式配置（而非回退）。"""
        if self._override is not None:
            if isinstance(self._override, dict):
                return role in self._override or "default" in self._override
            return True
        return role in self._settings().models.roles or True

    def has_explicit_role(self, role: str) -> bool:
        if self._override is not None:
            return role in self._override if isinstance(self._override, dict) else True
        return role in self._settings().models.roles

    def get(self, role: str) -> LLM:
        inner = self._resolve(role)
        return _MeteredLLM(inner, role)

    def _resolve(self, role: str) -> LLM:
        if self._override is not None:
            if isinstance(self._override, dict):
                if role in self._override:
                    return self._override[role]
                for fb in _ROLE_FALLBACKS.get(role, ()):
                    if fb in self._override:
                        return self._override[fb]
                if "default" in self._override:
                    return self._override["default"]
            else:
                return self._override
        models = self._settings().models
        if role in models.roles:
            return build_llm(models.roles[role])
        for fb in _ROLE_FALLBACKS.get(role, ()):
            if fb in models.roles:
                return build_llm(models.roles[fb])
        return build_llm(models.default)
