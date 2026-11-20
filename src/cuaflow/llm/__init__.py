"""LLM factory: build a client for a role (author / supervisor / repairer / vision_judge / matcher)."""

from __future__ import annotations

from typing import Callable

from cuaflow.config import ModelSpec, Settings
from cuaflow.llm.base import LLM, FakeLLM, LLMError, LLMResponse, ToolCall, image_part, text_part, tool_spec
from cuaflow.llm.chat_completions import ChatCompletionsLLM
from cuaflow.llm.responses import ResponsesLLM

ROLES = ("author", "supervisor", "repairer", "vision_judge", "matcher")

__all__ = [
    "LLM", "FakeLLM", "LLMError", "LLMResponse", "ToolCall", "image_part", "text_part", "tool_spec",
    "ChatCompletionsLLM", "ResponsesLLM", "build_llm", "LLMProvider", "ROLES",
]


def build_llm(spec: ModelSpec) -> LLM:
    kwargs = dict(model=spec.model, base_url=spec.base_url, api_key_env=spec.api_key_env, vision=spec.vision,
                  max_output_tokens=spec.max_output_tokens, request_timeout=spec.request_timeout)
    if spec.api == "responses":
        return ResponsesLLM(**kwargs)
    return ChatCompletionsLLM(tool_mode=spec.tool_mode, temperature=spec.temperature, **kwargs)


class LLMProvider:
    """Resolves role -> LLM. Tests inject ``override`` (role -> LLM or a single LLM for all roles)."""

    def __init__(self, settings_getter: Callable[[], Settings], override: dict[str, LLM] | LLM | None = None):
        self._settings = settings_getter
        self._override = override

    def get(self, role: str) -> LLM:
        if self._override is not None:
            if isinstance(self._override, dict):
                if role in self._override:
                    return self._override[role]
                if "default" in self._override:
                    return self._override["default"]
            else:
                return self._override
        return build_llm(self._settings().models.for_role(role))
