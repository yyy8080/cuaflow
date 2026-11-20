"""UI Automation tree dumps (compact text, analogous to a browser ARIA snapshot).

Line format (one control per line, indented two spaces per level)::

    Button name='保存' auto_id='1' class='Button' rect=(10,20,90,44) enabled focused value='...'

Empty fields are omitted. Uses ``uiautomation`` when installed (fast), otherwise
pywinauto's UIA element wrappers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

VALUE_LIMIT = 80
NAME_LIMIT = 120


@dataclass
class NodeProps:
    control_type: str = ""
    name: str = ""
    auto_id: str = ""
    class_name: str = ""
    rect: tuple[int, int, int, int] | None = None
    enabled: bool | None = True
    focused: bool = False
    offscreen: bool = False
    value: str | None = None
    toggle: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _clip(s: str, limit: int) -> str:
    s = s.replace("\r", " ").replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def format_node(p: NodeProps) -> str:
    parts = [p.control_type or "Unknown"]
    if p.name:
        parts.append(f"name={_clip(p.name, NAME_LIMIT)!r}")
    if p.auto_id:
        parts.append(f"auto_id={_clip(p.auto_id, 60)!r}")
    if p.class_name:
        parts.append(f"class={_clip(p.class_name, 60)!r}")
    if p.rect is not None:
        parts.append("rect=({},{},{},{})".format(*p.rect))
    if p.enabled is not None:
        parts.append("enabled" if p.enabled else "disabled")
    if p.focused:
        parts.append("focused")
    if p.offscreen:
        parts.append("offscreen")
    if p.toggle:
        parts.append(f"toggle={p.toggle}")
    if p.value is not None and p.value != "" and p.value != p.name:
        parts.append(f"value={_clip(p.value, VALUE_LIMIT)!r}")
    return " ".join(parts)


def dump_tree(
    root: Any,
    get_props: Callable[[Any], NodeProps],
    get_children: Callable[[Any], Iterable[Any]],
    max_depth: int = 8,
    max_nodes: int = 400,
) -> str:
    """Generic bounded depth-first dump. ``max_depth`` counts levels below the root (root = 0)."""
    lines: list[str] = []
    depth_cut = 0
    truncated = False
    count = 0

    def visit(node: Any, depth: int) -> None:
        nonlocal depth_cut, truncated, count
        if count >= max_nodes:
            truncated = True
            return
        try:
            props = get_props(node)
            line = format_node(props)
        except Exception as exc:  # noqa: BLE001
            line = f"<unreadable element: {type(exc).__name__}: {exc}>"
        lines.append("  " * depth + line)
        count += 1
        try:
            children = list(get_children(node))
        except Exception:  # noqa: BLE001
            children = []
        if not children:
            return
        if depth >= max_depth:
            depth_cut += 1
            lines[-1] += f" [+{len(children)} children beyond max_depth]"
            return
        for ch in children:
            if count >= max_nodes:
                truncated = True
                return
            visit(ch, depth + 1)

    visit(root, 0)
    if truncated:
        lines.append(f"[... truncated: max_nodes={max_nodes} reached; target a specific window/child or raise max_nodes ...]")
    if depth_cut:
        lines.append(f"[note: {depth_cut} subtree(s) cut at max_depth={max_depth}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# uiautomation backend
# ---------------------------------------------------------------------------

_TOGGLE = {0: "off", 1: "on", 2: "indeterminate"}


def _ua_props(ctrl: Any) -> NodeProps:
    import uiautomation as auto  # type: ignore

    ctype = str(getattr(ctrl, "ControlTypeName", "") or "")
    if ctype.endswith("Control"):
        ctype = ctype[: -len("Control")]
    rect = None
    try:
        r = ctrl.BoundingRectangle
        rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:  # noqa: BLE001
        pass
    value = toggle = None
    try:
        vp = ctrl.GetPattern(auto.PatternId.ValuePattern)
        if vp is not None:
            value = str(vp.Value or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        tp = ctrl.GetPattern(auto.PatternId.TogglePattern)
        if tp is not None:
            toggle = _TOGGLE.get(int(tp.ToggleState))
    except Exception:  # noqa: BLE001
        pass

    def _safe(attr: str, default: Any = "") -> Any:
        try:
            return getattr(ctrl, attr)
        except Exception:  # noqa: BLE001
            return default

    return NodeProps(
        control_type=ctype,
        name=str(_safe("Name") or ""),
        auto_id=str(_safe("AutomationId") or ""),
        class_name=str(_safe("ClassName") or ""),
        rect=rect,
        enabled=bool(_safe("IsEnabled", True)),
        focused=bool(_safe("HasKeyboardFocus", False)),
        offscreen=bool(_safe("IsOffscreen", False)),
        value=value,
        toggle=toggle,
    )


def dump_with_uiautomation(hwnd: int, max_depth: int = 8, max_nodes: int = 400) -> str:
    import uiautomation as auto  # type: ignore

    with auto.UIAutomationInitializerInThread():
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            raise LookupError(f"no UIA element for window handle {hwnd}")
        return dump_tree(root, _ua_props, lambda c: c.GetChildren(), max_depth, max_nodes)


# ---------------------------------------------------------------------------
# pywinauto backend
# ---------------------------------------------------------------------------


def _pwa_props(w: Any) -> NodeProps:
    info = w.element_info
    rect = None
    try:
        r = info.rectangle
        rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:  # noqa: BLE001
        pass
    value = None
    try:
        value = str(w.iface_value.CurrentValue)
    except Exception:  # noqa: BLE001
        pass
    toggle = None
    try:
        toggle = _TOGGLE.get(int(w.iface_toggle.CurrentToggleState))
    except Exception:  # noqa: BLE001
        pass
    focused = False
    try:
        focused = bool(w.has_keyboard_focus())
    except Exception:  # noqa: BLE001
        pass
    return NodeProps(
        control_type=str(getattr(info, "control_type", "") or ""),
        name=str(getattr(info, "name", "") or ""),
        auto_id=str(getattr(info, "automation_id", "") or ""),
        class_name=str(getattr(info, "class_name", "") or ""),
        rect=rect,
        enabled=bool(getattr(info, "enabled", True)),
        focused=focused,
        value=value,
        toggle=toggle,
    )


def dump_with_pywinauto(wrapper: Any, max_depth: int = 8, max_nodes: int = 400) -> str:
    return dump_tree(wrapper, _pwa_props, lambda w: w.children(), max_depth, max_nodes)
