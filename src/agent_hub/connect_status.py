"""Pure redaction and projection of provider connection status."""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping
import unicodedata

PROVIDER_LABELS = {
    "claude": "Claude",
    "grok": "Grok",
    "gemini": "Gemini",
    "gpt": "GPT",
}

PROVIDER_SESSION_LABELS = {
    "claude": "Claude Code 구독 세션",
    "grok": "xAI 구독 세션",
    "gemini": "Google Antigravity 세션",
    "gpt": "공식 Codex ChatGPT 세션",
}

PROVIDER_LOGIN_OWNERS = {
    "claude": "Claude Code",
    "grok": "Agent Hub",
    "gemini": "Agent Hub",
    "gpt": "공식 Codex",
}

MAX_MODEL_TEXT_CHARS = 180
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
PUBLIC_WARNING_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:- "
)


def public_warning(value: Any) -> str:
    warning = str(value or "").strip()
    if 0 < len(warning) <= 80 and all(char in PUBLIC_WARNING_CHARS for char in warning):
        return warning
    return "provider_warning"


def public_model_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > MAX_MODEL_TEXT_CHARS:
        return ""
    if any(unicodedata.category(char).startswith("C") for char in text):
        return ""
    return text


def public_model_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if MODEL_ID_PATTERN.fullmatch(text) else ""


def connection_state(state: Mapping[str, Any]) -> str:
    if state["ready"]:
        return "ready"
    if state["refreshable"]:
        return "refreshable"
    if state["relogin_required"] or (state["account_present"] and not state["logged_in"]):
        return "relogin_required"
    if state["logged_in"] or state["auth_ready"]:
        return "signed_in"
    return "signed_out"


def capability_labels(capabilities: Any) -> list[str]:
    if not isinstance(capabilities, dict):
        return []
    labels = {
        "chat": "대화",
        "compare": "비교",
        "review_diff": "코드 검토",
        "write": "문서 작성",
        "search": "검색",
        "vision": "이미지 입력",
        "image_generation": "이미지 생성",
    }
    return [labels[key] for key in labels if bool((capabilities.get(key) or {}).get("supported"))]


def project_status(raw: Mapping[str, Any]) -> Dict[str, Any]:
    public: Dict[str, Dict[str, Any]] = {}
    for provider_id, state in raw["providers"].items():
        auth_mode = state.get("auth_mode")
        auth_ready = bool(state.get("auth_ready", state.get("authenticated")))
        logged_in = bool(
            state.get(
                "logged_in",
                state.get("configured", state.get("authenticated")),
            )
            or auth_ready
        )
        account_present = bool(
            state.get(
                "account_present",
                state.get("configured", state.get("authenticated")),
            )
            or logged_in
        )
        refreshable = bool(state.get("refreshable") and logged_in and not auth_ready)
        relogin_required = bool(
            not auth_ready
            and not refreshable
            and (state.get("relogin_required") or account_present)
        )
        session_label = PROVIDER_SESSION_LABELS[provider_id]
        if provider_id == "claude" and auth_mode == "api_key":
            session_label = "Claude API key"
        elif provider_id == "grok" and auth_mode == "api_key":
            session_label = "xAI API key"
        elif provider_id == "gpt" and auth_mode in {"api_key", "apiKey"}:
            session_label = "Codex API key"
        safe = {
            "id": provider_id,
            "label": PROVIDER_LABELS[provider_id],
            "session_label": session_label,
            "login_owner": PROVIDER_LOGIN_OWNERS[provider_id],
            "consent": bool(state.get("consent")),
            "configured": bool(state.get("configured", state.get("authenticated"))),
            "authenticated": auth_ready,
            "logged_in": logged_in,
            "auth_ready": auth_ready,
            "account_present": account_present,
            "login_ready": auth_ready,
            "refresh_supported": bool(state.get("refresh_supported") or refreshable),
            "refreshable": refreshable,
            "relogin_required": relogin_required,
            "ready": bool(state.get("ready")),
            "invocation_ready": bool(state.get("invocation_ready", state.get("ready"))),
            "auto_refresh_on_invoke": bool(state.get("auto_refresh_on_invoke")),
            "auth_mode": auth_mode,
            "plan_type": state.get("plan_type"),
            "default_model": public_model_id(state.get("default_model")) or "알 수 없음",
            "base_default_model": public_model_id(state.get("base_default_model")) or None,
            "model_overridden": bool(state.get("model_overridden")),
            "model_managed_by_environment": bool(state.get("model_managed_by_environment")),
            "model_source": public_model_text(state.get("model_source")),
            "model_override_scope": public_model_text(state.get("model_override_scope")) or None,
            "settings_error": (
                public_warning(state.get("settings_error")) if state.get("settings_error") else None
            ),
            "warnings": [public_warning(item) for item in state.get("warnings") or []],
            "supports_local_logout": (provider_id in {"grok", "gemini"}),
            "local_credentials_present": bool(state.get("local_credentials_present")),
            "pending_login_present": bool(state.get("pending_login_present")),
            "login_transport": ("browser" if provider_id in {"grok", "gemini"} else "external_cli"),
            "capabilities": capability_labels(state.get("capabilities")),
        }
        safe["connection_state"] = connection_state(safe)
        public[provider_id] = safe
    return {
        "success": True,
        "providers": public,
        "summary": {
            "ready": sum(item["ready"] for item in public.values()),
            "authenticated": sum(item["authenticated"] for item in public.values()),
            "connected": sum(item["logged_in"] for item in public.values()),
            "refreshable": sum(item["refreshable"] for item in public.values()),
            "relogin_required": sum(item["relogin_required"] for item in public.values()),
            "consent_required": sum(
                item["logged_in"] and not item["consent"] for item in public.values()
            ),
            "total": len(public),
        },
    }
