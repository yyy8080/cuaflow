"""Output markers printed by ``cuaflow-tool`` and recognized by the agent loop.

* ``[[CUAFLOW_IMAGE:<abs path>]]``   – attach this image to the next observation
* ``[[CUAFLOW_ASK_USER:<question>]]`` – the agent wants to ask the human a question
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
    # "]]" would terminate the marker early; neutralize it.
    return f"{ASK_USER_MARKER_PREFIX}{question.replace(']]', '] ]')}{MARKER_SUFFIX}"


def find_image_markers(text: str) -> list[str]:
    """Absolute image paths referenced by image markers in ``text`` (in order, de-duplicated)."""
    seen: list[str] = []
    for m in IMAGE_MARKER_RE.finditer(text or ""):
        p = m.group(1).strip()
        if p and p not in seen:
            seen.append(p)
    return seen


def find_ask_user(text: str) -> str | None:
    """The first ask-user question in ``text``, or None."""
    m = ASK_USER_MARKER_RE.search(text or "")
    return m.group(1).strip() if m else None
