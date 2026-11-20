"""Generic tool-calling agent loop (refactored from upstream CUAWright ``DefaultAgent``).

Each turn: query the LLM with the running transcript + tools, execute every returned tool call,
append results (text as ``tool`` messages, images as a follow-up ``user`` message). Context is
bounded by pruning old screenshots and compacting the transcript into a summary when large.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cuaflow.llm.base import LLM, LLMError, image_part, strip_images, text_part

IMAGE_MARKER = re.compile(r"\[\[CUAFLOW_IMAGE:(.+?)\]\]")

COMPACT_PROMPT = """Your working context is about to be compacted. Write a COMPLETE handoff summary so a fresh
copy of you (same system prompt and task) can continue without losing progress:
- task goal and constraints; current plan and which steps are done / remaining
- key findings (working selectors, window titles, UIA ids, URLs, pitfalls that failed)
- exact current state of the browser/desktop and the next action to take.
Do not call tools. Reply with plain text only."""


@dataclass
class ToolResult:
    text: str
    images: list[str] = field(default_factory=list)
    done: bool = False
    final: Any = None


@dataclass
class Tool:
    spec: dict[str, Any]
    handler: Callable[[dict[str, Any]], ToolResult]

    @property
    def name(self) -> str:
        return self.spec["function"]["name"]


@dataclass
class AgentResult:
    done: bool
    final: Any = None
    turns: int = 0
    reason: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)


class Cancelled(Exception):
    pass


class ToolAgent:
    def __init__(
        self,
        llm: LLM,
        system_prompt: str,
        tools: list[Tool],
        role: str = "agent",
        on_log: Callable[[str, str], None] | None = None,
        keep_images: int = 3,
        compact_chars: int = 240_000,
        should_stop: Callable[[], bool] | None = None,
    ):
        self.llm = llm
        self.system_prompt = system_prompt
        self.tools = {t.name: t for t in tools}
        self.role = role
        self.on_log = on_log or (lambda kind, text: None)
        self.keep_images = keep_images
        self.compact_chars = compact_chars
        self.should_stop = should_stop or (lambda: False)
        self.messages: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    def run(self, user_content: str | list[dict[str, Any]], turn_limit: int = 40) -> AgentResult:
        self.messages = [{"role": "system", "content": self.system_prompt},
                         {"role": "user", "content": user_content}]
        self._first_user = self.messages[1]
        idle = 0
        for turn in range(1, turn_limit + 1):
            if self.should_stop():
                return AgentResult(False, reason="cancelled", turns=turn - 1, messages=self.messages)
            if turn_limit - turn == 2:
                self.messages.append({"role": "user", "content": "[budget] Only 3 turns remain. Finish now if "
                                                                 "possible, or report what blocks you."})
            try:
                resp = self.llm.chat(strip_images(self.messages, self.keep_images),
                                     tools=[t.spec for t in self.tools.values()])
            except LLMError as exc:
                self.on_log("error", f"LLM error: {exc}")
                return AgentResult(False, reason=f"llm_error: {exc}", turns=turn, messages=self.messages)
            self.messages.append(resp.assistant_message())
            if resp.content.strip():
                self.on_log("thought", resp.content.strip())
            if not resp.tool_calls:
                idle += 1
                if idle >= 3:
                    return AgentResult(False, reason="no_tool_calls", turns=turn, messages=self.messages)
                self.messages.append({"role": "user", "content": "You must call one of the provided tools. "
                                                                 "Continue with the next tool call."})
                continue
            idle = 0
            images: list[str] = []
            finished: ToolResult | None = None
            for tc in resp.tool_calls:
                tool = self.tools.get(tc.name)
                self.on_log("command", f"{tc.name}({_short_args(tc.arguments)})")
                if finished is not None:
                    result = ToolResult("Not executed: the task was already finished by a previous call.")
                elif tool is None:
                    result = ToolResult(f"Unknown tool {tc.name!r}. Available: {sorted(self.tools)}")
                elif "__invalid_json__" in tc.arguments:
                    result = ToolResult("Invalid JSON arguments; resend the call with valid JSON.")
                else:
                    try:
                        result = tool.handler(tc.arguments)
                    except Cancelled:
                        return AgentResult(False, reason="cancelled", turns=turn, messages=self.messages)
                    except Exception as exc:  # noqa: BLE001
                        result = ToolResult(f"Tool error: {type(exc).__name__}: {exc}")
                text = result.text
                for m in IMAGE_MARKER.finditer(text):
                    images.append(m.group(1).strip())
                self.on_log("observation", text[:4000])
                self.messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})
                images.extend(result.images)
                if result.done:
                    finished = result
            if images:
                parts: list[dict[str, Any]] = [text_part("Screenshot(s) from the previous tool call(s):")]
                for p in dict.fromkeys(images):
                    if Path(p).is_file():
                        parts.append(image_part(p))
                if len(parts) > 1:
                    self.messages.append({"role": "user", "content": parts})
            if finished is not None:
                return AgentResult(True, final=finished.final, turns=turn, messages=self.messages)
            self._maybe_compact()
        return AgentResult(False, reason="turn_limit", turns=turn_limit, messages=self.messages)

    # ------------------------------------------------------------------
    def _maybe_compact(self) -> None:
        size = sum(len(json.dumps(m.get("content", ""), ensure_ascii=False)) for m in strip_images(self.messages, 0))
        if size < self.compact_chars:
            return
        try:
            resp = self.llm.chat(strip_images(self.messages, 1) + [{"role": "user", "content": COMPACT_PROMPT}])
        except LLMError:
            return
        summary = resp.content.strip() or "(empty summary)"
        self.on_log("thought", f"[context compacted]\n{summary[:2000]}")
        self.messages = [self.messages[0], self._first_user,
                         {"role": "user", "content": f"## Handoff summary (context was compacted)\n{summary}"}]


def _short_args(args: dict[str, Any], limit: int = 600) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"
