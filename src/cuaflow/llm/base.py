"""LLM 客户端层。

内部所有智能体统一使用 OpenAI *Chat Completions* 消息格式::

    {"role": "system"|"user"|"assistant"|"tool", "content": str | [parts], ...}

其中图像片段格式为 ``{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}``，
助手工具调用使用 ``tool_calls``。各后端按需转换消息格式。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def assistant_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": tc.raw_arguments or json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in self.tool_calls
            ]
        return msg


class LLM(Protocol):
    vision: bool

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def image_part(path: str | Path, detail: str = "auto", max_side: int = 1600) -> dict[str, Any]:
    """将图像文件编码为 data-URL 片段（最大边长按需缩放至 ``max_side``）。"""
    path = Path(path)
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            if max(img.size) > max_side:
                img.thumbnail((max_side, max_side))
                buf = BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=85)
                data, mime = buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - 回退为原始字节数据
        pass
    url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    return {"type": "image_url", "image_url": {"url": url, "detail": detail}}


def tool_spec(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required if required is not None else list(properties),
            },
        },
    }


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_json_loose(text: str) -> Any:
    """从模型输出文本中宽松解析 JSON（容忍代码块围栏及前后说明文字）。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(text)
        if match:
            return json.loads(match.group(0))
        raise


def strip_images(messages: list[dict[str, Any]], keep_last: int = 2) -> list[dict[str, Any]]:
    """除最后 ``keep_last`` 张图像外，其余图像均替换为文本占位符（控制上下文长度）。"""
    positions = []
    for i, m in enumerate(messages):
        if isinstance(m.get("content"), list):
            for j, p in enumerate(m["content"]):
                if isinstance(p, dict) and p.get("type") == "image_url":
                    positions.append((i, j))
    drop = set(positions[:-keep_last] if keep_last > 0 else positions)
    if not drop:
        return messages
    out = []
    for i, m in enumerate(messages):
        if isinstance(m.get("content"), list) and any((i, j) in drop for j in range(len(m["content"]))):
            parts = [
                text_part("[earlier screenshot omitted]") if (i, j) in drop else p
                for j, p in enumerate(m["content"])
            ]
            m = {**m, "content": parts}
        out.append(m)
    return out


class RetryPolicy:
    def __init__(self, attempts: int = 6, base: float = 2.0, cap: float = 60.0):
        self.attempts, self.base, self.cap = attempts, base, cap

    def sleep(self, attempt: int) -> None:
        time.sleep(min(self.cap, self.base ** attempt) * (0.5 + random.random() / 2))


# ---------------------------------------------------------------------------
# 测试用模拟 LLM
# ---------------------------------------------------------------------------


class FakeLLM:
    """脚本化模拟 LLM。``script`` 元素可为 `LLMResponse`、字典、字符串或可调用对象 `callable(messages, tools)`。"""

    def __init__(self, script: list[Any] | Callable[..., Any] | None = None, vision: bool = True):
        self.script = script if script is not None else []
        self.vision = vision
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, tools=None, json_mode=False, max_tokens=None):  # noqa: ARG002
        self.calls.append({"messages": messages, "tools": tools, "json_mode": json_mode})
        if callable(self.script):
            item = self.script(messages, tools)
        else:
            if not self.script:
                raise LLMError("FakeLLM script exhausted")
            item = self.script.pop(0)
            if callable(item):
                item = item(messages, tools)
        return to_response(item)


def to_response(item: Any) -> LLMResponse:
    if isinstance(item, LLMResponse):
        return item
    if isinstance(item, str):
        return LLMResponse(content=item)
    if isinstance(item, dict):
        if "tool" in item:  # 简写形式 {"tool": name, "args": {...}, "content": ...}
            return LLMResponse(
                content=item.get("content", ""),
                tool_calls=[ToolCall(id=item.get("id", f"call_{random.randrange(1 << 30)}"),
                                     name=item["tool"], arguments=item.get("args", {}))],
                usage=dict(item.get("usage") or {}),
            )
        return LLMResponse(content=json.dumps(item, ensure_ascii=False))
    raise TypeError(f"unsupported fake response: {item!r}")


# ---------------------------------------------------------------------------
# Token 用量与脚本步骤节省量统计
# ---------------------------------------------------------------------------

import contextlib
import contextvars
import threading


def _approx_msg_tokens(obj: Any) -> int:
    """当服务商或模拟对象未返回用量时的粗略 Token 估算（避免将原始 base64 字节计入）。"""
    if isinstance(obj, str):
        return max(1, len(obj) // 4) if obj else 0
    if isinstance(obj, list):
        total = 0
        for item in obj:
            if isinstance(item, dict) and item.get("type") == "image_url":
                total += 765
            else:
                total += _approx_msg_tokens(item)
        return total
    if isinstance(obj, dict):
        if obj.get("type") == "image_url":
            return 765
        return sum(_approx_msg_tokens(v) for k, v in obj.items() if k != "raw")
    return 0


class UsageMeter:
    """线程安全的累加器，用于统计 LLM Token 消耗与零 Token 脚本步骤节省量。"""

    def __init__(self, tokens_per_script_step: int = 1800, initial: dict[str, Any] | None = None):
        self._lock = threading.Lock()
        self.tokens_per_script_step = max(0, int(tokens_per_script_step))
        init = dict(initial or {})
        self.llm_calls: int = int(init.get("llm_calls") or 0)
        self.prompt_tokens: int = int(init.get("prompt_tokens") or 0)
        self.completion_tokens: int = int(init.get("completion_tokens") or 0)
        self.script_steps: int = int(init.get("script_steps") or 0)
        self.estimated_tokens_saved: int = int(init.get("estimated_tokens_saved") or 0)
        self.by_role: dict[str, dict[str, int]] = {
            k: dict(v) for k, v in (init.get("by_role") or {}).items() if isinstance(v, dict)
        }

    def record_llm(self, role: str, messages: list[dict[str, Any]], resp: LLMResponse) -> None:
        u = resp.usage or {}
        pt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        ct = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        if pt == 0 and ct == 0:
            pt = _approx_msg_tokens(messages)
            ct = _approx_msg_tokens(resp.content) + sum(
                _approx_msg_tokens(tc.raw_arguments or tc.arguments) + 8 for tc in resp.tool_calls
            )
        with self._lock:
            self.llm_calls += 1
            self.prompt_tokens += pt
            self.completion_tokens += ct
            bucket = self.by_role.setdefault(role, {"calls": 0, "tokens": 0})
            bucket["calls"] += 1
            bucket["tokens"] += pt + ct

    def record_script_step(self, saved_tokens: int | None = None) -> None:
        gain = self.tokens_per_script_step if saved_tokens is None else max(0, int(saved_tokens))
        with self._lock:
            self.script_steps += 1
            self.estimated_tokens_saved += gain

    def merge_from(self, other: dict[str, Any] | "UsageMeter" | None) -> None:
        if not other:
            return
        d = other.snapshot() if isinstance(other, UsageMeter) else other
        with self._lock:
            self.llm_calls += int(d.get("llm_calls") or 0)
            self.prompt_tokens += int(d.get("prompt_tokens") or 0)
            self.completion_tokens += int(d.get("completion_tokens") or 0)
            self.script_steps += int(d.get("script_steps") or 0)
            self.estimated_tokens_saved += int(d.get("estimated_tokens_saved") or 0)
            for role, rv in (d.get("by_role") or {}).items():
                if isinstance(rv, dict):
                    b = self.by_role.setdefault(role, {"calls": 0, "tokens": 0})
                    b["calls"] += int(rv.get("calls") or 0)
                    b["tokens"] += int(rv.get("tokens") or 0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self.prompt_tokens + self.completion_tokens
            denom = total + self.estimated_tokens_saved
            return {
                "llm_calls": self.llm_calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": total,
                "script_steps": self.script_steps,
                "estimated_tokens_saved": self.estimated_tokens_saved,
                "savings_ratio": round(self.estimated_tokens_saved / denom, 4) if denom > 0 else 0.0,
                "by_role": {k: dict(v) for k, v in self.by_role.items()},
            }


_ACTIVE_METERS: contextvars.ContextVar[tuple[UsageMeter, ...]] = contextvars.ContextVar("_ACTIVE_METERS", default=())


def record_active_llm_usage(role: str, messages: list[dict[str, Any]], resp: LLMResponse) -> None:
    for meter in _ACTIVE_METERS.get():
        meter.record_llm(role, messages, resp)


def record_active_script_step(saved_tokens: int | None = None) -> None:
    for meter in _ACTIVE_METERS.get():
        meter.record_script_step(saved_tokens)


@contextlib.contextmanager
def usage_scope(meter: UsageMeter):
    stack = _ACTIVE_METERS.get()
    token = _ACTIVE_METERS.set((*stack, meter))
    try:
        yield meter
    finally:
        _ACTIVE_METERS.reset(token)
