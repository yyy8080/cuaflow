"""OpenAI Responses API 后端（保留自上游 CUAWright，用于 GPT 推理模型）。

将内部 Chat Completions 风格的消息转换为 Responses 接口所需的 ``input`` 项。
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx

from cuaflow.llm.base import LLMError, LLMResponse, RetryPolicy, ToolCall

_TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504}


def _to_input(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    instructions, items = "", []
    for m in messages:
        role, content = m["role"], m.get("content", "")
        if role == "system":
            instructions += (content if isinstance(content, str) else "".join(
                p.get("text", "") for p in content)) + "\n"
            continue
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": str(content)})
            continue
        if role == "assistant":
            if content:
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": str(content)}]})
            for tc in m.get("tool_calls") or []:
                items.append({"type": "function_call", "call_id": tc["id"], "name": tc["function"]["name"],
                              "arguments": tc["function"]["arguments"]})
            continue
        parts = []
        for p in content if isinstance(content, list) else [{"type": "text", "text": content}]:
            if p.get("type") == "image_url":
                parts.append({"type": "input_image", "image_url": p["image_url"]["url"],
                              "detail": p["image_url"].get("detail", "auto")})
            else:
                parts.append({"type": "input_text", "text": p.get("text", "")})
        items.append({"role": "user", "content": parts})
    return instructions.strip(), items


class ResponsesLLM:
    def __init__(self, model: str, base_url: str = "https://api.openai.com/v1", api_key: str | None = None,
                 api_key_env: str = "OPENAI_API_KEY", vision: bool = True, max_output_tokens: int = 8192,
                 reasoning_effort: str | None = "medium", request_timeout: float = 300,
                 retry: RetryPolicy | None = None, transport: httpx.BaseTransport | None = None, **_: Any):
        self.model, self.base_url = model, base_url.rstrip("/")
        self.api_key = api_key if api_key is not None and api_key != "" else os.environ.get(api_key_env, "")
        self.vision, self.max_output_tokens, self.reasoning_effort = vision, max_output_tokens, reasoning_effort
        self.retry = retry or RetryPolicy()
        self._client = httpx.Client(timeout=request_timeout, transport=transport)

    def chat(self, messages, tools=None, json_mode=False, max_tokens=None) -> LLMResponse:  # noqa: ARG002
        instructions, items = _to_input(messages)
        payload: dict[str, Any] = {"model": self.model, "input": items, "store": False,
                                   "max_output_tokens": max_tokens or self.max_output_tokens}
        if instructions:
            payload["instructions"] = instructions
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        if tools:
            payload["tools"] = [{"type": "function", "name": t["function"]["name"],
                                 "description": t["function"]["description"],
                                 "parameters": t["function"]["parameters"]} for t in tools]
            payload["parallel_tool_calls"] = False
        data = self._post(payload)
        text, calls = [], []
        for item in data.get("output", []):
            if item.get("type") == "message":
                text += [c.get("text", "") for c in item.get("content", []) if c.get("type") == "output_text"]
            elif item.get("type") == "function_call":
                raw = item.get("arguments") or "{}"
                try:
                    args = json.loads(raw)
                except json.JSONDecodeError:
                    args = {"__invalid_json__": raw}
                calls.append(ToolCall(id=item.get("call_id") or uuid.uuid4().hex, name=item["name"],
                                      arguments=args, raw_arguments=raw))
        return LLMResponse(content="\n".join(text), tool_calls=calls, usage=data.get("usage") or {}, raw=data)

    def _post(self, payload):
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last: Exception | None = None
        for attempt in range(self.retry.attempts):
            try:
                r = self._client.post(f"{self.base_url}/responses", headers=headers, json=payload)
                if r.status_code in _TRANSIENT:
                    last = LLMError(f"HTTP {r.status_code}")
                    self.retry.sleep(attempt)
                    continue
                if r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:800]}")
                return r.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                self.retry.sleep(attempt)
        raise LLMError(f"Responses request failed: {last}")
