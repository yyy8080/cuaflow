"""通用生产级智能体循环系统 `ToolAgent`（含双层滚动上下文自动压缩、异常自愈兼容与监督者实时巡检看门狗）。

核心机制：
1. **双层自动上下文压缩**：
   - 微观折叠（零 LLM 开销）：自动将较早轮次的超长工具观测（如旧 ARIA/UIA 树）折叠为首尾摘要，仅保留最近 4 条完整观测；
   - 滚动交接压缩（Rolling Compaction）：当历史字符数超过 `compact_chars`（默认 60,000）或单段步数超过阈值时，自动提炼结构化工作记忆（`working_memory`），同时**严格在安全的 `assistant/tool` 边界保留最近数轮对话**，杜绝孤立 `tool` 消息引发 400 协议报错。
2. **上下文溢出与协议异常自动自愈**：
   - 捕获 `ContextOverflowError` / 400 / 413 长度超限错误时，自动紧急压缩上下文、裁剪旧截图并原地无感重试，绝不直接中断任务。
3. **内置异常看门狗（`LoopWatchdog`）与监督者介入钩子（`supervisor_hook`）**：
   - 实时监测连续工具报错或重复死循环调用，自动触发零 Token 环境自愈并唤醒 `Supervisor` 注入纠偏方案。
4. **持久工作记忆字典（`pinned_notes`）**：通过 `memory_note` 保存的结构化中间结果永久挂载于交接摘要，经历任意次压缩均不丢失。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cuaflow.llm.base import (
    LLM,
    ContextOverflowError,
    LLMError,
    LLMResponse,
    image_part,
    is_context_overflow_error,
    strip_images,
    text_part,
)

IMAGE_MARKER = re.compile(r"\[\[CUAFLOW_IMAGE:(.+?)\]\]")

COMPACT_PROMPT = """你的对话历史即将执行滚动压缩以释放上下文空间。请结合此前的工作记忆与本阶段对话，用中文输出一份**结构清晰、信息完整的最新交接记忆**：
1. 【总目标与约束】：用户原始目标及任何中途动态调整指令；
2. 【已完成进展与已提取数据】：已经完成的动作、关键结论、提取到的具体数值/记录/文件路径（务必原样保留关键数据）；
3. 【有效经验与避坑记录】：已验证有效的 URL、按钮/输入框定位方式，以及失败过切勿重复尝试的方法；
4. 【当前状态与下一步动作】：当前浏览器/桌面停留在何处，接下来应立即执行的下一步具体操作。
请勿调用任何工具，直接输出纯文本摘要。"""

PROGRESS_SUMMARY_PROMPT = """请用简洁清晰的中文（3~6 句话）总结你到目前为止已经完成的操作进展、已经提取到的有效数据或结论，以及尚未完成的剩余步骤。请勿调用任何工具，直接输出纯文本。"""

_ERROR_PREFIXES = (
    "error:",
    "tool error:",
    "observe failed:",
    "python failed:",
    "browser_goto 失败",
    "browser_interact(",
    "browser_extract 失败",
    "desktop_control(",
    "invalid json arguments",
    "unknown tool",
)


def _looks_like_tool_failure(text: str) -> bool:
    raw = (text or "").strip()
    low = raw.lower()
    if not low:
        return False
    first_line = raw.splitlines()[0]
    if any(
        k in first_line
        for k in ("失败：", "未成功：", "异常：", "文件不存在：", "目录不存在：", "参数错误：", "请求失败")
    ):
        return True
    if low.startswith(("error:", "tool error:", "observe failed:", "python failed:", "invalid json arguments", "unknown tool")):
        return True
    if "\nerror:" in low or "traceback (most recent call last)" in low:
        return True
    if low.startswith("exit_code=") and not low.startswith("exit_code=0"):
        return True
    return False


def _find_safe_tail_start(messages: list[dict[str, Any]], min_prefix: int = 2, max_tail: int = 6) -> int:
    """在消息列表中寻找安全的尾部保留起点，确保起点绝不是孤立的 `tool` 消息或紧跟其后的截图附件消息。"""
    n = len(messages)
    if n <= min_prefix + 2:
        return n
    start = max(min_prefix, n - max_tail)
    while start < n:
        role = messages[start].get("role")
        content = messages[start].get("content")
        is_shot_followup = (
            role == "user"
            and isinstance(content, list)
            and any(isinstance(p, dict) and p.get("type") == "image_url" for p in content)
        )
        if role == "tool" or is_shot_followup:
            start += 1
            continue
        break
    return start


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
    working_memory: str = ""


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
        keep_images: int = 2,
        compact_chars: int = 60_000,
        compact_every_turns: int = 24,
        should_stop: Callable[[], bool] | None = None,
        drain_steering: Callable[[], list[str]] | None = None,
        supervisor_hook: Callable[[list[dict[str, Any]], dict[str, Any]], str | None] | None = None,
        allow_text_finish: bool = False,
    ):
        self.llm = llm
        self.system_prompt = system_prompt
        self.tools = {t.name: t for t in tools}
        self.role = role
        self.on_log = on_log or (lambda kind, text: None)
        self.keep_images = keep_images
        self.compact_chars = compact_chars
        self.compact_every_turns = max(8, int(compact_every_turns))
        self.should_stop = should_stop or (lambda: False)
        self.drain_steering = drain_steering
        self.supervisor_hook = supervisor_hook
        self.allow_text_finish = allow_text_finish

        self.messages: list[dict[str, Any]] = []
        self._first_user: dict[str, Any] | None = None
        self.working_memory: str = ""
        self.pinned_notes: dict[str, Any] = {}
        self.total_turns: int = 0
        self._turns_since_compact: int = 0

    def pin_note(self, key: str, value: Any) -> None:
        """写入抗压缩的置顶工作记忆键值。"""
        if key:
            self.pinned_notes[key] = value

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

    def _prune_old_observations(self, keep_recent_tools: int = 4, max_old_chars: int = 1400) -> None:
        """零 LLM 开销的微观上下文修剪：将较早轮次的超长工具返回折叠为首尾摘要。"""
        tool_indices = [i for i, m in enumerate(self.messages) if m.get("role") == "tool"]
        if len(tool_indices) <= keep_recent_tools:
            return
        for idx in tool_indices[:-keep_recent_tools]:
            content = self.messages[idx].get("content")
            if isinstance(content, str) and len(content) > max_old_chars and "[历史观测已自动折叠" not in content:
                omitted = len(content) - 1100
                self.messages[idx] = {
                    **self.messages[idx],
                    "content": (
                        content[:750]
                        + f"\n...[历史观测已自动折叠，省略中间 {omitted} 字符]...\n"
                        + content[-350:]
                    ),
                }

    def _call_llm_resilient(self) -> tuple[LLMResponse | None, str | None]:
        """带上下文超限紧急压缩重试与历史修复的大模型调用封装。"""
        tool_specs = [t.spec for t in self.tools.values()] or None
        try:
            return self.llm.chat(strip_images(self.messages, self.keep_images), tools=tool_specs), None
        except ContextOverflowError as exc:
            self.on_log("observation", f"[上下文超限自动恢复] 正在压缩历史记录并无损重试（{str(exc)[:120]}）")
            self._compact_context(preserve_tail=2, extra_note="前序对话接近上下文上限，已自动压缩历史并保留最新状态，请继续执行。")
            try:
                return self.llm.chat(strip_images(self.messages, 1), tools=tool_specs), None
            except LLMError as exc2:
                return None, str(exc2)
        except LLMError as exc:
            if is_context_overflow_error(exc):
                self.on_log("observation", "[检测到输入长度限制] 正在紧急压缩上下文并重试…")
                self._compact_context(preserve_tail=0, extra_note="已自动精简历史上下文，请从当前实时状态继续。")
                try:
                    return self.llm.chat(strip_images(self.messages, 1), tools=tool_specs), None
                except LLMError as exc2:
                    return None, str(exc2)
            # 若历史消息较长且触发上游协议格式校验异常，尝试全量归并压缩后重试一次
            if len(self.messages) > 6 and ("400" in str(exc) or "422" in str(exc)):
                self.on_log("observation", f"[协议兼容自愈] 压缩历史消息结构后重试（{str(exc)[:120]}）")
                self._compact_context(preserve_tail=0)
                try:
                    return self.llm.chat(strip_images(self.messages, 1), tools=tool_specs), None
                except LLMError as exc2:
                    return None, str(exc2)
            return None, str(exc)

    # ------------------------------------------------------------------ 主循环与多阶段滚动续跑
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
        self._turns_since_compact = 0
        return self._run_segments(turn_limit=max(1, int(turn_limit)), max_continuations=max(0, int(max_continuations)))

    def continue_after_limit(self, extra_turns: int, instruction: str = "") -> AgentResult:
        """在达到轮数上限后压缩历史消息并在同一实时运行环境中继续追加执行轮数。"""
        if len(self.messages) > 6:
            self._compact_context(preserve_tail=4, extra_note=instruction)
        elif instruction.strip():
            self.messages.append({"role": "user", "content": instruction.strip()})
        return self._run_segments(turn_limit=max(1, int(extra_turns)), max_continuations=0)

    def summarize_progress(self) -> str:
        """在不调用任何工具的前提下总结当前已完成进展与剩余事项。"""
        if not self.messages and not self.working_memory:
            return "尚未产生执行记录。"
        try:
            resp = self.llm.chat(strip_images(self.messages, 1) + [{"role": "user", "content": PROGRESS_SUMMARY_PROMPT}])
            text = (resp.content or "").strip()
            if text:
                return text
        except LLMError:
            pass
        if self.working_memory:
            return self.working_memory[:600]
        return f"已累计执行 {self.total_turns} 轮工具操作。"

    def _run_segments(self, *, turn_limit: int, max_continuations: int) -> AgentResult:
        had_tool_execution = any(m.get("role") == "tool" for m in self.messages)
        error_streak = 0
        repeat_streak = 0
        last_call_sig = ""
        supervisor_interventions = 0

        for seg_idx in range(max_continuations + 1):
            is_final_segment = seg_idx >= max_continuations
            seg_budget = turn_limit
            seg_turn = 0
            idle = 0

            while seg_turn < seg_budget:
                seg_turn += 1
                self.total_turns += 1
                self._turns_since_compact += 1

                if self.should_stop():
                    return AgentResult(
                        False,
                        reason="cancelled",
                        turns=self.total_turns - 1,
                        messages=self.messages,
                        working_memory=self.working_memory,
                    )
                if self._inject_steering():
                    # 收到用户中途调整指令时，自动顺延本阶段剩余步数预算
                    seg_budget = min(turn_limit + 50, max(seg_budget, seg_turn + 30))
                    error_streak = 0
                    repeat_streak = 0

                self._prune_old_observations()

                if is_final_segment and (seg_budget - seg_turn == 2) and not self.allow_text_finish:
                    self.messages.append(
                        {
                            "role": "user",
                            "content": (
                                "【步数预算提醒】当前自动执行仅剩最后 3 轮。"
                                "若核心任务已完成，请立即调用 finish 提交结果；"
                                "若仍有收尾步骤，请优先完成关键动作后调用 finish 汇报。"
                            ),
                        }
                    )

                resp, llm_err = self._call_llm_resilient()
                if llm_err is not None or resp is None:
                    self.on_log("error", f"LLM error: {llm_err}")
                    return AgentResult(
                        False,
                        reason=f"llm_error: {llm_err}",
                        turns=self.total_turns,
                        messages=self.messages,
                        working_memory=self.working_memory,
                    )

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
                            working_memory=self.working_memory,
                        )
                    idle += 1
                    if idle >= 3:
                        if had_tool_execution and "finish" in self.tools and content_text:
                            return AgentResult(
                                True,
                                final={"summary": content_text, "output": dict(self.pinned_notes)},
                                turns=self.total_turns,
                                messages=self.messages,
                                working_memory=self.working_memory,
                            )
                        return AgentResult(
                            False,
                            reason="no_tool_calls",
                            turns=self.total_turns,
                            messages=self.messages,
                            working_memory=self.working_memory,
                        )
                    if "finish" in self.tools:
                        self.messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "请调用工具继续：若任务目标已经完成，请立即调用 `finish` 工具提交摘要与结果；"
                                    "若尚未完成，请调用下一步操作工具（如 browser_goto / browser_interact / browser_extract / observe / python）。"
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
                turn_all_failed = True
                turn_sigs: list[str] = []
                last_obs_sample = ""

                for tc in resp.tool_calls:
                    tool = self.tools.get(tc.name)
                    sig = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False)}"
                    turn_sigs.append(sig)
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
                            return AgentResult(
                                False,
                                reason="cancelled",
                                turns=self.total_turns,
                                messages=self.messages,
                                working_memory=self.working_memory,
                            )
                        except Exception as exc:  # noqa: BLE001
                            result = ToolResult(f"Tool error: {type(exc).__name__}: {exc}")

                    text = str(result.text or "")
                    last_obs_sample = text[:1200]
                    if not _looks_like_tool_failure(text):
                        turn_all_failed = False
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
                    if self._inject_steering():
                        seg_budget = min(turn_limit + 50, max(seg_budget, seg_turn + 30))
                    else:
                        return AgentResult(
                            True,
                            final=finished.final,
                            turns=self.total_turns,
                            messages=self.messages,
                            working_memory=self.working_memory,
                        )

                # 看门狗异常检测：连续报错或原样死循环重复调用时唤醒监督者介入自愈纠偏
                joined_sig = "|".join(turn_sigs)
                if joined_sig and joined_sig == last_call_sig:
                    repeat_streak += 1
                else:
                    repeat_streak = 0
                    last_call_sig = joined_sig
                error_streak = (error_streak + 1) if turn_all_failed else 0

                if (
                    self.supervisor_hook is not None
                    and supervisor_interventions < 4
                    and (error_streak >= 3 or repeat_streak >= 3)
                ):
                    trigger_kind = "consecutive_errors" if error_streak >= 3 else "repeated_action_loop"
                    error_streak = 0
                    repeat_streak = 0
                    supervisor_interventions += 1
                    advice = self.supervisor_hook(
                        self.messages,
                        {
                            "kind": trigger_kind,
                            "interventions": supervisor_interventions,
                            "last_call": joined_sig[:400],
                            "last_observation": last_obs_sample,
                        },
                    )
                    if advice and advice.strip():
                        self.on_log("observation", f"[监督者巡检纠偏 #{supervisor_interventions}] {advice[:300]}")
                        self.messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "【监督者 Supervisor 实时巡检与环境自愈指导（请立即切换策略，切勿重复刚才失败的做法）】\n"
                                    f"{advice.strip()}"
                                ),
                            }
                        )

                self._maybe_compact(allow_periodic=(max_continuations > 0 or seg_budget > 20))

            # 当前分段满额：若仍有自动续跑分段额度，则滚动压缩上下文后无缝进入下一阶段
            if not is_final_segment:
                self._compact_context(
                    preserve_tail=6,
                    extra_note=(
                        f"已完成第 {seg_idx + 1} 阶段共 {self.total_turns} 轮操作。"
                        "实时浏览器、桌面窗口与已保存记忆均保持存活，请紧接着当前状态继续推进剩余目标，完成后立即调用 finish。"
                    ),
                )

        return AgentResult(
            False,
            reason="turn_limit",
            turns=self.total_turns,
            messages=self.messages,
            working_memory=self.working_memory,
        )

    # ------------------------------------------------------------------ 滚动上下文压缩与持久记忆挂载
    def _maybe_compact(self, *, allow_periodic: bool = True) -> None:
        size = sum(len(json.dumps(m.get("content", ""), ensure_ascii=False)) for m in strip_images(self.messages, 0))
        if size < self.compact_chars and (not allow_periodic or self._turns_since_compact < self.compact_every_turns):
            return
        self._compact_context(preserve_tail=6)

    def _compact_context(self, *, preserve_tail: int = 6, extra_note: str = "") -> None:
        self._turns_since_compact = 0
        tail_start = _find_safe_tail_start(self.messages, min_prefix=2, max_tail=max(0, preserve_tail))
        preserved_tail = self.messages[tail_start:] if preserve_tail > 0 and tail_start < len(self.messages) else []
        to_summarize = self.messages[:tail_start] if preserved_tail else self.messages

        prompt_parts = [COMPACT_PROMPT]
        if self.working_memory:
            prompt_parts.append(f"\n[上一轮累计工作记忆]:\n{self.working_memory}")
        if self.pinned_notes:
            prompt_parts.append(f"\n[已持久保存的键值笔记]:\n{json.dumps(self.pinned_notes, ensure_ascii=False, indent=2)}")

        summary = ""
        try:
            resp = self.llm.chat(
                strip_images(to_summarize, 1) + [{"role": "user", "content": "\n".join(prompt_parts)}]
            )
            summary = (resp.content or "").strip()
        except LLMError:
            summary = self.working_memory or f"已执行 {self.total_turns} 轮操作。"

        if summary:
            self.working_memory = summary
        self.on_log("thought", f"[上下文已自动滚动压缩（保留最近 {len(preserved_tail)} 条消息）]\n{self.working_memory[:1600]}")

        handoff_blocks = [
            f"## 累计执行交接记忆（早期对话已自动压缩，实时浏览器/桌面环境保持不变）\n{self.working_memory or '(暂无摘要)'}"
        ]
        if self.pinned_notes:
            handoff_blocks.append(
                f"## 已持久化保存的数据（memory_note）\n{json.dumps(self.pinned_notes, ensure_ascii=False, indent=2)}"
            )
        if extra_note.strip():
            handoff_blocks.append(f"## 续跑提示\n{extra_note.strip()}")

        first_user = self._first_user or (self.messages[1] if len(self.messages) > 1 else {"role": "user", "content": ""})
        self.messages = [
            self.messages[0],
            first_user,
            {"role": "user", "content": "\n\n".join(handoff_blocks)},
            *preserved_tail,
        ]


def _short_args(args: dict[str, Any], limit: int = 600) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"
