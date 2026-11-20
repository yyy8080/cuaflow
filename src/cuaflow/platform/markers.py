"""由 ``cuaflow-tool`` 输出并由智能体主循环识别的特殊标记工具。

* ``[[CUAFLOW_IMAGE:<abs path>]]``   —— 将指定图像附加到下一轮观测上下文
* ``[[CUAFLOW_ASK_USER:<question>]]`` —— 智能体请求向人类操作员提问
"""

from __future__ import annotations

import re
from pathlib import Path

IMAGE_MARKER_PREFIX = "[[CUAFLOW_IMAGE:"
ASK_USER_MARKER_PREFIX = "[[CUAFLOW_ASK_USER:"
MARKER_SUFFIX = "]]"

IMAGE_MARKER_RE = re.compile(r"\[\[CUAFLOW_IMAGE:(.+?)\]\]")
ASK_USER_MARKER_RE = re.compile(r"\[\[CUAFLOW_ASK_USER:(.*?)\]\]", re.DOTALL)


def image_marker(path: str | Path) -> str:
    return f"{IMAGE_MARKER_PREFIX}{Path(path).resolve()}{MARKER_SUFFIX}"


def ask_user_marker(question: str) -> str:
    # 内容中的 "]]" 会导致标记提前闭合，此处将其转义拆开。
    return f"{ASK_USER_MARKER_PREFIX}{question.replace(']]', '] ]')}{MARKER_SUFFIX}"


def find_image_markers(text: str) -> list[str]:
    """按出现顺序提取 ``text`` 中图像标记引用的图片绝对路径列表（已去重）。"""
    seen: list[str] = []
    for m in IMAGE_MARKER_RE.finditer(text or ""):
        p = m.group(1).strip()
        if p and p not in seen:
            seen.append(p)
    return seen


def find_ask_user(text: str) -> str | None:
    """提取 ``text`` 中第一个人工提问标记的问题内容，未找到则返回 None。"""
    m = ASK_USER_MARKER_RE.search(text or "")
    return m.group(1).strip() if m else None
