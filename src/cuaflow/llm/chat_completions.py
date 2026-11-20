"""兼容 OpenAI Chat Completions 协议的后端（支持 GPT、DeepSeek、Qwen/DashScope、GLM、vLLM、Ollama 等）。

具备完备的跨厂商异常兼容特性：
1. 自动识别上下文超限错误并转化为 `ContextOverflowError`，供 Agent Loop 即时压缩上下文无损重试。
2. 当模型不支持 `image_url` 多模态输入返回 400 错误时，自动降级为纯文本模式并立即重试。
3. 支持 `parse_tool_args_resilient` 宽松 JSON 参数修复，并在 `native` 模式下自动兜底解析模型写在正文 `<tool_call>` 或 JSON 块中的内联工具调用。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

import httpx

from cuaflow.llm.base import (
    ContextOverflowError,
    LLMError,
    LLMResponse,
    RetryPolicy,
    ToolCall,
    is_context_overflow_error,
    parse_json_loose,
    parse_tool_args_resilient,
)

_TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_XML_TOOL_RE = re.compile(r"<tool_call>\s*(\{[\s\S]*?\})\s*</tool_call>", re.IGNORECASE)
_VISION_UNSUPPORTED_HINTS = (
    "image_url",
    "does not support image",
    "vision is not supported",
    "multimodal",
    "unknown variant `image_url`",
    "expected a string",
)

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

    # ------------------------------------------------------------------ 主对话请求
    def chat(self, messages, tools=None, json_mode=False, max_tokens=None) -> LLMResponse:
        try:
            return self._chat_once(messages, tools=tools, json_mode=json_mode, max_tokens=max_tokens)
        except LLMError as exc:
            low = str(exc).lower()
            # 若上游文本模型不支持传入截图（image_url），自动降级为纯文本模态并无感重试一次
            if self.vision and any(h in low for h in _VISION_UNSUPPORTED_HINTS) and not is_context_overflow_error(exc):
                self.vision = False
                return self._chat_once(messages, tools=tools, json_mode=json_mode, max_tokens=max_tokens)
            raise

    def _chat_once(self, messages, tools=None, json_mode=False, max_tokens=None) -> LLMResponse:
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
        if tools and not resp.tool_calls:
            if self.tool_mode == "json":
                resp = self._parse_json_tool(resp)
            else:
                resp = self._recover_inline_tool_calls(resp, tools)
        return resp

    # ------------------------------------------------------------------ 消息规范化与协议兼容
    def _prepare_messages(self, messages, json_tools):
        out = []
        for m in messages:
            m = dict(m)
            if not self.vision and isinstance(m.get("content"), list):
                text_chunks = [
                    p.get("text", "") if p.get("type") != "image_url" else "[image omitted: model has no vision]"
                    for p in m["content"]
                    if isinstance(p, dict)
                ]
                m["content"] = "\n".join(c for c in text_chunks if c)
            if json_tools is not None:
                # 将原生工具调用消息展平为纯文本对话轮次
                if m["role"] == "assistant" and m.get("tool_calls"):
                    tc = m["tool_calls"][0]["function"]
                    parsed_args = parse_tool_args_resilient(tc.get("arguments") or "{}")
                    parsed_args.pop("__invalid_json__", None)
                    m = {
                        "role": "assistant",
                        "content": json.dumps(
                            {"thought": m.get("content", ""), "tool": tc["name"], "args": parsed_args},
                            ensure_ascii=False,
                        ),
                    }
                elif m["role"] == "tool":
                    m = {"role": "user", "content": f"Tool result:\n{m.get('content', '')}"}
            out.append(m)
        if json_tools is not None:
            schema = json.dumps(
                [
                    {
                        "name": t["function"]["name"],
                        "description": t["function"]["description"],
                        "args": t["function"]["parameters"],
                    }
                    for t in json_tools
                ],
                ensure_ascii=False,
                indent=1,
            )
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
                    err_msg = f"HTTP {r.status_code} from {url}: {r.text[:800]}"
                    if is_context_overflow_error(err_msg):
                        raise ContextOverflowError(err_msg)
                    raise LLMError(err_msg)
                return r.json()
            except (httpx.TimeoutException, httpx.TransportError, json.JSONDecodeError) as exc:
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
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            raw = fn.get("arguments")
            args = parse_tool_args_resilient(raw if raw is not None else "{}")
            calls.append(
                ToolCall(
                    id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    name=str(fn.get("name") or "").strip(),
                    arguments=args,
                    raw_arguments=raw if isinstance(raw, str) else json.dumps(args, ensure_ascii=False),
                )
            )
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
            args = parse_tool_args_resilient(obj.get("args") or {})
            return LLMResponse(
                content=str(obj.get("thought", "")),
                tool_calls=[
                    ToolCall(
                        id=f"call_{uuid.uuid4().hex[:8]}",
                        name=str(obj["tool"]).strip(),
                        arguments=args,
                        raw_arguments=json.dumps(args, ensure_ascii=False),
                    )
                ],
                usage=resp.usage,
                raw=resp.raw,
            )
        return resp

    def _recover_inline_tool_calls(self, resp: LLMResponse, tools: list[dict[str, Any]]) -> LLMResponse:
        """兼容部分模型在 native 工具模式下偶发将工具调用直接输出在正文 `<tool_call>` 或 JSON 对象中的情况。"""
        text = (resp.content or "").strip()
        if not text:
            return resp
        known_names = {t.get("function", {}).get("name") for t in tools if isinstance(t, dict)}

        # 1. 识别 <tool_call>{"name": ..., "arguments": ...}</tool_call>
        xml_matches = _XML_TOOL_RE.findall(text)
        if xml_matches:
            recovered: list[ToolCall] = []
            for raw_block in xml_matches:
                parsed = parse_tool_args_resilient(raw_block)
                tname = str(parsed.get("name") or parsed.get("tool") or "").strip()
                if tname in known_names:
                    targs = parse_tool_args_resilient(parsed.get("arguments") if "arguments" in parsed else parsed.get("args", {}))
                    recovered.append(
                        ToolCall(
                            id=f"call_{uuid.uuid4().hex[:8]}",
                            name=tname,
                            arguments=targs,
                            raw_arguments=json.dumps(targs, ensure_ascii=False),
                        )
                    )
            if recovered:
                clean_thought = _XML_TOOL_RE.sub("", text).strip()
                return LLMResponse(content=clean_thought, tool_calls=recovered, usage=resp.usage, raw=resp.raw)

        # 2. 识别正文为单个合法工具 JSON 对象 {"tool": "...", "args": {...}} 或 {"name": "...", "arguments": {...}}
        if "{" in text and "}" in text:
            try:
                obj = parse_json_loose(text)
            except (json.JSONDecodeError, ValueError):
                return resp
            if isinstance(obj, dict):
                tname = str(obj.get("tool") or obj.get("name") or "").strip()
                if tname in known_names and ("args" in obj or "arguments" in obj):
                    targs = parse_tool_args_resilient(obj.get("args") if "args" in obj else obj.get("arguments"))
                    return LLMResponse(
                        content=str(obj.get("thought") or ""),
                        tool_calls=[
                            ToolCall(
                                id=f"call_{uuid.uuid4().hex[:8]}",
                                name=tname,
                                arguments=targs,
                                raw_arguments=json.dumps(targs, ensure_ascii=False),
                            )
                        ],
                        usage=resp.usage,
                        raw=resp.raw,
                    )
        return resp
