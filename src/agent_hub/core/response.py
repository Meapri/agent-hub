"""Shared MCP response helpers."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


def warning_list(*groups: Iterable[Any]) -> List[str]:
    seen = set()
    result: List[str] = []
    for group in groups:
        for item in group or []:
            text = str(item or "").strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
    return result


def standard_fields(
    *,
    success: bool = True,
    provider: str = "",
    backend: str = "api",
    model: str = "",
    usage: Dict[str, Any] | None = None,
    warnings: Iterable[Any] | None = None,
    diagnostics: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "success": bool(success),
        "provider": provider,
        "backend": backend,
        "warnings": warning_list(warnings or []),
    }
    if model:
        data["model"] = model
    if usage is not None:
        data["usage"] = usage
    if diagnostics:
        data["diagnostics"] = diagnostics
    return data


# Codes carry the meaning; the message is a fixed sentence chosen by code rather
# than lifted from the exception. str(exc) on a provider failure can carry a
# request URL, a header fragment, or a path -- openai_codex already guarded
# against that with its own mapping, and claude_codex and grok_codex did not.
_FAILURE_MESSAGES = {
    "codex_unavailable": "The provider CLI is unavailable.",
    "codex_timeout": "The provider generation timed out.",
    "codex_process_error": "The provider generation process failed.",
    "codex_protocol_error": "The provider returned an invalid protocol response.",
    "codex_authentication_required": "The provider requires a login.",
    "codex_subscription_login_required": "The provider requires a subscription login.",
    "codex_side_effect_refused": "The operation was refused by the read-only safety boundary.",
    "explicit_consent_required": "Explicit provider consent is required.",
    "provider_not_configured": "The provider is not configured.",
    "rate_limit": "The provider rate limit was reached.",
    "temporary_unavailable": "The provider is temporarily unavailable.",
}


def failure_payload(
    exc: Exception,
    *,
    provider: str,
    backend: str,
) -> Dict[str, Any]:
    """The shape a failed provider call reports, without echoing the exception.

    agent_hub.v2's failure classification reads `error_type` off this payload
    (see provider_worker._raise_failed_payload), so the code matters and has to
    stay stable. The human-readable half does not need to come from the
    exception, and an exception string is exactly the kind of value that carries
    a URL or a path out to a caller.
    """

    code = str(getattr(exc, "code", "") or type(exc).__name__)
    if "consent" in str(exc).lower():
        code = "explicit_consent_required"
    message = _FAILURE_MESSAGES.get(code, "The provider operation failed.")
    return {
        "text": message,
        "success": False,
        "error": code,
        "error_type": code,
        "provider": provider,
        "backend": backend,
        "warnings": [],
    }
