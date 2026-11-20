"""Zero-token runtime & browser Sentinel (Supervisor L1.5) and output contract checker.

Runs deterministically after every step with zero LLM calls:
- Immediately fails steps that hit hard browser errors (HTTP 4xx/5xx document responses,
  ``chrome-error://`` network pages) or safety boundaries (CAPTCHA / human-verification gates,
  which must NEVER be bypassed).
- Flags suspicious runtime anomalies (unexpected login redirects, background API 401/429/5xx,
  uncaught JS exceptions, error alert banners, blank pages, stalled DOM) so the Supervisor can
  adaptively escalate to the L2 visual/semantic judge even when ``l2_policy == "on_change"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_LOGIN_WORDS = ("login", "sign in", "signin", "auth", "sso", "登录", "登入", "账号", "密码")
_ERROR_EXPECT_WORDS = ("error", "404", "500", "denied", "invalid", "失败", "错误", "报错", "异常", "不存在")
_NEGATED_RE = re.compile(
    r"(无|没有|不出现|未出现|而非|不是|不显示|避免|no\s+|not\s+a?\s*|without\s+|free\s+of\s+)[^，,。;；\n]{1,16}",
    re.IGNORECASE,
)


def _positive_text(step: dict[str, Any]) -> str:
    raw = f"{step.get('title', '')} {step.get('expect', '')}".lower()
    return _NEGATED_RE.sub(" ", raw)


@dataclass
class SentinelReport:
    passed: bool = True
    anomaly: bool = False
    blocker_kind: str | None = None
    detail: str = "clean"
    dom_fingerprint: str | None = None
    warnings: list[str] = field(default_factory=list)


def _expects_error(step: dict[str, Any]) -> bool:
    text = _positive_text(step)
    return any(w in text for w in _ERROR_EXPECT_WORDS)


def _expects_login(step: dict[str, Any]) -> bool:
    text = _positive_text(step)
    return any(w in text for w in _LOGIN_WORDS)


def inspect_step(
    step: dict[str, Any],
    result: dict[str, Any],
    *,
    prev_fingerprint: str | None = None,
    prev_state: dict[str, Any] | None = None,
    step_stats: dict[str, Any] | None = None,
) -> SentinelReport:
    obs = result.get("observation") or {}
    health = obs.get("page_health") or {}
    tel = obs.get("browser_telemetry") or {}
    warnings: list[str] = []

    # 1. Safety boundary: passive CAPTCHA / anti-bot gate detection -> immediate safe stop.
    if health.get("captcha_detected"):
        return SentinelReport(
            passed=False,
            anomaly=True,
            blocker_kind="captcha",
            detail="检测到人机验证/CAPTCHA 页面，已按安全策略停止自动操作并请求人工处理",
            dom_fingerprint=health.get("dom_fingerprint"),
        )

    used_browser = "browser" in (result.get("surfaces") or []) or bool(health)
    expects_err = _expects_error(step)

    if used_browser:
        # 2. Hard browser/network error page
        if health.get("is_error_page") and not expects_err:
            return SentinelReport(
                passed=False,
                anomaly=True,
                blocker_kind="network_error",
                detail=f"浏览器加载错误页 ({health.get('url') or health.get('error_reason')})",
                dom_fingerprint=health.get("dom_fingerprint"),
            )

        # 3. Main-frame HTTP 4xx/5xx error status on navigation
        doc_status = tel.get("doc_status")
        if isinstance(doc_status, int) and doc_status >= 400 and not expects_err:
            if doc_status >= 500 or doc_status in (401, 403, 404, 429):
                return SentinelReport(
                    passed=False,
                    anomaly=True,
                    blocker_kind="http_error",
                    detail=f"页面主文档返回 HTTP {doc_status} ({tel.get('doc_url', '')})",
                    dom_fingerprint=health.get("dom_fingerprint"),
                )

        # 4. Suspicious anomalies -> wake up Adaptive L2 Judge
        if health.get("login_wall_detected") and not _expects_login(step):
            warnings.append("意外跳转到登录页（会话可能已过期）")

        api_errs = [
            e for e in (tel.get("http_errors") or [])
            if not e.get("main_frame") and int(e.get("status") or 0) in (401, 403, 429, 500, 502, 503, 504)
        ]
        if api_errs and not expects_err:
            codes = ", ".join(f"{e['status']}" for e in api_errs[:3])
            warnings.append(f"后台接口返回错误状态码 ({codes})")

        page_errs = tel.get("page_errors") or []
        if page_errs:
            warnings.append(f"页面未捕获 JS 异常: {page_errs[0][:120]}")

        net_fails = tel.get("network_failures") or []
        if net_fails and not expects_err:
            warnings.append(f"网络请求失败 ({len(net_fails)} 项): {net_fails[0].get('failure', '')[:80]}")

        alerts = health.get("alerts") or []
        if alerts and not expects_err:
            warnings.append(f"页面出现告警提示: {alerts[0][:120]}")

        dialogs = tel.get("dialogs") or []
        if dialogs:
            warnings.append(f"捕获浏览器弹窗 ({dialogs[0].get('type')}): {dialogs[0].get('message', '')[:100]}")

        url = str(health.get("url") or obs.get("url") or "")
        if doc_status is not None and health.get("body_text_len") == 0 and url and not url.startswith(("about:", "data:")):
            warnings.append("导航后页面可见正文为空（疑似白屏）")

        fp = health.get("dom_fingerprint")
        if (
            fp
            and prev_fingerprint
            and fp == prev_fingerprint
            and prev_state is not None
            and (result.get("state") or {}) == prev_state
            and not (result.get("output") or {})
        ):
            warnings.append("步骤执行后页面 DOM 与状态均未发生变化（疑似空操作或卡住）")

    new_modals = obs.get("new_modal_dialogs") or []
    if new_modals:
        warnings.append(f"出现新的桌面模态弹窗: {new_modals}")

    if step_stats and int(step_stats.get("runs") or 0) >= 3:
        avg_ms = float(step_stats.get("total_ms") or 0) / max(1, int(step_stats["runs"]))
        dur = float(result.get("duration_ms") or 0)
        if avg_ms > 0 and dur > max(8000.0, avg_ms * 4.5):
            warnings.append(f"耗时显著异常 ({int(dur)}ms vs 历史均值 {int(avg_ms)}ms)")

    detail = "runtime clean" if not warnings else "anomaly: " + "; ".join(warnings)
    return SentinelReport(
        passed=True,
        anomaly=bool(warnings),
        detail=detail,
        dom_fingerprint=health.get("dom_fingerprint"),
        warnings=warnings,
    )


def verify_output_contract(output: dict[str, Any], schema: dict[str, Any] | None) -> tuple[bool, str]:
    """Validate ``c.output`` against ``flow.output_schema``."""
    if not schema:
        return True, "no output schema"
    missing: list[str] = []
    for name, raw_spec in schema.items():
        spec = raw_spec if isinstance(raw_spec, dict) else {"type": str(raw_spec)}
        if spec.get("required") is False or "default" in spec:
            continue
        if name not in output or output[name] is None:
            missing.append(name)
        elif spec.get("type") == "str" and output[name] == "":
            missing.append(f"{name} (empty)")
    if missing:
        return False, f"输出字段缺失或为空: {', '.join(missing)}"
    return True, f"输出契约校验通过 ({len(schema)} 项)"
