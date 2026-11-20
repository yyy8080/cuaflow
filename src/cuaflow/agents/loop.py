"""通用工具调用智能体循环（重构自上游 CUAWright 的 ``DefaultAgent``）。

每轮迭代：携带当前对话记录与工具列表调用 LLM，依次执行返回的全部工具调用，
并追加执行结果（文本作为 ``tool`` 消息，图像作为后续 ``user`` 消息）。
支持多阶段自动上下文压缩续跑（`max_continuations`）、中途动态转向顺延预算以及超限后平滑交接续跑。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cuaflow.llm.base import LLM, LLMError, image_part, strip_images, text_part

IMAGE_MARKER = re.compile(r"\[\[CUAFLOW_IMAGE:(.+?)\]\]")

COMPACT_PROMPT = """你的工作上下文即将执行压缩续期。请用中文写一份**完整、精炼的交接摘要**，以便接续的你在保留实时浏览器/桌面状态的前提下无缝继续，不丢失任何进度：
1. 原始任务总目标与最新约束（含中途调整要求）；
2. 已完成的步骤与已经提取到的具体数据/中间结论（务必保留关键数值、列表或文件路径）；
3. 已验证有效的选择器、URL、窗口标题以及踩过且不要重复尝试的失败坑点；
4. 当前浏览器/桌面的确切停留状态，以及接下来要立即执行的下一步动作。
请勿调用任何工具，直接输出纯文本摘要。"""

PROGRESS_SUMMARY_PROMPT = """请用简洁清晰的中文（3~6 句话）总结你到目前为止已经完成的操作进展、已经提取到的有效数据或结论，以及尚未完成的剩余步骤。请勿调用任何工具，直接输出纯文本。"""


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
        drain_steering: Callable[[], list[str]] | None = None,
        allow_text_finish: bool = False,
    ):
        self.llm = llm
        self.system_prompt = system_prompt
        self.tools = {t.name: t for t in tools}
        self.role = role
        self.on_log = on_log or (lambda kind, text: None)
        self.keep_images = keep_images
        self.compact_chars = compact_chars
        self.should_stop = should_stop or (lambda: False)
        self.drain_steering = drain_steering
        self.allow_text_finish = allow_text_finish
        self.messages: list[dict[str, Any]] = []
        self._first_user: dict[str, Any] | None = None
        self.total_turns: int = 0

    def _inject_steering(self) -> bool:
        """抽取并注入来自主智能体或用户的中途动态调整指令。"""
        if self.drain_steering is None:
            return False
        notes = self.drain_steering()
        if not notes:
            return False
        for note in notes:
            self.on_log("observation", f"[收到中途调整指令] {note}")
            self.messages.append(
                {
                    "role": "user",
                    "content": (
                        "【主控智能体/用户中途动态调整指令（最高优先级，请立即按此新要求调整后续执行方案）】\n"
                        f"{note}"
                    ),
                }
            )
        return True

    # ------------------------------------------------------------------ 主循环与多阶段自动续跑
    def run(
        self,
        user_content: str | list[dict[str, Any]],
        turn_limit: int = 40,
        prior_messages: list[dict[str, Any]] | None = None,
        *,
        max_continuations: int = 0,
    ) -> AgentResult:
        if prior_messages is not None:
            self.messages = [
                {"role": "system", "content": self.system_prompt},
                *prior_messages,
                {"role": "user", "content": user_content},
            ]
        else:
            self.messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_content},
            ]
        self._first_user = self.messages[-1]
        self.total_turns = 0
        return self._run_segments(turn_limit=max(1, int(turn_limit)), max_continuations=max(0, int(max_continuations)))

    def continue_after_limit(self, extra_turns: int, instruction: str = "") -> AgentResult:
        """在达到轮数上限后压缩历史消息并在同一实时运行环境中继续追加执行轮数。"""
        if len(self.messages) > 8:
            self._compact_context(extra_note=instruction)
        elif instruction.strip():
            self.messages.append({"role": "user", "content": instruction.strip()})
        return self._run_segments(turn_limit=max(1, int(extra_turns)), max_continuations=0)

    def summarize_progress(self) -> str:
        """在不调用任何工具的前提下总结当前已完成进展与剩余事项。"""
        if not self.messages:
            return "尚未产生执行记录。"
        try:
            resp = self.llm.chat(strip_images(self.messages, 1) + [{"role": "user", "content": PROGRESS_SUMMARY_PROMPT}])
            text = (resp.content or "").strip()
            if text:
                return text
        except LLMError:
            pass
        return f"已累计执行 {self.total_turns} 轮工具操作。"

    def _run_segments(self, *, turn_limit: int, max_continuations: int) -> AgentResult:
        had_tool_execution = any(m.get("role") == "tool" for m in self.messages)
        for seg_idx in range(max_continuations + 1):
            is_final_segment = seg_idx >= max_continuations
            seg_budget = turn_limit
            seg_turn = 0
            idle = 0
            while seg_turn < seg_budget:
                seg_turn += 1
                self.total_turns += 1
                if self.should_stop():
                    return AgentResult(False, reason="cancelled", turns=self.total_turns - 1, messages=self.messages)
                if self._inject_steering():
                    # 收到用户中途调整指令时，自动顺延本阶段剩余步数预算，防止因任务变更导致步数耗尽
                    seg_budget = min(turn_limit + 40, max(seg_budget, seg_turn + 25))
                if is_final_segment and (seg_budget - seg_turn == 2) and not self.allow_text_finish:
                    self.messages.append(
                        {
                            "role": "user",
                            "content": (
                                "【步数预算提醒】当前自动执行阶段仅剩最后 3 轮。"
                                "若核心任务已完成，请立即调用 finish 提交结果；"
                                "若仍有收尾步骤，请合并在一次工具调用内完成并随即调用 finish 汇报。"
                            ),
                        }
                    )
                try:
                    resp = self.llm.chat(
                        strip_images(self.messages, self.keep_images),
                        tools=[t.spec for t in self.tools.values()],
                    )
                except LLMError as exc:
                    self.on_log("error", f"LLM error: {exc}")
                    return AgentResult(False, reason=f"llm_error: {exc}", turns=self.total_turns, messages=self.messages)

                self.messages.append(resp.assistant_message())
                content_text = resp.content.strip()
                if content_text:
                    self.on_log("thought", content_text)

                if not resp.tool_calls:
                    if self.allow_text_finish and content_text:
                        return AgentResult(
                            True,
                            final={"reply": content_text, "summary": content_text},
                            turns=self.total_turns,
                            messages=self.messages,
                        )
                    idle += 1
                    # 兼容部分模型在完成操作后直接输出总结文本而忘记显式调用 finish 工具的情况
                    if idle >= 3:
                        if had_tool_execution and "finish" in self.tools and content_text:
                            return AgentResult(
                                True,
                                final={"summary": content_text, "output": {}},
                                turns=self.total_turns,
                                messages=self.messages,
                            )
                        return AgentResult(False, reason="no_tool_calls", turns=self.total_turns, messages=self.messages)
                    if "finish" in self.tools:
                        self.messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "请调用工具继续：如果你已经完成了任务目标，请立即调用 `finish` 工具提交摘要与结果；"
                                    "如果尚未完成，请调用下一步操作工具（不要仅回复纯文本）。"
                                ),
                            }
                        )
                    else:
                        self.messages.append(
                            {
                                "role": "user",
                                "content": "You must call one of the provided tools. Continue with the next tool call.",
                            }
                        )
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
                            had_tool_execution = True
                        except Cancelled:
                            return AgentResult(False, reason="cancelled", turns=self.total_turns, messages=self.messages)
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
                # 若在最后一步执行期间刚好收到主控新的中途调整指令，则暂缓结束并继续按新指令调整
                if finished is not None:
                    if self._inject_steering():
                        seg_budget = min(turn_limit + 40, max(seg_budget, seg_turn + 25))
                    else:
                        return AgentResult(True, final=finished.final, turns=self.total_turns, messages=self.messages)
                self._maybe_compact()

            # 当前分段已达轮数上限：若仍有自动续跑分段额度，则压缩上下文后无缝进入下一阶段
            if not is_final_segment:
                self._compact_context(
                    extra_note=(
                        f"已完成第 {seg_idx + 1} 阶段共 {self.total_turns} 轮操作。"
                        "实时浏览器与桌面会话均完整保留，请从当前状态无缝继续执行剩余未完成步骤，切勿重复已完成的动作。"
                    )
                )

        return AgentResult(False, reason="turn_limit", turns=self.total_turns, messages=self.messages)

    # ------------------------------------------------------------------ 上下文压缩与交接
    def _maybe_compact(self) -> None:
        size = sum(len(json.dumps(m.get("content", ""), ensure_ascii=False)) for m in strip_images(self.messages, 0))
        if size < self.compact_chars:
            return
        self._compact_context()

    def _compact_context(self, extra_note: str = "") -> None:
        try:
            resp = self.llm.chat(strip_images(self.messages, 1) + [{"role": "user", "content": COMPACT_PROMPT}])
            summary = resp.content.strip() or "(empty summary)"
        except LLMError:
            if extra_note.strip():
                self.messages.append({"role": "user", "content": extra_note.strip()})
            return
        self.on_log("thought", f"[上下文已压缩续期]\n{summary[:2000]}")
        handoff = f"## 历史执行交接摘要（上下文已自动压缩，实时环境保持不变）\n{summary}"
        if extra_note.strip():
            handoff += f"\n\n## 续跑指引\n{extra_note.strip()}"
        first_user = self._first_user or (self.messages[1] if len(self.messages) > 1 else {"role": "user", "content": ""})
        self.messages = [
            self.messages[0],
            first_user,
            {"role": "user", "content": handoff},
        ]


def _short_args(args: dict[str, Any], limit: int = 600) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"
