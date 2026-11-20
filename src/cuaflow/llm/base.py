"""LLM client layer.

Internally every agent speaks the OpenAI *Chat Completions* message format::

    {"role": "system"|"user"|"assistant"|"tool", "content": str | [parts], ...}

with image parts ``{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}``
and assistant ``tool_calls``. Backends translate as needed.
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
# helpers
# ---------------------------------------------------------------------------


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def image_part(path: str | Path, detail: str = "auto", max_side: int = 1600) -> dict[str, Any]:
    """Encode an image file as a data-URL part (downscaled to ``max_side``)."""
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
    except Exception:  # noqa: BLE001 - fall back to raw bytes
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
    """Parse JSON from model text (tolerates code fences / surrounding prose)."""
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
    """Replace all but the last ``keep_last`` images with a text placeholder (bounds context)."""
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
# Fake LLM for tests
# ---------------------------------------------------------------------------


class FakeLLM:
    """Scripted LLM. ``script`` items are LLMResponse, dicts, strings, or callables(messages, tools)."""

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
        if "tool" in item:  # shorthand {"tool": name, "args": {...}, "content": ...}
            return LLMResponse(
                content=item.get("content", ""),
                tool_calls=[ToolCall(id=item.get("id", f"call_{random.randrange(1 << 30)}"),
                                     name=item["tool"], arguments=item.get("args", {}))],
            )
        return LLMResponse(content=json.dumps(item, ensure_ascii=False))
    raise TypeError(f"unsupported fake response: {item!r}")
