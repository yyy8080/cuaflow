"""兼容 OpenAI Chat Completions 协议的后端（支持 GPT、DeepSeek、Qwen/DashScope、vLLM、Ollama 等）。"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx

from cuaflow.llm.base import LLMError, LLMResponse, RetryPolicy, ToolCall, parse_json_loose

_TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504, 529}

JSON_TOOL_PROMPT = """

# Tool protocol
You cannot call tools natively. Instead reply with EXACTLY ONE JSON object (no prose, no code fence):
{{"thought": "<short reasoning>", "tool": "<tool name>", "args": {{...}}}}
Available tools (JSON schema of args):
{tools}
"""


class ChatCompletionsLLM:
    def __init__(
        self,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        vision: bool = True,
        max_output_tokens: int = 4096,
        tool_mode: str = "native",
        temperature: float | None = None,
        request_timeout: float = 180,
        retry: RetryPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key if api_key is not None and api_key != "" else os.environ.get(api_key_env, "")
        self.api_key_env = api_key_env
        self.vision = vision
        self.max_output_tokens = max_output_tokens
        self.tool_mode = tool_mode
        self.temperature = temperature
        self.retry = retry or RetryPolicy()
        self._client = httpx.Client(timeout=request_timeout, transport=transport)
        self.usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    # ------------------------------------------------------------------
    def chat(self, messages, tools=None, json_mode=False, max_tokens=None) -> LLMResponse:
        native_tools = tools if (tools and self.tool_mode == "native") else None
        msgs = self._prepare_messages(messages, tools if (tools and self.tool_mode == "json") else None)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": max_tokens or self.max_output_tokens,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if native_tools:
            payload["tools"] = native_tools
            payload["tool_choice"] = "auto"
        if json_mode or (tools and self.tool_mode == "json"):
            payload["response_format"] = {"type": "json_object"}
        data = self._post(payload)
        resp = self._parse(data)
        if tools and self.tool_mode == "json" and not resp.tool_calls:
            resp = self._parse_json_tool(resp)
        return resp

    # ------------------------------------------------------------------
    def _prepare_messages(self, messages, json_tools):
        out = []
        for m in messages:
            m = dict(m)
            if not self.vision and isinstance(m.get("content"), list):
                m["content"] = [
                    p if p.get("type") != "image_url" else {"type": "text", "text": "[image omitted: model has no vision]"}
                    for p in m["content"]
                ]
            if json_tools is not None:
                # 将原生工具调用消息展平为纯文本对话轮次
                if m["role"] == "assistant" and m.get("tool_calls"):
                    tc = m["tool_calls"][0]["function"]
                    m = {"role": "assistant", "content": json.dumps(
                        {"thought": m.get("content", ""), "tool": tc["name"], "args": json.loads(tc["arguments"] or "{}")},
                        ensure_ascii=False)}
                elif m["role"] == "tool":
                    m = {"role": "user", "content": f"Tool result:\n{m.get('content', '')}"}
            out.append(m)
        if json_tools is not None:
            schema = json.dumps(
                [{"name": t["function"]["name"], "description": t["function"]["description"],
                  "args": t["function"]["parameters"]} for t in json_tools],
                ensure_ascii=False, indent=1)
            if out and out[0]["role"] == "system":
                out[0] = {**out[0], "content": str(out[0]["content"]) + JSON_TOOL_PROMPT.format(tools=schema)}
            else:
                out.insert(0, {"role": "system", "content": JSON_TOOL_PROMPT.format(tools=schema)})
        return out

    def _post(self, payload):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = f"{self.base_url}/chat/completions"
        last: Exception | None = None
        for attempt in range(self.retry.attempts):
            try:
                r = self._client.post(url, headers=headers, json=payload)
                if r.status_code in _TRANSIENT:
                    last = LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                    self.retry.sleep(attempt)
                    continue
                if r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code} from {url}: {r.text[:800]}")
                return r.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                self.retry.sleep(attempt)
        raise LLMError(f"LLM request failed after {self.retry.attempts} attempts: {last}")

    def _parse(self, data) -> LLMResponse:
        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"malformed completion: {str(data)[:500]}") from exc
        calls = []
        for tc in choice.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                args = {"__invalid_json__": raw}
            calls.append(ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}", name=fn.get("name", ""),
                                  arguments=args, raw_arguments=raw if isinstance(raw, str) else json.dumps(raw)))
        usage = data.get("usage") or {}
        self.usage_total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.usage_total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        self.usage_total["calls"] += 1
        content = choice.get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return LLMResponse(content=content, tool_calls=calls, usage=usage, raw=data)

    def _parse_json_tool(self, resp: LLMResponse) -> LLMResponse:
        try:
            obj = parse_json_loose(resp.content)
        except (json.JSONDecodeError, ValueError):
            return resp
        if isinstance(obj, dict) and obj.get("tool"):
            args = obj.get("args") or {}
            return LLMResponse(
                content=str(obj.get("thought", "")),
                tool_calls=[ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=obj["tool"], arguments=args,
                                     raw_arguments=json.dumps(args, ensure_ascii=False))],
                usage=resp.usage, raw=resp.raw)
        return resp
