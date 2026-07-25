"""Canonical Agent Hub operations and their single public registry."""

from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from threading import BoundedSemaphore, Lock, Thread
import time
from typing import Any, Callable, Dict, Iterable, List
import uuid

from agent_hub import (
    capabilities,
    consistency as consistency_gate,
    orchestrator,
    provider_registry,
    provider_settings,
)
from agent_hub.core import handoff as handoff_state
from agent_hub.core import limits, media, parallel
from agent_hub.core import run_lifecycle
from agent_hub.core import takeover as takeover_state
from claude_codex import auth as claude_auth
from claude_codex import models as claude_models
from claude_codex import mcp_server as claude_mcp
from claude_codex import search as claude_search
from claude_codex import security as claude_security
from grok_codex import auth as grok_auth
from grok_codex import models as grok_models
from grok_codex import mcp_server as grok_mcp
from grok_codex import image as grok_image
from grok_codex import search as grok_search
from grok_codex import security as grok_security
from google_antigravity_codex import mcp_server as google_mcp
from google_antigravity_codex import model_prefs as google_model_prefs
from google_antigravity_codex import models as google_models
from google_antigravity_codex import oauth_login as google_oauth
from google_antigravity_codex import profiles as google_profiles
from google_antigravity_codex import provider as google_provider
from google_antigravity_codex import diff_review as google_diff_review
from google_antigravity_codex import release as google_release
from google_antigravity_codex import security as google_security
from google_antigravity_codex import session_prefs as google_session_prefs
from google_antigravity_codex import writing as google_writing
from openai_codex import auth as openai_auth
from openai_codex import models as openai_models
from openai_codex import mcp_server as openai_mcp
from openai_codex import security as openai_security
from orchestrate_codex import (
    broker,
    events as run_events,
    gather,
    policy,
    recipes,
    runner,
    store,
    verify,
)


ProviderHandler = Callable[[Dict[str, Any]], Dict[str, Any]]


def _positive_float_env(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


PROVIDERS = provider_registry.AVAILABLE_PROVIDERS
DEFAULT_COMPARE_PROVIDERS = provider_registry.DEFAULT_COMPARE_PROVIDERS
ADAPTIVE_WORKFLOW_TIMEOUT_MIN = 30.0
ADAPTIVE_MCP_CALL_TIMEOUT = _positive_float_env(
    "AGENT_HUB_MCP_CALL_TIMEOUT",
    limits.MAX_PROVIDER_TIMEOUT_SECONDS,
)
ADAPTIVE_TIMEOUT_RETURN_MARGIN = _positive_float_env(
    "AGENT_HUB_TIMEOUT_RETURN_MARGIN",
    limits.MCP_RETURN_MARGIN_SECONDS,
)
ADAPTIVE_WORKFLOW_TIMEOUT_MAX = max(
    ADAPTIVE_WORKFLOW_TIMEOUT_MIN,
    ADAPTIVE_MCP_CALL_TIMEOUT - ADAPTIVE_TIMEOUT_RETURN_MARGIN,
)
ADAPTIVE_WORKFLOW_TIMEOUT_DEFAULT = min(
    _positive_float_env(
        "AGENT_HUB_WORKFLOW_TIMEOUT",
        limits.MAX_ADAPTIVE_TIMEOUT_SECONDS,
    ),
    ADAPTIVE_WORKFLOW_TIMEOUT_MAX,
)
ADAPTIVE_PER_CALL_TIMEOUT_DEFAULT = _positive_float_env(
    "AGENT_HUB_PER_CALL_TIMEOUT",
    limits.MAX_ADAPTIVE_TIMEOUT_SECONDS,
)
ADAPTIVE_PER_CALL_TIMEOUT_MAX = min(
    limits.MAX_PROVIDER_TIMEOUT_SECONDS,
    ADAPTIVE_WORKFLOW_TIMEOUT_MAX,
)
ADAPTIVE_MAX_WAVES_PER_CALL = limits.MAX_WAVES_PER_CALL
ADAPTIVE_STATE_SCHEMA_VERSION = 2
ADAPTIVE_CALL_ACCOUNTING_VERSION = 2
ADAPTIVE_LEASE_GRACE_SECONDS = 30.0
ADAPTIVE_DEPENDENCY_ITEM_MAX_CHARS = 250_000
ADAPTIVE_DEPENDENCY_CONTEXT_MAX_CHARS = 1_000_000
COMPARE_PARTICIPANT_MAX_CHARS = 250_000
_PROVIDER_CALL_INTERNAL_KEYS = {
    "_provider_call_budget",
    "_provider_call_reservation",
    "_reasoning_effort_implicit",
}
PROVIDER_ALIASES = dict(provider_registry.ALIASES)
_BACKGROUND_CONTINUE_SLOTS = BoundedSemaphore(value=len(PROVIDERS))
_BACKGROUND_CONTINUE_LOCK = Lock()
_BACKGROUND_CONTINUE_THREADS: Dict[str, Thread] = {}

COMMON_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "operation": {"type": "string"},
        "provider": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "text": {"type": "string"},
        "finish_reason": {"type": ["string", "null"]},
        "usage": {"type": "object"},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "error": {"type": ["object", "null"]},
        "artifacts": {"type": "array"},
        "data": {"type": "object"},
    },
    "required": ["success", "operation", "text", "warnings", "data"],
    "additionalProperties": True,
}


def _object(
    properties: Dict[str, Any] | None = None,
    *,
    required: Iterable[str] = (),
    additional: bool = False,
) -> Dict[str, Any]:
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": additional,
    }
    required_list = list(required)
    if required_list:
        schema["required"] = required_list
    return schema


def _provider_property(*, all_value: bool = False, auto: bool = False) -> Dict[str, Any]:
    values = list(PROVIDERS)
    if auto:
        values.insert(0, "auto")
    if all_value:
        values.insert(0, "all")
    return {"type": "string", "enum": values, "default": values[0]}


def _spec(
    name: str,
    title: str,
    description: str,
    schema: Dict[str, Any],
    *,
    read_only: bool,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
) -> Dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": schema,
        "outputSchema": deepcopy(COMMON_OUTPUT_SCHEMA),
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": open_world,
        },
    }


def _with_provider(schema: Dict[str, Any], *, auto: bool = True) -> Dict[str, Any]:
    out = deepcopy(schema)
    props = {"provider": _provider_property(auto=auto), **(out.get("properties") or {})}
    out["properties"] = props
    out["additionalProperties"] = False
    return out


def _with_supported_provider(
    schema: Dict[str, Any], providers: Iterable[str], *, allow_all: bool = False
) -> Dict[str, Any]:
    out = deepcopy(schema)
    values = ["auto", *providers]
    if allow_all:
        values.insert(1, "all")
    out["properties"] = {
        "provider": {"type": "string", "enum": values, "default": values[0]},
        **(out.get("properties") or {}),
    }
    out["additionalProperties"] = False
    return out


def _normalize_provider(value: Any, *, allow_all: bool = False, allow_auto: bool = False) -> str:
    return provider_registry.normalize(
        value,
        allow_all=allow_all,
        allow_auto=allow_auto,
        default="all" if allow_all else "auto" if allow_auto else "",
    )


def _selected_providers(value: Any) -> List[str]:
    provider = _normalize_provider(value or "all", allow_all=True)
    return list(PROVIDERS) if provider == "all" else [provider]


def _error_object(raw: Dict[str, Any]) -> Dict[str, Any] | None:
    value = raw.get("error")
    if not value:
        return None
    if isinstance(value, dict):
        return value
    return {
        "type": str(raw.get("error_type") or "operation_error"),
        "message": str(value),
    }


def envelope(
    operation: str,
    raw: Dict[str, Any] | None,
    *,
    provider: str | None = None,
    success: bool | None = None,
) -> Dict[str, Any]:
    payload = dict(raw or {})
    ok = bool(payload.get("success", payload.get("ok", not payload.get("error"))))
    if success is not None:
        ok = bool(success)
    text = payload.get("text")
    if not isinstance(text, str):
        text = json.dumps(payload, ensure_ascii=False)
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    return {
        "success": ok,
        "operation": operation,
        "provider": provider or payload.get("provider") or None,
        "model": payload.get("model") or None,
        "text": text,
        "finish_reason": payload.get("finish_reason") or None,
        "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
        "warnings": [str(item) for item in warnings],
        "error": None
        if ok
        else _error_object(payload)
        or {
            "type": str(payload.get("error_type") or "operation_failed"),
            "message": text,
        },
        "artifacts": artifacts,
        "data": payload,
    }


def _unwrap_mcp_result(result: Dict[str, Any]) -> Dict[str, Any]:
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        return result
    raw = dict(structured)
    content = result.get("content")
    content_text = ""
    if isinstance(content, list) and content and isinstance(content[0], dict):
        content_text = str(content[0].get("text") or "")
    raw.setdefault("text", content_text)
    raw.setdefault("success", not bool(result.get("isError")))
    if result.get("isError") and not raw.get("error"):
        raw["error"] = content_text or str(raw.get("error_type") or "operation failed")
    return raw


def _provider_model_state(
    provider: str,
    *,
    fallback: str,
    environment_name: str | None = None,
) -> Dict[str, Any]:
    settings, settings_error = provider_settings.inspect(provider)
    saved = str(settings.get("model") or "").strip()
    environment = (
        str(os.getenv(environment_name, "") or "").strip()
        if environment_name
        else ""
    )
    return {
        "default_model": saved or environment or fallback,
        "base_default_model": fallback,
        "model_overridden": bool(saved),
        "model_managed_by_environment": bool(environment and not saved),
        "model_source": (
            "saved" if saved else "environment" if environment else "provider_default"
        ),
        "settings_error": settings_error,
        "settings": settings,
    }


def _gemini_model_state() -> Dict[str, Any]:
    prefs, settings_error = google_model_prefs.inspect_prefs()
    tasks = prefs.get("task_models") if isinstance(prefs.get("task_models"), dict) else {}
    task_model = str(tasks.get("chat") or "").strip()
    saved_default = str(prefs.get("default_model") or "").strip()
    environment = str(os.getenv("GOOGLE_ANTIGRAVITY_DEFAULT_MODEL", "") or "").strip()
    profile = google_profiles.active_profile()
    profile_model = str((profile or {}).get("model") or "").strip()
    profile_name = str((profile or {}).get("name") or "").strip()
    selected = (
        task_model
        or saved_default
        or environment
        or profile_model
        or "gemini-3.5-flash-high"
    )
    return {
        "default_model": google_model_prefs.normalize_model_id(selected),
        "base_default_model": "gemini-3.5-flash-high",
        "model_overridden": bool(task_model or saved_default),
        "model_managed_by_environment": bool(
            environment and not task_model and not saved_default
        ),
        "model_source": (
            "saved_chat"
            if task_model
            else "saved"
            if saved_default
            else "environment"
            if environment
            else "profile"
            if profile_model
            else "provider_default"
        ),
        "model_override_scope": (
            "task:chat"
            if task_model
            else "default"
            if saved_default
            else f"profile:{profile_name}"
            if profile_model and profile_name
            else None
        ),
        "settings_error": settings_error,
    }


def _effective_provider_models() -> Dict[str, str]:
    return {
        "claude": _provider_model_state(
            "claude",
            fallback=claude_models.DEFAULT_MODEL,
            environment_name="CLAUDE_CODEX_MODEL",
        )["default_model"],
        "grok": _provider_model_state(
            "grok",
            fallback=grok_models.DEFAULT_MODEL,
            environment_name="GROK_CODEX_MODEL",
        )["default_model"],
        "gemini": _gemini_model_state()["default_model"],
        "gpt": _provider_model_state(
            "gpt",
            fallback=openai_models.DEFAULT_MODEL,
        )["default_model"],
    }


def _snapshot_provider_models(explicit: Any = None) -> Dict[str, str]:
    """Freeze every provider model, overlaying only non-empty explicit choices."""

    snapshot = _effective_provider_models()
    if isinstance(explicit, dict):
        for provider in PROVIDERS:
            selected = str(explicit.get(provider) or "").strip()
            if selected:
                snapshot[provider] = selected
    return snapshot


def _auth_lifecycle(
    *,
    account_present: bool,
    logged_in: bool,
    auth_ready: bool,
    refresh_supported: bool,
) -> Dict[str, bool]:
    """Return the redacted auth lifecycle shared by every provider."""

    auth_ready = bool(auth_ready)
    logged_in = bool(logged_in or auth_ready)
    account_present = bool(account_present or logged_in)
    refreshable = bool(logged_in and not auth_ready and refresh_supported)
    return {
        "account_present": account_present,
        "logged_in": logged_in,
        "auth_ready": auth_ready,
        "refresh_supported": bool(refresh_supported),
        "refreshable": refreshable,
        "relogin_required": bool(
            account_present and not auth_ready and not refreshable
        ),
    }


def _status(args: Dict[str, Any]) -> Dict[str, Any]:
    probe = bool(args.get("probe", False))
    states: Dict[str, Any] = {}
    for provider in _selected_providers(args.get("provider")):
        if provider == "claude":
            consent = claude_security.consent_status()
            auth = claude_auth.status()
            authenticated = bool(auth.get("ready"))
            subscription = auth.get("subscription") or {}
            lifecycle = _auth_lifecycle(
                account_present=bool(auth.get("credentials_present")),
                logged_in=bool(auth.get("credentials_present")),
                auth_ready=authenticated,
                refresh_supported=bool(
                    subscription.get("logged_in")
                    and subscription.get("has_refresh_token")
                ),
            )
            model_state = _provider_model_state(
                "claude",
                fallback=claude_models.DEFAULT_MODEL,
                environment_name="CLAUDE_CODEX_MODEL",
            )
            states[provider] = {
                "consent": bool(consent.get("user_consent")),
                "configured": bool(auth.get("configured")),
                "authenticated": authenticated,
                "ready": bool(consent.get("user_consent") and authenticated),
                "auth_mode": auth.get("active_mode"),
                **lifecycle,
                **model_state,
                "capabilities": capabilities.provider_capabilities("claude"),
                "warnings": (
                    []
                    if authenticated
                    else [
                        "auth_refresh_available"
                        if lifecycle["refreshable"]
                        else "reauthentication_required"
                        if lifecycle["relogin_required"]
                        else "credentials_missing"
                    ]
                )
                + ([model_state["settings_error"]] if model_state["settings_error"] else []),
            }
        elif provider == "grok":
            consent = grok_security.consent_status()
            auth = grok_auth.status()
            authenticated = bool(auth.get("ready"))
            subscription = auth.get("subscription") or {}
            account_present = bool(
                auth.get("credentials_present")
                or subscription.get("token_file_present")
            )
            lifecycle = _auth_lifecycle(
                account_present=account_present,
                logged_in=bool(auth.get("credentials_present")),
                auth_ready=authenticated,
                refresh_supported=bool(
                    subscription.get("logged_in")
                    and subscription.get("has_refresh_token")
                ),
            )
            model_state = _provider_model_state(
                "grok",
                fallback=grok_models.DEFAULT_MODEL,
                environment_name="GROK_CODEX_MODEL",
            )
            states[provider] = {
                "consent": bool(consent.get("user_consent")),
                "configured": bool(auth.get("configured")),
                "authenticated": authenticated,
                "ready": bool(consent.get("user_consent") and authenticated),
                "auth_mode": auth.get("active_mode"),
                **lifecycle,
                "local_credentials_present": bool(
                    (auth.get("subscription") or {}).get("token_file_present")
                ),
                "pending_login_present": bool(
                    (auth.get("subscription") or {}).get("pending_login_present")
                ),
                **model_state,
                "capabilities": capabilities.provider_capabilities("grok"),
                "warnings": (
                    []
                    if authenticated
                    else [
                        "auth_refresh_available"
                        if lifecycle["refreshable"]
                        else "reauthentication_required"
                        if lifecycle["relogin_required"]
                        else "credentials_missing"
                    ]
                )
                + ([model_state["settings_error"]] if model_state["settings_error"] else []),
            }
        elif provider == "gpt":
            consent = openai_security.consent_status()
            auth = openai_auth.status(refresh=False)
            model_state = _provider_model_state(
                "gpt",
                fallback=openai_models.DEFAULT_MODEL,
            )
            warnings = [str(auth["warning"])] if auth.get("warning") else []
            if auth.get("status_warning"):
                warnings.append(str(auth["status_warning"]))
            if auth.get("error_type"):
                warnings.append(str(auth["error_type"]))
            if model_state["settings_error"]:
                warnings.append(model_state["settings_error"])
            lifecycle = _auth_lifecycle(
                account_present=bool(auth.get("logged_in")),
                logged_in=bool(auth.get("logged_in")),
                auth_ready=bool(auth.get("configured")),
                refresh_supported=False,
            )
            states[provider] = {
                "consent": bool(consent.get("user_consent")),
                "configured": bool(auth.get("configured")),
                "authenticated": bool(auth.get("configured")),
                "ready": bool(consent.get("user_consent") and auth.get("configured")),
                "auth_mode": auth.get("auth_mode"),
                "plan_type": auth.get("plan_type"),
                **lifecycle,
                **model_state,
                "capabilities": capabilities.provider_capabilities("gpt"),
                "warnings": warnings,
            }
        else:
            consent = google_security.consent_status()
            provider_state = google_provider.status(probe=probe)
            login = google_oauth.login_status()
            authenticated = bool(
                login.get("credentials_readable") and login.get("expired") is not True
            )
            lifecycle = _auth_lifecycle(
                account_present=bool(login.get("token_file_present")),
                logged_in=bool(login.get("credentials_readable")),
                auth_ready=authenticated,
                refresh_supported=bool(login.get("refresh_token_present")),
            )
            configured = bool(provider_state.get("configured"))
            healthy = provider_state.get("healthy")
            ready = bool(
                (consent.get("user_consent") or consent.get("agy_session_enabled"))
                and authenticated
                and configured
                and healthy is not False
            )
            model_state = _gemini_model_state()
            states[provider] = {
                "consent": bool(consent.get("user_consent") or consent.get("agy_session_enabled")),
                "configured": configured,
                "authenticated": authenticated,
                "ready": ready,
                "auth_mode": provider_state.get("auth_method") or "plugin_oauth_login",
                **lifecycle,
                "local_credentials_present": bool(login.get("token_file_present")),
                "pending_login_present": bool(login.get("pending_login")),
                **model_state,
                "quota_state": "unknown",
                "quota_telemetry_available": False,
                "quota_available": None,
                "quota_exhausted": None,
                "capabilities": capabilities.provider_capabilities("gemini"),
                "warnings": (
                    []
                    if ready
                    else [
                        "auth_refresh_available"
                        if lifecycle["refreshable"]
                        else "reauthentication_required"
                        if lifecycle["relogin_required"]
                        else str(
                            provider_state.get("error_type")
                            or "provider_not_ready"
                        )
                    ]
                )
                + ([model_state["settings_error"]] if model_state["settings_error"] else []),
            }
    ready_count = sum(bool(item.get("ready")) for item in states.values())
    raw = {
        "success": True,
        "text": f"{ready_count}/{len(states)} selected providers are ready.",
        "providers": states,
        "probe": probe,
    }
    return envelope("status", raw, success=True)


def _list_models(args: Dict[str, Any]) -> Dict[str, Any]:
    probe = bool(args.get("probe", False))
    listed: Dict[str, Any] = {}
    warnings: List[str] = []
    for provider in _selected_providers(args.get("provider")):
        try:
            if provider == "claude":
                listed[provider] = claude_models.list_models({"probe": probe})
            elif provider == "grok":
                listed[provider] = grok_models.list_models({"probe": probe})
            elif provider == "gpt":
                listed[provider] = openai_models.list_models({"probe": probe})
            else:
                listed[provider] = _unwrap_mcp_result(
                    google_mcp.dispatch_tool("google_antigravity_list_models", {})
                )
        except Exception as exc:  # noqa: BLE001
            listed[provider] = {"success": False, "error": str(exc)}
            warnings.append(f"{provider}:model_list_failed")
    raw = {
        "success": True,
        "text": f"Model catalogs returned for {len(listed)} provider(s).",
        "models": listed,
        "warnings": warnings,
    }
    return envelope("list_models", raw, success=True)


def _auth_gui_action(
    args: Dict[str, Any],
    *,
    operation: str,
) -> Dict[str, Any]:
    provider = _normalize_provider(args.get("provider"))
    console_script = Path(sys.executable).with_name("agent-hub-connect")
    if console_script.is_file() and os.access(console_script, os.X_OK):
        command = str(console_script)
        command_args: list[str] = []
    else:
        command = sys.executable
        command_args = ["-m", "agent_hub.connect_app"]
    raw = {
        "success": False,
        "text": (
            "Open the local Agent Hub connection manager and confirm the account action "
            "in its browser tab."
        ),
        "next_action": {
            "type": "local_gui",
            "command": command,
            "args": command_args,
            "provider": provider,
        },
        "error": "provider_gui_required",
        "error_type": "provider_gui_required",
    }
    return envelope(operation, raw, provider=provider, success=False)


def _auth_start(args: Dict[str, Any]) -> Dict[str, Any]:
    return _auth_gui_action(args, operation="auth_start")


def _auth_complete(args: Dict[str, Any]) -> Dict[str, Any]:
    return _auth_gui_action(args, operation="auth_complete")


def _auth_refresh(args: Dict[str, Any]) -> Dict[str, Any]:
    return _auth_gui_action(args, operation="auth_refresh")


def _auth_logout(args: Dict[str, Any]) -> Dict[str, Any]:
    return _auth_gui_action(args, operation="auth_logout")


def _auto_chat_provider(args: Dict[str, Any]) -> str:
    requested = _normalize_provider(args.get("provider") or "auto", allow_auto=True)
    if requested != "auto":
        return requested
    model = str(args.get("model") or "").lower()
    routed = provider_registry.provider_for_model(model, default="")
    if routed:
        return routed
    return "claude"


_BASIC_CHAT_KEYS = {
    "prompt",
    "system",
    "model",
    "max_tokens",
    "temperature",
    "timeout_sec",
    "messages",
    "images",
    "workspace_root",
    "api_mode",
    "session_id",
    "tools",
    "reasoning_effort",
}


def _operation_provider(args: Dict[str, Any], capability: str, *, default: str = "gemini") -> str:
    requested = _normalize_provider(args.get("provider") or "auto", allow_auto=True)
    if requested != "auto":
        capabilities.require(requested, capability)
        return requested
    model = str(args.get("model") or "").lower()
    routed = provider_registry.provider_for_model(model, default="")
    if routed:
        selected = routed
    elif capability == "search" and str(args.get("source") or "").lower() in {"x", "both"}:
        selected = "grok"
    else:
        selected = default
    capabilities.require(selected, capability)
    return selected


def _prepare_multimodal(call_args: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(call_args)
    images = media.normalize_images(
        out.pop("images", None), workspace_root=out.get("workspace_root")
    )
    out.pop("workspace_root", None)
    if not images:
        return out
    prompt = str(out.get("prompt") or "")
    messages = out.get("messages") if isinstance(out.get("messages"), list) else []
    messages = [dict(item) for item in messages if isinstance(item, dict)]
    if not messages and out.get("system"):
        messages.append({"role": "system", "content": str(out["system"])})
    messages.append({"role": "user", "content": media.user_content(prompt, images)})
    out["messages"] = messages
    out.pop("prompt", None)
    out.pop("system", None)
    return out


def _provider_dispatch_scope(args: Dict[str, Any]) -> Any:
    reservation = args.get("_provider_call_reservation")
    if reservation is not None:
        return reservation.dispatch()
    budget = args.get("_provider_call_budget")
    if budget is not None:
        return budget.dispatch()
    return nullcontext()


def _clamp_provider_timeout(call_args: Dict[str, Any], remaining_seconds: float | None) -> None:
    if remaining_seconds is None:
        return
    requested = float(call_args.get("timeout_sec") or remaining_seconds)
    call_args["timeout_sec"] = max(0.001, min(requested, remaining_seconds))


def _chat_raw(provider: str, args: Dict[str, Any]) -> Dict[str, Any]:
    capabilities.require(provider, "chat")
    effort_is_implicit = bool(args.get("_reasoning_effort_implicit", False))
    call_args = {
        k: v
        for k, v in args.items()
        if k != "provider" and k not in _PROVIDER_CALL_INTERNAL_KEYS and v is not None
    }
    if provider in {"claude", "grok", "gpt"}:
        defaults = provider_settings.get(provider)
        for key, value in defaults.items():
            call_args.setdefault(key, value)
    effective_model = str(call_args.get("model") or "").strip()
    if not effective_model:
        if provider == "gemini":
            effective_model = str(_gemini_model_state()["default_model"])
        else:
            effective_model = str(_effective_provider_models().get(provider) or "")
    omitted_effort_warning = ""
    if (
        effort_is_implicit
        and call_args.get("reasoning_effort") is not None
        and effective_model
        and not capabilities.supports_reasoning_effort(provider, effective_model)
    ):
        call_args.pop("reasoning_effort", None)
        omitted_effort_warning = (
            f"automatic_reasoning_effort_omitted:{provider}:{effective_model}"
        )
    call_args = _prepare_multimodal(call_args)
    call_args, provenance = consistency_gate.prepare_provider_call(call_args)
    with _provider_dispatch_scope(args) as remaining_seconds:
        _clamp_provider_timeout(call_args, remaining_seconds)
        if provider == "claude":
            raw = claude_mcp.dispatch_tool(
                "claude_codex_chat",
                {k: v for k, v in call_args.items() if k in _BASIC_CHAT_KEYS},
            )
        elif provider == "grok":
            raw = grok_mcp.dispatch_tool(
                "grok_codex_chat",
                {k: v for k, v in call_args.items() if k in _BASIC_CHAT_KEYS},
            )
        elif provider == "gpt":
            raw = openai_mcp.dispatch_tool(
                "openai_codex_chat",
                {k: v for k, v in call_args.items() if k in _BASIC_CHAT_KEYS},
            )
        else:
            if call_args.get("reasoning_effort") is not None:
                call_args["thinking_level"] = call_args.pop("reasoning_effort")
            raw = _unwrap_mcp_result(google_mcp.dispatch_tool("google_antigravity_chat", call_args))
    result = dict(raw)
    if omitted_effort_warning:
        warnings = [str(item) for item in result.get("warnings") or []]
        if omitted_effort_warning not in warnings:
            warnings.append(omitted_effort_warning)
        result["warnings"] = warnings
    existing = result.get("consistency") if isinstance(result.get("consistency"), dict) else {}
    result["consistency"] = {**existing, **provenance}
    return result


def _chat(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _auto_chat_provider(args)
    raw = _chat_raw(provider, args)
    return envelope("chat", raw, provider=provider)


def _search(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _operation_provider(args, "search", default="gemini")
    call_args = {
        key: value
        for key, value in args.items()
        if key != "provider" and key not in _PROVIDER_CALL_INTERNAL_KEYS and value is not None
    }
    with _provider_dispatch_scope(args) as remaining_seconds:
        _clamp_provider_timeout(call_args, remaining_seconds)
        if provider == "claude":
            raw = claude_search.run_search(call_args)
        elif provider == "grok":
            raw = grok_search.run_search(call_args)
        else:
            raw = _unwrap_mcp_result(google_mcp.dispatch_tool("google_grounded_search", call_args))
    return envelope("search", raw, provider=provider)


def _write_call(
    provider: str,
    args: Dict[str, Any],
    built: Dict[str, Any],
    *,
    prompt: str,
) -> Dict[str, Any]:
    return _chat_raw(
        provider,
        {
            "prompt": prompt,
            "system": built["system"],
            "model": args.get("model"),
            "temperature": args.get("temperature", 0.35),
            "max_tokens": args.get("max_tokens"),
            "reasoning_effort": args.get("reasoning_effort"),
            "timeout_sec": (
                args.get("timeout_sec") or limits.MAX_PROVIDER_TIMEOUT_SECONDS
            ),
            "project_root": args.get("project_root"),
            "policy_mode": args.get("policy_mode"),
            "policy_file": args.get("policy_file"),
            "max_policy_chars": args.get("max_policy_chars"),
            "_provider_call_budget": args.get("_provider_call_budget"),
            "_provider_call_reservation": args.get("_provider_call_reservation"),
            "_reasoning_effort_implicit": args.get("_reasoning_effort_implicit"),
        },
    )


def _write_verification(text: str, built: Dict[str, Any]) -> Dict[str, Any]:
    return verify.verify_text(
        text,
        doc_class=str(built.get("doc_class") or "transform"),
        fact_pack=built.get("fact_pack") if isinstance(built.get("fact_pack"), dict) else None,
        user_facing=built.get("task") == "readme",
    )


def _rewrite_prompt(built: Dict[str, Any], text: str, warnings: List[str]) -> str:
    findings = "\n".join(f"- {item}" for item in warnings)
    return (
        f"{built['prompt']}\n\n"
        "The following draft failed Agent Hub's document quality gate. Rewrite the complete "
        "document, not just the flagged sentences. Preserve every verified fact, command, "
        "constraint, and useful section. For Korean user-facing documentation, use natural "
        "polite prose that a Korean developer would actually write; avoid translated English "
        "structure, unexplained internal jargon, process narration, and repeated '-한다/-이다' "
        "endings. Return only the corrected final document.\n\n"
        f"Quality findings:\n{findings}\n\n"
        f"Draft to replace:\n{text}"
    )


def _write(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _operation_provider(args, "write", default="gemini")
    built = google_writing.build_prompt(args)
    raw_chat = _write_call(provider, args, built, prompt=str(built["prompt"]))
    text = str(raw_chat.get("text") or "").strip()
    verification = _write_verification(text, built)
    rewrite_limit = max(0, min(int(args.get("quality_rewrite_attempts", 2)), 2))
    rewrite_attempts = 0
    while (
        bool(raw_chat.get("success", not raw_chat.get("error")))
        and not verification.get("ok")
        and rewrite_attempts < rewrite_limit
    ):
        rewrite_attempts += 1
        raw_chat = _write_call(
            provider,
            args,
            built,
            prompt=_rewrite_prompt(built, text, list(verification.get("warnings") or [])),
        )
        text = str(raw_chat.get("text") or "").strip()
        verification = _write_verification(text, built)
    warnings = google_writing.review_text(text, durable=bool(built.get("durable")))
    warnings.extend(
        str(item) for item in raw_chat.get("warnings") or [] if str(item) not in warnings
    )
    warnings.extend(
        str(item) for item in verification.get("warnings") or [] if str(item) not in warnings
    )
    if rewrite_attempts:
        warnings.append(f"quality_rewrite_applied:{rewrite_attempts}")
    provider_success = bool(raw_chat.get("success", not raw_chat.get("error")))
    quality_passed = bool(verification.get("ok"))
    raw = {
        **raw_chat,
        "success": provider_success and quality_passed,
        "text": text,
        "task": built["task"],
        "profiles": built["profiles"],
        "doc_class": built.get("doc_class"),
        "project_context_used": built.get("project_context_used"),
        "fact_pack_used": built.get("fact_pack_used"),
        "quality_gate": {
            "applied": True,
            "passed": quality_passed,
            "checker_version": verification.get("checker_version"),
            "rewrite_attempts": rewrite_attempts,
            "warnings": list(verification.get("warnings") or []),
            "policy_source": (
                raw_chat.get("consistency", {}).get("policy_source")
                if isinstance(raw_chat.get("consistency"), dict)
                else None
            ),
        },
        "call_usage": {"provider_calls": 1 + rewrite_attempts},
        "warnings": warnings,
    }
    if provider_success and not quality_passed:
        raw["error"] = {
            "type": "document_quality_failed",
            "message": "Document quality verification failed after bounded rewriting.",
        }
    return envelope("write", raw, provider=provider)


def _generate_image(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _operation_provider(args, "image_generation", default="gemini")
    call_args = {
        key: value for key, value in args.items() if key != "provider" and value is not None
    }
    if provider == "grok":
        raw = grok_image.generate_image(call_args)
    else:
        raw = _unwrap_mcp_result(
            google_mcp.dispatch_tool("google_antigravity_generate_image", call_args)
        )
    return envelope("generate_image", raw, provider=provider)


def _model_provider(model: str) -> str:
    return provider_registry.provider_for_model(model)


def _compare_models(args: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    raw_models = args.get("models")
    if isinstance(raw_models, str):
        models = [item.strip() for item in raw_models.replace(";", ",").split(",") if item.strip()]
    elif isinstance(raw_models, list):
        models = [str(item).strip() if item is not None else "" for item in raw_models]
    else:
        models = []
    raw_providers = args.get("providers")
    providers = (
        [_normalize_provider(item) for item in raw_providers]
        if isinstance(raw_providers, list) and raw_providers
        else []
    )
    requested = str(args.get("provider") or "auto").lower()
    if not providers and requested not in {"auto", "all", ""}:
        providers = [_normalize_provider(requested)]
    if not providers and requested == "all":
        providers = list(PROVIDERS)
    if not providers and any(models):
        providers = [
            _model_provider(model.split("/", 1)[-1])
            for model in models
            if model
        ]
    if not providers:
        providers = list(DEFAULT_COMPARE_PROVIDERS)
    targets: List[tuple[str, str | None]] = []
    for index, provider in enumerate(providers[: len(PROVIDERS)]):
        model = (models[index] or None) if index < len(models) else None
        if model and "/" in model and model.split("/", 1)[0] in PROVIDERS:
            prefix, model = model.split("/", 1)
            provider = prefix
        capabilities.require(provider, "compare")
        targets.append((provider, model))
    raw_min_successes = args.get("min_successes", min(2, len(targets)))
    if isinstance(raw_min_successes, bool) or not isinstance(raw_min_successes, int):
        raise ValueError("min_successes must be an integer")
    min_successes = raw_min_successes
    if not 1 <= min_successes <= len(targets):
        raise ValueError(f"min_successes must be within 1..{len(targets)}")
    gate_value = args.get("consistency")
    gate_config = dict(gate_value) if isinstance(gate_value, dict) else {}
    gate_enabled = bool(gate_config.get("enabled", bool(gate_config)))
    labels: List[str] = []
    call_prompt = prompt
    project_root = str(gate_config.get("project_root") or args.get("project_root") or ".")
    policy_mode = str(
        gate_config.get("policy_mode")
        or args.get("policy_mode")
        or ("required" if gate_enabled else "auto")
    )
    policy_file = str(gate_config.get("policy_file") or args.get("policy_file") or "")
    max_policy_chars = int(
        gate_config.get("max_policy_chars")
        or args.get("max_policy_chars")
        or consistency_gate.DEFAULT_MAX_POLICY_CHARS
    )
    if gate_enabled:
        labels = consistency_gate.validate_labels(gate_config.get("decision_labels") or [])
        consistency_gate.load_policy(
            project_root=project_root,
            policy_file=policy_file,
            required=policy_mode == "required",
            max_chars=max_policy_chars,
        )
        call_prompt = consistency_gate.decision_prompt(prompt, labels)

    def call_target(provider: str, model: str | None) -> Dict[str, Any]:
        return _chat_raw(
            provider,
            {
                "prompt": call_prompt,
                "system": args.get("system"),
                "model": model,
                "temperature": args.get("temperature", 0.2),
                "max_tokens": args.get("max_tokens"),
                "reasoning_effort": args.get("reasoning_effort"),
                "timeout_sec": (
                    args.get("timeout_sec")
                    or limits.MAX_PROVIDER_TIMEOUT_SECONDS
                ),
                "project_root": project_root
                if (gate_enabled or args.get("project_root"))
                else None,
                "policy_mode": policy_mode,
                "policy_file": policy_file or None,
                "max_policy_chars": max_policy_chars,
                "_provider_call_budget": args.get("_provider_call_budget"),
                "_provider_call_reservation": active_reservation,
                "_reasoning_effort_implicit": args.get("_reasoning_effort_implicit"),
            },
        )

    execution = str(args.get("execution") or "parallel")
    max_concurrency = int(args.get("max_concurrency") or len(PROVIDERS))
    active_reservation = args.get("_provider_call_reservation")
    owned_reservation = None
    if active_reservation is None and args.get("_provider_call_budget") is not None:
        owned_reservation = args["_provider_call_budget"].reserve(len(targets))
        active_reservation = owned_reservation
    reservation_calls_before = active_reservation.used if active_reservation is not None else 0
    try:
        outcomes = parallel.run_ordered(
            [lambda p=provider, m=model: call_target(p, m) for provider, model in targets],
            execution=execution,
            max_workers=max_concurrency,
        )
    finally:
        if owned_reservation is not None:
            owned_reservation.close()
    provider_calls = (
        active_reservation.used - reservation_calls_before
        if active_reservation is not None
        else len(targets)
    )
    results: List[Dict[str, Any]] = []
    for (provider, model), outcome in zip(targets, outcomes):
        if outcome.error is not None:
            provider_result: Dict[str, Any] = {
                "provider": provider,
                "model": model,
                "success": False,
                "text": "",
                "original_chars": 0,
                "text_truncated": False,
                "usage": {},
                "warnings": ["provider_call_exception"],
                "elapsed_ms": outcome.elapsed_ms,
                "error": str(outcome.error),
            }
        else:
            provider_raw = dict(outcome.value or {})
            provider_ok = bool(provider_raw.get("success", not provider_raw.get("error")))
            full_text = str(provider_raw.get("text") or "")
            provider_result = {
                "provider": provider,
                "model": provider_raw.get("model") or model,
                "success": provider_ok,
                "text": full_text[:COMPARE_PARTICIPANT_MAX_CHARS],
                "original_chars": len(full_text),
                "text_truncated": len(full_text) > COMPARE_PARTICIPANT_MAX_CHARS,
                "usage": provider_raw.get("usage") or {},
                "warnings": provider_raw.get("warnings") or [],
                "finish_reason": provider_raw.get("finish_reason"),
                "elapsed_ms": outcome.elapsed_ms,
                "provenance": provider_raw.get("consistency") or {},
            }
            if not provider_ok:
                provider_result["error"] = (
                    provider_raw.get("error")
                    or full_text
                    or "provider returned an unsuccessful response"
                )
            if gate_enabled and provider_ok:
                try:
                    provider_result["decision"] = consistency_gate.parse_decision(full_text, labels)
                except ValueError as exc:
                    provider_result["contract_error"] = str(exc)
        results.append(provider_result)

    ok = sum(bool(item["success"]) for item in results)
    if ok == len(results):
        status = "complete"
        warnings: List[str] = []
    elif ok >= min_successes:
        status = "partial"
        warnings = ["partial_compare_failures"]
    elif ok:
        status = "insufficient"
        warnings = ["partial_compare_failures", "insufficient_compare_responses"]
    else:
        status = "failed"
        warnings = ["compare_provider_failures", "insufficient_compare_responses"]
    warnings.extend(
        str(warning)
        for item in results
        for warning in item.get("warnings") or []
    )
    consistency_report: Dict[str, Any] | None = None
    success = ok >= min_successes
    text = f"Compared {len(results)} provider/model targets ({ok} succeeded)."
    if gate_enabled:
        consistency_report = consistency_gate.evaluate_decisions(
            results,
            threshold=float(gate_config.get("threshold", 1.0)),
            require_all=bool(gate_config.get("require_all", True)),
            min_responses=int(gate_config.get("min_responses", 2)),
        )
        policy_values = [item.get("provenance", {}).get("policy_sha256") for item in results]
        request_values = [item.get("provenance", {}).get("request_sha256") for item in results]
        policy_hashes = {value for value in policy_values if value}
        request_hashes = {value for value in request_values if value}
        provenance_consistent = bool(
            len(policy_values) == len(results)
            and all(policy_values)
            and len(policy_hashes) == 1
            and len(request_values) == len(results)
            and all(request_values)
            and len(request_hashes) == 1
        )
        consistency_report.update(
            {
                "policy_sha256": next(iter(policy_hashes)) if len(policy_hashes) == 1 else None,
                "request_sha256": next(iter(request_hashes)) if len(request_hashes) == 1 else None,
                "provenance_consistent": provenance_consistent,
                "execution": execution,
                "max_concurrency": max_concurrency,
            }
        )
        if not consistency_report["provenance_consistent"]:
            consistency_report["passed"] = False
            consistency_report["human_review"] = True
            consistency_report["decision"] = None
            consistency_report["review_reasons"].append("provenance_mismatch")
        success = success and bool(consistency_report["passed"])
        if success:
            text = (
                f'Consistency Gate passed with decision "{consistency_report["decision"]}" '
                f"({consistency_report['valid_responses']}/{len(results)} valid)."
            )
        else:
            warnings.append("consistency_gate_human_review")
            text = "Consistency Gate requires human review: " + ", ".join(
                consistency_report["review_reasons"]
            )
    raw = {
        "success": success,
        "text": text,
        "schema": "compare_result_v1",
        "status": status,
        "requested": len(results),
        "succeeded": ok,
        "min_successes": min_successes,
        "participants": results,
        "results": results,
        "call_usage": {
            "provider_calls": provider_calls,
            "provider_attempts": len(targets),
        },
        "execution": execution,
        "warnings": list(dict.fromkeys(warnings)),
        **({"consistency": consistency_report} if consistency_report is not None else {}),
    }
    if any(
        str(item.get("error") or "") == orchestrator.PROVIDER_CALL_DEADLINE_ERROR
        for item in results
    ):
        raw["error"] = orchestrator.PROVIDER_CALL_DEADLINE_ERROR
        raw["error_type"] = orchestrator.PROVIDER_CALL_DEADLINE_ERROR
    return envelope("compare_models", raw, provider="multiple")


def _review_diff(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _operation_provider(args, "review_diff", default="gemini")
    cwd = str(args.get("cwd") or args.get("repo") or ".").strip() or "."
    repo = google_diff_review._resolve_repo(cwd)
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        raise ValueError(f"Not a git repository: {repo}")
    staged = bool(args.get("staged"))
    base = str(args.get("base") or args.get("ref") or "").strip()
    paths = args.get("paths")
    path_list = [str(item) for item in paths] if isinstance(paths, list) else None
    include_untracked = bool(args.get("include_untracked", False))
    status = google_diff_review._run_git(repo, ["status", "--short"])
    branch = google_diff_review._run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    diff = google_diff_review._collect_diff(
        repo,
        staged=staged,
        base=base,
        paths=path_list,
        include_untracked=include_untracked,
    )
    if not diff.strip():
        return envelope(
            "review_diff",
            {
                "success": True,
                "text": "No diff to review (working tree matches selected base).",
                "repo": str(repo),
                "branch": branch,
                "diff_chars": 0,
                "status": status,
                "warnings": ["empty_diff"],
            },
            provider="local",
        )
    truncated = len(diff) > google_diff_review.MAX_DIFF_CHARS
    diff = diff[: google_diff_review.MAX_DIFF_CHARS]
    instruction = str(args.get("instruction") or google_diff_review.DEFAULT_REVIEW_PROMPT).strip()
    focus = str(args.get("focus") or "").strip()
    require_complete = bool(args.get("require_complete", False))
    completion_marker = "[AGENT_HUB_REVIEW_COMPLETE]"
    if require_complete:
        instruction += (
            "\nThe entire available diff, including bounded untracked text files, is included below. "
            "Do not request shell or file tools. Return the completed review now. If there are no "
            f"findings, say so explicitly. End the response with {completion_marker}."
        )
    prompt = (
        f"{instruction}\n\n"
        + (f"Focus: {focus}\n\n" if focus else "")
        + f"Repository: {repo}\nBranch: {branch}\n"
        + f"Diff mode: {'staged' if staged else (f'base={base}' if base else 'HEAD')}\n"
        + ("(diff truncated)\n" if truncated else "")
        + f"```diff\n{diff}\n```\n\nGit status:\n```\n{status[:4000]}\n```"
    )
    raw_chat = _chat_raw(
        provider,
        {
            "prompt": prompt,
            "model": args.get("model"),
            "temperature": args.get("temperature", 0.2),
            "max_tokens": args.get("max_tokens"),
            "reasoning_effort": args.get("reasoning_effort"),
            "timeout_sec": (
                args.get("timeout_sec") or limits.MAX_PROVIDER_TIMEOUT_SECONDS
            ),
            "project_root": str(repo),
            "policy_mode": args.get("policy_mode") or "auto",
            "policy_file": args.get("policy_file"),
            "max_policy_chars": args.get("max_policy_chars"),
            "_provider_call_budget": args.get("_provider_call_budget"),
            "_provider_call_reservation": args.get("_provider_call_reservation"),
            "_reasoning_effort_implicit": args.get("_reasoning_effort_implicit"),
        },
    )
    warnings = list(raw_chat.get("warnings") or [])
    text = str(raw_chat.get("text") or "")
    complete = not require_complete or completion_marker in text
    if require_complete and not complete:
        warnings.append("incomplete_review_output")
        raw_chat = {
            **raw_chat,
            "success": False,
            "error": "review response did not include the completion marker",
            "error_type": "incomplete_review_output",
        }
    elif require_complete:
        raw_chat["text"] = text.replace(completion_marker, "").rstrip()
    if truncated:
        warnings.append("diff_truncated")
    return envelope(
        "review_diff",
        {
            **raw_chat,
            "repo": str(repo),
            "branch": branch,
            "diff_chars": len(diff),
            "truncated": truncated,
            "status_preview": status[:2000],
            "warnings": warnings,
        },
        provider=provider,
    )


def _release_snapshot(args: Dict[str, Any]) -> Dict[str, Any]:
    call_args = {
        key: value for key, value in args.items() if key != "provider" and value is not None
    }
    return envelope(
        "release_snapshot", google_release.release_snapshot(call_args), provider="local"
    )


def _release_draft(args: Dict[str, Any]) -> Dict[str, Any]:
    call_args = {
        key: value for key, value in args.items() if key != "provider" and value is not None
    }
    snapshot = google_release.collect_snapshot(call_args)
    draft = google_release.render_draft(snapshot, call_args)
    if not bool(args.get("polish")):
        return envelope(
            "release_draft",
            {
                "success": True,
                "text": draft,
                "draft": draft,
                "snapshot": google_release.snapshot_to_dict(snapshot),
            },
            provider="local",
        )
    provider = _operation_provider(args, "release_draft", default="gemini")
    polished = _chat_raw(
        provider,
        {
            "prompt": (
                "Polish this release draft without inventing facts. Preserve versions, links, "
                f"commands, and validation results.\n\n{draft}"
            ),
            "model": args.get("model"),
            "max_tokens": args.get("max_tokens"),
            "reasoning_effort": args.get("reasoning_effort"),
            "timeout_sec": (
                args.get("timeout_sec") or limits.MAX_PROVIDER_TIMEOUT_SECONDS
            ),
            "project_root": str(args.get("repo") or "."),
            "policy_mode": args.get("policy_mode") or "auto",
            "policy_file": args.get("policy_file"),
            "max_policy_chars": args.get("max_policy_chars"),
            "_provider_call_budget": args.get("_provider_call_budget"),
            "_provider_call_reservation": args.get("_provider_call_reservation"),
            "_reasoning_effort_implicit": args.get("_reasoning_effort_implicit"),
        },
    )
    return envelope(
        "release_draft",
        {
            **polished,
            "draft": draft,
            "snapshot": google_release.snapshot_to_dict(snapshot),
        },
        provider=provider,
    )


def _get_settings(args: Dict[str, Any]) -> Dict[str, Any]:
    requested = str(args.get("provider") or "all")
    providers = _selected_providers(requested)
    values: Dict[str, Any] = {}
    if "claude" in providers:
        values["claude"] = {
            "defaults": {"model": claude_models.DEFAULT_MODEL},
            "overrides": provider_settings.get("claude"),
            "scope": capabilities.provider_capabilities("claude")["settings"]["scope"],
        }
    if "grok" in providers:
        values["grok"] = {
            "defaults": {"model": grok_models.DEFAULT_MODEL, "api_mode": "chat"},
            "overrides": provider_settings.get("grok"),
            "scope": capabilities.provider_capabilities("grok")["settings"]["scope"],
        }
    if "gpt" in providers:
        values["gpt"] = {
            "defaults": {"model": openai_models.DEFAULT_MODEL},
            "overrides": provider_settings.get("gpt"),
            "scope": capabilities.provider_capabilities("gpt")["settings"]["scope"],
            "auth_owner": "official-codex",
        }
    if "gemini" in providers:
        values["gemini"] = {
            "model_preferences": google_model_prefs.get_prefs_tool({}),
            "session": google_session_prefs.get_session_prefs({}),
            "profiles": google_profiles.list_profiles_tool({}),
            "scope": capabilities.provider_capabilities("gemini")["settings"]["scope"],
        }
    raw = {"success": True, "text": "Agent Hub settings loaded.", "providers": values}
    return envelope("get_settings", raw, success=True)


def _known_text_model_ids(provider: str) -> set[str]:
    if provider == "claude":
        items = claude_models.CURATED
    elif provider == "grok":
        items = [
            item
            for item in grok_models.CURATED
            if "imagine-image" not in str(item.get("id") or "")
            and "imagine-video" not in str(item.get("id") or "")
        ]
    elif provider == "gpt":
        items = [{"id": openai_models.DEFAULT_MODEL}]
    else:
        items = google_models.static_model_catalog()
    return {
        str(item.get("id") or "").strip()
        for item in items
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }


def _validate_text_model(provider: str, model: Any) -> str:
    raw = str(model or "").strip()
    model_id = (
        google_model_prefs.normalize_model_id(raw)
        if provider == "gemini"
        else raw
    )
    if not model_id:
        raise ValueError("model is required")
    known = _known_text_model_ids(provider)
    if not known:
        raise ValueError(
            f"{provider} text model catalog is unavailable; "
            "retry or set validate=false explicitly"
        )
    if model_id not in known:
        raise ValueError(
            f"model '{model_id}' is not in the installed {provider} text model catalog; "
            "set validate=false only after verifying it with the provider"
        )
    return model_id


def _update_settings(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _normalize_provider(args.get("provider") or "gemini", allow_auto=True)
    if provider == "auto":
        provider = "gemini"
    validate = bool(args.get("validate", True))
    if provider in {"claude", "grok", "gpt"}:
        requested = {
            key: args.get(key)
            for key in ("model", "temperature", "max_tokens", "api_mode")
            if args.get(key) is not None
        }
        allowed = set(provider_registry.manifest(provider).settings_fields)
        unsupported = sorted(set(requested) - allowed)
        if unsupported:
            raise ValueError(
                f"unsupported {provider} settings: {', '.join(unsupported)}"
            )
        changes = {
            key: value
            for key, value in requested.items()
            if key in allowed
        }
        if "model" in changes and validate:
            changes["model"] = _validate_text_model(provider, changes["model"])
        if not changes:
            raise ValueError(f"provide a supported {provider} setting to update")
        current = provider_settings.update(provider, changes)
        return envelope(
            "update_settings",
            {
                "success": True,
                "text": f"Updated {provider} defaults.",
                "provider_settings": current,
            },
            provider=provider,
        )
    changes: List[Dict[str, Any]] = []
    if args.get("model"):
        model = (
            _validate_text_model("gemini", args["model"])
            if validate
            else args["model"]
        )
        changes.append(
            google_model_prefs.set_model_tool(
                {
                    "model": model,
                    "task": args.get("task"),
                    # The canonical Agent Hub layer already enforced exact
                    # text-catalog membership when validation was requested.
                    "validate": False,
                    "notes": args.get("notes", ""),
                }
            )
        )
    if args.get("transport") or args.get("clear_transport"):
        changes.append(
            google_session_prefs.set_provider_tool(
                {"provider": args.get("transport"), "clear": bool(args.get("clear_transport"))}
            )
        )
    if "profile" in args:
        changes.append(
            google_profiles.use_profile_tool(
                {
                    "name": args.get("profile") or "",
                    "apply_model_pref": bool(args.get("apply_model_pref", True)),
                    "apply_provider": bool(args.get("apply_provider", True)),
                }
            )
        )
    if isinstance(args.get("save_profile"), dict):
        changes.append(google_profiles.save_custom_profile_tool(args["save_profile"]))
    if not changes:
        raise ValueError("provide model, transport, profile, or save_profile to update")
    raw = {
        "success": all(change.get("success", True) for change in changes),
        "text": f"Applied {len(changes)} setting change(s).",
        "changes": changes,
    }
    return envelope("update_settings", raw, provider="gemini")


def _reset_settings(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _normalize_provider(args.get("provider") or "gemini", allow_all=True)
    reset = str(args.get("reset") or "all")
    if provider in {"claude", "grok", "gpt"}:
        if reset == "model":
            remaining = provider_settings.remove(provider, {"model"})
            removed = {
                "provider": provider,
                "removed": True,
                "remaining": remaining,
            }
        elif reset == "all":
            removed = provider_settings.reset(provider)
        else:
            raise ValueError(
                f"reset={reset} is only supported for the gemini provider"
            )
        return envelope(
            "reset_settings",
            {
                "success": True,
                "text": (
                    f"Reset {provider} model."
                    if reset == "model"
                    else f"Reset {provider} settings."
                ),
                **removed,
            },
            provider=provider,
        )
    if provider == "all":
        if reset == "model":
            removed = [
                {
                    "provider": item,
                    "removed": True,
                    "remaining": provider_settings.remove(item, {"model"}),
                }
                for item in ("claude", "grok", "gpt")
            ]
        elif reset == "all":
            removed = [
                provider_settings.reset(item)
                for item in ("claude", "grok", "gpt")
            ]
        else:
            removed = []
    else:
        removed = []
    changes: List[Dict[str, Any]] = []
    if reset in {"all", "model"}:
        changes.append(
            google_model_prefs.clear_prefs_tool(
                {
                    "task": args.get("task"),
                    "all": reset == "all" or bool(args.get("all")),
                    "default_scopes": reset == "model" and not args.get("task"),
                }
            )
        )
    if reset in {"all", "transport"}:
        changes.append(google_session_prefs.clear_provider())
    if reset in {"all", "profile"}:
        changes.append(google_profiles.use_profile_tool({"name": ""}))
    raw = {
        "success": all(change.get("success", True) for change in changes),
        "text": f"Reset {reset} settings.",
        "changes": changes,
        "provider_settings_removed": removed,
    }
    return envelope("reset_settings", raw, provider="multiple" if provider == "all" else "gemini")


def _get_handoff(args: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = handoff_state.load_handoff(
        str(args.get("project_root") or "."),
        mode=str(args.get("mode") or "auto"),
        search=str(args.get("search") or "nearest"),
        file=str(args.get("file") or ""),
        max_chars=int(args.get("max_chars") or handoff_state.DEFAULT_MAX_CHARS),
    )
    return envelope(
        "get_handoff",
        {
            "success": True,
            "text": (
                handoff_state.render_context(snapshot)
                if snapshot.get("loaded")
                else "No project handoff was found."
            ),
            "handoff": handoff_state.public_snapshot(snapshot),
        },
    )


def _prepare_handoff_update(args: Dict[str, Any]) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "body": str(args.get("body") or ""),
        "file": str(args.get("file") or ""),
        "search": str(args.get("search") or "project-only"),
    }
    if "base_managed_sha256" in args:
        kwargs["base_managed_sha256"] = args.get("base_managed_sha256")
    prepared = handoff_state.prepare_handoff_update(
        str(args.get("project_root") or "."),
        **kwargs,
    )
    return envelope(
        "prepare_handoff_update",
        {
            "success": True,
            "text": "Handoff update prepared; no file was changed.",
            **prepared,
        },
    )


def _apply_handoff_update(args: Dict[str, Any]) -> Dict[str, Any]:
    if "expected_sha256" not in args:
        raise ValueError("expected_sha256 is required; use null only for a new file")
    applied = handoff_state.apply_handoff_update(
        str(args.get("project_root") or "."),
        file=str(args.get("file") or ""),
        content=str(args.get("content") or ""),
        expected_sha256=args.get("expected_sha256"),
    )
    return envelope(
        "apply_handoff_update",
        {
            "success": True,
            "text": "HANDOFF.md updated atomically.",
            **applied,
        },
    )


def _workflow_resolution(args: Dict[str, Any]) -> Dict[str, Any]:
    return recipes.resolve_workflow(
        str(args.get("workflow_id") or args.get("recipe_id") or ""),
        str(args.get("preset") or ""),
    )


def _is_adaptive(args: Dict[str, Any]) -> bool:
    return str(args.get("workflow_id") or args.get("recipe_id") or "").strip().lower() in {
        "adaptive",
        "auto",
    }


def _fixed_bindings(args: Dict[str, Any]) -> Dict[str, str] | None:
    explicit = (
        {str(key): str(value) for key, value in args["bindings"].items()}
        if isinstance(args.get("bindings"), dict)
        else {}
    )
    provider = _normalize_provider(args.get("provider") or "auto", allow_auto=True)
    if provider == "auto":
        return explicit or None

    chat_tool = provider_registry.manifest(provider).chat_tool
    generated = {
        "chat": chat_tool,
        "write_ag": (
            "google_antigravity_write" if provider == "gemini" else chat_tool
        ),
    }
    conflicts = sorted(
        key
        for key, value in explicit.items()
        if key in generated and value != generated[key]
    )
    if conflicts:
        raise ValueError(
            "provider conflicts with explicit fixed bindings: "
            + ", ".join(conflicts)
        )
    return {**generated, **explicit}


_ADAPTIVE_RUN_OPTION_KEYS = {
    "project_root",
    "models",
    "max_concurrency",
    "max_leaf_calls",
    "per_call_timeout",
    "workflow_timeout",
    "max_tokens",
    "policy_mode",
    "policy_file",
    "max_policy_chars",
    "handoff_mode",
    "handoff_search",
    "handoff_file",
    "handoff_drift_policy",
    "max_handoff_chars",
}


def _adaptive_workflow_timeout(value: Any) -> float:
    requested = float(value or ADAPTIVE_WORKFLOW_TIMEOUT_DEFAULT)
    return max(
        ADAPTIVE_WORKFLOW_TIMEOUT_MIN,
        min(requested, ADAPTIVE_WORKFLOW_TIMEOUT_MAX),
    )


def _adaptive_run_options(
    args: Dict[str, Any], existing: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    options = dict(existing or {})
    resuming = existing is not None
    options.update(
        {
            key: value
            for key, value in args.items()
            if key in _ADAPTIVE_RUN_OPTION_KEYS
            and value is not None
            and not (resuming and key == "models")
        }
    )
    if resuming:
        persisted_models = options.get("models")
        complete_snapshot = bool(
            isinstance(persisted_models, dict)
            and all(
                str(persisted_models.get(provider) or "").strip()
                for provider in PROVIDERS
            )
        )
        if not complete_snapshot:
            options["models"] = _snapshot_provider_models(persisted_models)
    options["project_root"] = str(gather.validate_project_root(options.get("project_root") or "."))
    options["workflow_timeout"] = _adaptive_workflow_timeout(options.get("workflow_timeout"))
    options["per_call_timeout"] = max(
        5.0,
        min(
            float(options.get("per_call_timeout") or ADAPTIVE_PER_CALL_TIMEOUT_DEFAULT),
            ADAPTIVE_PER_CALL_TIMEOUT_MAX,
        ),
    )
    options["max_concurrency"] = max(
        1,
        min(int(options.get("max_concurrency") or len(PROVIDERS)), len(PROVIDERS)),
    )
    options["max_leaf_calls"] = max(
        1,
        min(
            int(options.get("max_leaf_calls") or broker.DEFAULT_MAX_LEAF_CALLS),
            100,
        ),
    )
    drift_policy = str(options.get("handoff_drift_policy") or "pause").strip().lower()
    if drift_policy not in {"pause", "use-snapshot"}:
        raise ValueError("handoff_drift_policy must be pause or use-snapshot")
    options["handoff_drift_policy"] = drift_policy
    return options


def _load_workflow_handoff(args: Dict[str, Any]) -> Dict[str, Any]:
    return handoff_state.load_handoff(
        str(args.get("project_root") or "."),
        mode=str(args.get("handoff_mode") or "auto"),
        search=str(args.get("handoff_search") or "nearest"),
        file=str(args.get("handoff_file") or ""),
        max_chars=int(args.get("max_handoff_chars") or handoff_state.DEFAULT_MAX_CHARS),
    )


def _adaptive_public_state(state: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(state)
    store.validate_run_status(out)
    lease = out.pop("_lease", None)
    out["event_journal"] = run_events.event_summary(out)
    out.pop("events", None)
    out.pop("event_seq", None)
    out.pop("events_dropped", None)
    handoff_snapshot = out.pop("_handoff_snapshot", None)
    if isinstance(handoff_snapshot, dict):
        out["handoff"] = handoff_state.public_snapshot(handoff_snapshot)
    try:
        lease_expires_at = float(lease.get("expires_at") or 0) if isinstance(lease, dict) else 0.0
    except (TypeError, ValueError):
        lease_expires_at = 0.0
    out["lease_active"] = lease_expires_at > time.time()
    out["continuation_status"] = "running" if out["lease_active"] else "idle"
    plan = out.get("plan") if isinstance(out.get("plan"), dict) else {}
    completed = set((out.get("results") or {}).keys())
    out["pending_steps"] = [
        str(step.get("id"))
        for step in plan.get("steps") or []
        if str(step.get("id")) not in completed
    ]
    out["done"] = out.get("status") in run_lifecycle.TERMINAL_STATUSES
    if out["done"]:
        out["next_action"] = {
            "type": "done" if out.get("status") == "completed" else "failed",
            "message": out.get("error")
            or f"Adaptive workflow is {out.get('status')}.",
        }
    elif out["lease_active"]:
        out["next_action"] = {
            "type": "call_tool",
            "tool": "agent_hub_get_run",
            "arguments": {"run_id": str(out.get("run_id") or "")},
        }
    else:
        out["next_action"] = {
            "type": "call_tool",
            "tool": "agent_hub_continue_workflow",
            "arguments": {
                "run_id": str(out.get("run_id") or ""),
                "expected_revision": int(out.get("store_revision") or 0),
            },
        }
    return out


def _save_adaptive_state(state: Dict[str, Any]) -> Dict[str, Any]:
    return _adaptive_public_state(store.create(state))


def _new_adaptive_state(
    plan: Dict[str, Any],
    args: Dict[str, Any],
    handoff_snapshot: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    snapshot = (
        deepcopy(handoff_snapshot)
        if isinstance(handoff_snapshot, dict)
        else _load_workflow_handoff(args)
    )
    options = _adaptive_run_options(args)
    options["models"] = _snapshot_provider_models(options.get("models"))
    created_at = time.time()
    state = {
        "run_id": uuid.uuid4().hex[:12],
        "workflow_id": "adaptive",
        "run_kind": "adaptive",
        "mode": "supervised",
        "state_schema_version": ADAPTIVE_STATE_SCHEMA_VERSION,
        "call_accounting_version": ADAPTIVE_CALL_ACCOUNTING_VERSION,
        "call_accounting_unit": "provider_adapter_invocation",
        "store_revision": 0,
        "created_at": created_at,
        "updated_at": created_at,
        "status": "paused",
        "plan": plan,
        "options": options,
        "project_root": options["project_root"],
        "_handoff_snapshot": snapshot,
        "results": {},
        "waves": [],
        "leaf_calls": 0,
        "planner_calls": int(
            (plan.get("planner") or {}).get("attempts", 0)
            if isinstance(plan.get("planner"), dict)
            else 0
        ),
    }
    run_events.append_event(
        state,
        "run_created",
        at=created_at,
        base_revision=None,
        resulting_revision=0,
        run_kind="adaptive",
        workflow_id="adaptive",
        status="paused",
        **run_events.text_identity(
            args.get("prompt") or args.get("instruction") or "",
            prefix="prompt",
        ),
    )
    return state


def _adaptive_plan(
    args: Dict[str, Any],
    handoff_snapshot: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    supplied = args.get("plan")
    max_steps = int(args.get("max_steps") or orchestrator.MAX_PLAN_STEPS)
    max_calls = int(args.get("max_leaf_calls") or broker.DEFAULT_MAX_LEAF_CALLS)
    root = str(gather.validate_project_root(args.get("project_root") or "."))
    policy_mode = str(args.get("policy_mode") or "required")
    policy_file = str(args.get("policy_file") or "")
    max_policy_chars = int(
        args.get("max_policy_chars") or consistency_gate.DEFAULT_MAX_POLICY_CHARS
    )
    policy = consistency_gate.load_policy(
        project_root=root,
        policy_file=policy_file,
        required=policy_mode == "required",
        max_chars=max_policy_chars,
    )
    snapshot = (
        handoff_snapshot if isinstance(handoff_snapshot, dict) else _load_workflow_handoff(args)
    )
    if isinstance(supplied, dict):
        raw_supplied = {key: supplied.get(key) for key in ("schema", "goal", "rationale", "steps")}
        plan = orchestrator.validate_plan(raw_supplied, max_steps=max_steps, max_calls=max_calls)
        return {
            **plan,
            "planner": {
                "provider": "caller",
                "model": None,
                "attempts": 0,
                "policy_source": policy.get("source"),
                "policy_sha256": policy.get("sha256"),
            },
        }

    goal = str(args.get("prompt") or args.get("instruction") or "").strip()
    if not goal:
        raise ValueError("adaptive workflow requires prompt or instruction")
    planner_provider = _normalize_provider(args.get("planner_provider") or "gemini")
    facts_text = ""
    try:
        fact_pack = gather.gather_durable_facts(root)
        facts_text = str(fact_pack.get("text") or "")
    except (ValueError, OSError):
        facts_text = ""
    initial_prompt = orchestrator.planner_prompt(goal, facts=facts_text, max_steps=max_steps)
    operational_handoff = handoff_state.render_context(snapshot)
    if operational_handoff:
        initial_prompt = f"{initial_prompt}\n\n{operational_handoff}"
    repairs = max(
        0,
        min(
            int(args.get("planner_repair_attempts", limits.MAX_PLANNER_REPAIRS)),
            limits.MAX_PLANNER_REPAIRS,
        ),
    )
    attempts: List[Dict[str, Any]] = []
    previous_text = ""
    validation_error = ""
    planner_timeout = min(
        float(args.get("per_call_timeout") or ADAPTIVE_PER_CALL_TIMEOUT_DEFAULT),
        max(
            ADAPTIVE_TIMEOUT_RETURN_MARGIN,
            _adaptive_workflow_timeout(args.get("workflow_timeout"))
            - ADAPTIVE_TIMEOUT_RETURN_MARGIN,
        ),
    )
    for attempt in range(repairs + 1):
        planner_input = initial_prompt
        if attempt:
            planner_input += (
                "\n\nYour previous JSON plan was rejected by the local validator. "
                f"Error: {validation_error}. Return a corrected JSON object only.\n"
                f"Rejected plan:\n{previous_text[:12000]}"
            )
        response = _chat_raw(
            planner_provider,
            {
                "prompt": planner_input,
                "model": args.get("planner_model"),
                "temperature": 0.1,
                "max_tokens": int(
                    args.get("planner_max_tokens") or limits.MAX_OUTPUT_TOKENS
                ),
                "timeout_sec": int(planner_timeout),
                "project_root": root,
                "policy_mode": policy_mode,
                "policy_file": policy_file or None,
                "max_policy_chars": max_policy_chars,
            },
        )
        previous_text = str(response.get("text") or "")
        if not bool(response.get("success", not response.get("error"))):
            validation_error = str(
                response.get("error") or previous_text or "planner provider failed"
            )
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "success": False,
                    "error": validation_error,
                    "finish_reason": response.get("finish_reason"),
                }
            )
            continue
        try:
            parsed = orchestrator.parse_plan(previous_text)
            # The planner chooses the DAG, but it may not rewrite or summarize the caller's goal.
            # Keeping the original goal in the validated plan also binds it to plan_sha256.
            parsed["goal"] = goal
            plan = orchestrator.validate_plan(parsed, max_steps=max_steps, max_calls=max_calls)
        except ValueError as exc:
            validation_error = str(exc)
            attempts.append({"attempt": attempt + 1, "success": False, "error": validation_error})
            continue
        attempts.append({"attempt": attempt + 1, "success": True})
        provenance = (
            response.get("consistency") if isinstance(response.get("consistency"), dict) else {}
        )
        return {
            **plan,
            "planner": {
                "provider": planner_provider,
                "model": response.get("model") or args.get("planner_model"),
                "attempts": len(attempts),
                "attempt_log": attempts,
                "policy_source": provenance.get("policy_source") or policy.get("source"),
                "policy_sha256": provenance.get("policy_sha256") or policy.get("sha256"),
                "request_sha256": provenance.get("request_sha256"),
            },
        }
    raise ValueError(f"adaptive planner failed validation: {validation_error}")


def _render_compare_dependency(result: Dict[str, Any]) -> str:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    participants = data.get("participants")
    if not isinstance(participants, list):
        participants = data.get("results")
    lines = [
        (
            "Compare result "
            f"(status={data.get('status') or 'unknown'}, "
            f"succeeded={data.get('succeeded', '?')}/{data.get('requested', '?')}, "
            f"minimum={data.get('min_successes', '?')})"
        )
    ]
    for item in participants or []:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "unknown")
        model = str(item.get("model") or "default")
        lines.append(f"Participant — {provider} / {model}")
        if item.get("success"):
            lines.append(str(item.get("text") or "[empty response]"))
            if item.get("text_truncated"):
                lines.append(
                    f"[participant response truncated from {item.get('original_chars')} chars]"
                )
        else:
            lines.append(f"ERROR: {item.get('error') or 'provider call failed'}")
    return "\n".join(lines)


def _render_dependency_outputs(
    dependencies: Dict[str, Dict[str, Any]],
    *,
    max_chars: int = ADAPTIVE_DEPENDENCY_CONTEXT_MAX_CHARS,
) -> str:
    chunks: List[str] = []
    for step_id, result in dependencies.items():
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        if data.get("schema") == "compare_result_v1":
            body = _render_compare_dependency(result)
        else:
            body = str(result.get("text") or "")
        bounded = body[:ADAPTIVE_DEPENDENCY_ITEM_MAX_CHARS]
        if len(body) > ADAPTIVE_DEPENDENCY_ITEM_MAX_CHARS:
            bounded += "\n[dependency output truncated]"
        chunks.append(f"Dependency output — {step_id}:\n{bounded}")
    rendered = "\n\n".join(chunks)
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "\n[dependency context truncated]"
    return rendered


def _adaptive_context(
    goal: str, step: Dict[str, Any], dependencies: Dict[str, Dict[str, Any]]
) -> str:
    parts = [
        f"Overall goal:\n{goal}",
        f"Current step:\n{step['instruction']}",
        (
            "Execution contract:\nComplete this step in the current response using only the "
            "provided goal and dependency outputs. Return the finished analysis or artifact now; "
            "do not announce, request, or defer future file or tool inspection."
        ),
    ]
    if step.get("investigation_depth"):
        parts.append(f"Required investigation depth: {step['investigation_depth']}")
    parts.append(f"Reasoning effort selected by planner: {step.get('reasoning_effort', 'medium')}")
    if dependencies:
        parts.append(_render_dependency_outputs(dependencies))
    return "\n\n".join(parts)


def _adaptive_step_call(
    step: Dict[str, Any],
    provider: str,
    dependencies: Dict[str, Dict[str, Any]],
    *,
    args: Dict[str, Any],
    goal: str,
) -> Dict[str, Any]:
    root = str(args.get("project_root") or ".")
    model_map = args.get("models") if isinstance(args.get("models"), dict) else {}
    selected_model = str(model_map.get(provider) or "").strip() or None
    policy_args = {
        "policy_mode": args.get("policy_mode") or "required",
        "policy_file": args.get("policy_file"),
        "max_policy_chars": args.get("max_policy_chars"),
    }
    capability = step["capability"]
    context = _adaptive_context(goal, step, dependencies)
    operational_handoff = handoff_state.render_context(args.get("_handoff_snapshot"))
    if operational_handoff and capability not in {
        "search",
        "verify",
        "release_snapshot",
    }:
        context = f"{context}\n\n{operational_handoff}"
    common = {
        "provider": provider,
        "model": selected_model,
        "max_tokens": args.get("max_tokens"),
        "reasoning_effort": step.get("reasoning_effort") or "medium",
        "timeout_sec": args.get("per_call_timeout") or ADAPTIVE_PER_CALL_TIMEOUT_DEFAULT,
        "project_root": root,
        "_provider_call_budget": args.get("_provider_call_budget"),
        "_provider_call_reservation": step.get("_provider_call_reservation"),
        "_reasoning_effort_implicit": bool(
            args.get("_reasoning_effort_implicit", False)
        ),
        **policy_args,
    }
    if capability == "chat":
        return _chat({**common, "prompt": context})
    if capability == "review_text":
        return _chat({**common, "prompt": context})
    if capability == "inspect_codebase":
        depth = str(step.get("investigation_depth") or "standard")
        code_context = gather.gather_code_context(
            root,
            depth=depth,
            focus=f"{goal}\n{step.get('instruction') or ''}",
        )
        durable_facts = gather.gather_durable_facts(root)
        evidence = (
            f"{context}\n\n"
            "Repository evidence follows. Use only this evidence for repository claims. "
            "Cite exact file:line ranges from the numbered excerpts for important claims. "
            "A complete marker means the whole file is present; never infer from unshown portions of "
            "a partial file. State what the bounded context could not prove.\n\n"
            f"{durable_facts.get('text') or ''}\n\n{code_context.get('text') or ''}"
        )
        result = _chat({**common, "prompt": evidence})
        result.setdefault("data", {}).setdefault("inspection", {})
        result["data"]["inspection"].update(
            {
                "depth": depth,
                "file_count": code_context.get("file_count"),
                "candidate_count": code_context.get("candidate_count"),
                "files": code_context.get("files"),
                "complete_files": code_context.get("complete_files"),
                "partial_files": code_context.get("partial_files"),
                "evidence_segments": code_context.get("evidence_segments"),
                "focus_applied": code_context.get("focus_applied"),
                "candidate_limit": code_context.get("candidate_limit"),
                "candidate_truncated": code_context.get("candidate_truncated"),
                "focus_scan_truncated": code_context.get("focus_scan_truncated"),
                "read_bytes": code_context.get("read_bytes"),
                "read_byte_limit": code_context.get("read_byte_limit"),
                "focus_scan_byte_limit": code_context.get("focus_scan_byte_limit"),
                "skipped_file_counts": code_context.get("skipped_file_counts"),
                "source_truncated_files": code_context.get("source_truncated_files"),
                "text_chars": code_context.get("text_chars"),
                "text_char_limit": code_context.get("text_char_limit"),
                "text_truncated": code_context.get("text_truncated"),
                "git_output_truncated": (
                    code_context.get("git", {}).get("output_truncated")
                    if isinstance(code_context.get("git"), dict)
                    else None
                ),
                "durable_read_bytes": durable_facts.get("durable_read_bytes"),
                "durable_read_byte_limit": durable_facts.get("durable_read_byte_limit"),
                "durable_text_truncated": durable_facts.get("text_truncated"),
            }
        )
        return result
    if capability == "search":
        searched = _search(
            {
                "provider": provider,
                "model": selected_model,
                "query": context,
                "max_tokens": args.get("max_tokens"),
                "timeout_sec": (
                    args.get("per_call_timeout") or ADAPTIVE_PER_CALL_TIMEOUT_DEFAULT
                ),
                "_provider_call_budget": args.get("_provider_call_budget"),
                "_provider_call_reservation": step.get("_provider_call_reservation"),
            }
        )
        searched["warnings"].append("reasoning_effort_not_configurable_for_search")
        searched["data"].setdefault("warnings", []).append(
            "reasoning_effort_not_configurable_for_search"
        )
        return searched
    if capability == "write":
        source = _render_dependency_outputs(dependencies)
        return _write(
            {
                **common,
                # Let the durable writer infer README / technical-doc from the
                # reviewed goal. Forcing custom here silently downgraded a
                # README to transform-class verification.
                "task": "auto",
                "instruction": context,
                "source_text": source or None,
                "quality_rewrite_attempts": step.get("quality_rewrite_attempts", 2),
            }
        )
    if capability == "review_diff":
        reviewed = _review_diff(
            {
                **common,
                "cwd": root,
                "instruction": step["instruction"],
                "focus": context if dependencies else None,
                "require_complete": True,
                "include_untracked": True,
            }
        )
        if "empty_diff" in reviewed.get("warnings", []):
            reviewed["success"] = False
            reviewed["error"] = "adaptive_review_diff_empty"
            reviewed["error_type"] = "adaptive_review_diff_empty"
            data = reviewed.get("data")
            if isinstance(data, dict):
                data["success"] = False
                data["error"] = "adaptive_review_diff_empty"
                data["error_type"] = "adaptive_review_diff_empty"
        return reviewed
    if capability == "compare":
        participants = step.get("participants") or list(DEFAULT_COMPARE_PROVIDERS)
        participant_models = [
            str(model_map.get(participant) or "").strip() for participant in participants
        ]
        gate = None
        if step.get("decision_labels"):
            gate = {
                "enabled": True,
                "decision_labels": step["decision_labels"],
                "project_root": root,
                "policy_mode": policy_args["policy_mode"],
                "policy_file": policy_args["policy_file"],
                "max_policy_chars": policy_args["max_policy_chars"],
            }
        return _compare_models(
            {
                "prompt": context,
                "providers": participants,
                "models": participant_models if any(participant_models) else None,
                "project_root": root,
                "execution": "parallel",
                "max_concurrency": args.get("max_concurrency") or len(PROVIDERS),
                "min_successes": step.get("min_successes", min(2, len(participants))),
                "consistency": gate,
                "max_tokens": args.get("max_tokens"),
                "reasoning_effort": step.get("reasoning_effort") or "medium",
                "timeout_sec": (
                    args.get("per_call_timeout") or ADAPTIVE_PER_CALL_TIMEOUT_DEFAULT
                ),
                "_provider_call_budget": args.get("_provider_call_budget"),
                "_provider_call_reservation": step.get("_provider_call_reservation"),
                "_reasoning_effort_implicit": bool(
                    args.get("_reasoning_effort_implicit", False)
                ),
                **policy_args,
            }
        )
    if capability == "verify":
        verified = _verify({"text": context, "doc_class": "durable", "project_root": root})
        if not bool(verified.get("data", {}).get("ok")):
            verified["success"] = False
            verified["error"] = {
                "type": "verification_failed",
                "message": verified.get("text") or "verification failed",
            }
        return verified
    if capability == "release_snapshot":
        return _release_snapshot({"repo": root})
    if capability == "release_draft":
        return _release_draft(
            {
                **common,
                "repo": root,
                "polish": True,
                "title": step["instruction"],
            }
        )
    raise ValueError(f"unsupported adaptive capability: {capability}")


def _list_workflows(_args: Dict[str, Any]) -> Dict[str, Any]:
    workflows = recipes.list_workflows()
    workflows.append(
        {
            "id": "adaptive",
            "description": (
                "A planner LLM creates a validated provider/dependency DAG; every ready frontier "
                "runs concurrently and failures use declared fallbacks before failing closed."
            ),
            "default_preset": "llm-planned",
            "presets": ["llm-planned"],
            "recipe_ids": {},
            "dynamic": True,
            "capabilities": orchestrator.capability_manifest(),
        }
    )
    return envelope(
        "list_workflows",
        {"success": True, "text": f"{len(workflows)} workflow templates.", "workflows": workflows},
        success=True,
    )


def _get_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
    if _is_adaptive(args):
        return envelope(
            "get_workflow",
            {
                "success": True,
                "text": "Adaptive workflow uses an LLM-planned, locally validated DAG.",
                "workflow_id": "adaptive",
                "dynamic": True,
                "schema": orchestrator.PLAN_SCHEMA,
                "capabilities": orchestrator.capability_manifest(),
                "execution": "dependency-ready frontiers run concurrently",
                "safety": [
                    "strict capability/provider allowlist",
                    "cycle and orphan rejection",
                    "step and call budgets",
                    "single final sink",
                    "fallback then fail-closed",
                    "canonical policy provenance",
                ],
            },
        )
    resolved = recipes.explain_workflow(
        str(args.get("workflow_id") or ""), str(args.get("preset") or "")
    )
    return envelope("get_workflow", {"success": True, "text": "Workflow resolved.", **resolved})


def _plan_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
    if _is_adaptive(args):
        handoff_snapshot = _load_workflow_handoff(args)
        plan = _adaptive_plan(args, handoff_snapshot)
        return envelope(
            "plan_workflow",
            {
                "success": True,
                "text": f"Adaptive plan ready with {len(plan['steps'])} LLM-chosen steps.",
                "workflow_id": "adaptive",
                "dynamic": True,
                "plan": plan,
                "handoff": handoff_state.public_snapshot(handoff_snapshot),
            },
        )
    resolved = _workflow_resolution(args)
    bindings = _fixed_bindings(args)
    plan_args = {
        k: v for k, v in args.items() if k not in {"workflow_id", "recipe_id", "preset", "bindings"}
    }
    plan_args["_provider_models"] = _effective_provider_models()
    planned = recipes.plan_recipe(resolved["recipe_id"], args=plan_args, bindings=bindings)
    return envelope(
        "plan_workflow",
        {"success": True, "text": "Workflow plan ready.", **resolved, "plan": planned},
    )


def _start_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
    if _is_adaptive(args):
        handoff_snapshot = _load_workflow_handoff(args)
        plan = _adaptive_plan(args, handoff_snapshot)
        state = _save_adaptive_state(_new_adaptive_state(plan, args, handoff_snapshot))
        return envelope(
            "start_workflow",
            {
                "success": True,
                "text": (
                    "Adaptive run persisted. Continue it one dependency-ready wave at a time."
                ),
                "workflow_id": "adaptive",
                "dynamic": True,
                **state,
            },
        )
    resolved = _workflow_resolution(args)
    bindings = _fixed_bindings(args)
    run_args = {
        k: v
        for k, v in args.items()
        if k not in {"workflow_id", "recipe_id", "preset", "bindings", "auto_local"}
    }
    run_args["_provider_models"] = _effective_provider_models()
    state = runner.start_run(
        resolved["recipe_id"],
        args=run_args,
        bindings=bindings,
        project_root=str(args.get("project_root") or "."),
        auto_local=bool(args.get("auto_local", True)),
    )
    return envelope(
        "start_workflow", {"success": True, "text": "Workflow started.", **resolved, **state}
    )


def _continue_adaptive_workflow(
    args: Dict[str, Any],
    state: Dict[str, Any],
    claim: store.RunClaim,
) -> Dict[str, Any]:
    store.validate_run_status(state)
    if state.get("status") in run_lifecycle.TERMINAL_STATUSES:
        public = _adaptive_public_state(store.abort_claim(claim))
        return envelope(
            "continue_workflow",
            {
                "success": state.get("status") == "completed",
                "text": (
                    "Adaptive workflow is already complete."
                    if state.get("status") == "completed"
                    else str(
                        state.get("error")
                        or f"Adaptive workflow is {state.get('status')}."
                    )
                ),
                **public,
            },
        )
    if state.get("call_accounting_version") != ADAPTIVE_CALL_ACCOUNTING_VERSION:
        public = _adaptive_public_state(store.abort_claim(claim))
        return envelope(
            "continue_workflow",
            {
                "success": False,
                "text": (
                    "This adaptive run uses legacy call accounting and cannot be resumed "
                    "without risking an under-counted provider budget."
                ),
                "error": {
                    "type": "legacy_call_accounting",
                    "message": "Restart the adaptive workflow with state schema v2.",
                    "retryable": False,
                },
                "pause_reason": "call_accounting_upgrade_required",
                **public,
            },
        )

    options = _adaptive_run_options(args, state.get("options") or {})
    handoff_snapshot = state.get("_handoff_snapshot")
    handoff_drift = handoff_state.check_drift(
        handoff_snapshot if isinstance(handoff_snapshot, dict) else None
    )
    if handoff_drift.get("drifted") and (options["handoff_drift_policy"] == "pause"):
        released = store.abort_claim(claim)
        public = _adaptive_public_state(released)
        public["pause_reason"] = "handoff_drift"
        public["handoff_drift"] = handoff_drift
        return envelope(
            "continue_workflow",
            {
                "success": False,
                "text": (
                    "HANDOFF.md changed after this run was planned. "
                    "Review it, restore it, or continue with "
                    "handoff_drift_policy=use-snapshot."
                ),
                "error": {
                    "type": "handoff_drift",
                    "message": "The persisted handoff snapshot no longer matches disk.",
                    "retryable": True,
                },
                **public,
            },
        )
    if handoff_drift.get("drifted"):
        state["handoff_drift"] = handoff_drift
        warnings = state.setdefault("warnings", [])
        if "handoff_drift_using_snapshot" not in warnings:
            warnings.append("handoff_drift_using_snapshot")
    else:
        state.pop("handoff_drift", None)
    workflow_timeout = float(options["workflow_timeout"])
    requested_per_call = float(options["per_call_timeout"])
    max_waves = max(
        1,
        min(
            int(
                args.get("max_waves_per_call")
                or limits.MAX_WAVES_PER_CALL
            ),
            ADAPTIVE_MAX_WAVES_PER_CALL,
        ),
    )
    started = time.monotonic()
    plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
    completed_before = list(
        (state.get("results") or {}).keys()
        if isinstance(state.get("results"), dict)
        else ()
    )
    executable = {key: plan.get(key) for key in ("schema", "goal", "rationale", "steps")}
    provider_budget = orchestrator.ProviderCallBudget(
        int(options["max_leaf_calls"]),
        used=int(state.get("leaf_calls") or 0),
        max_concurrency=int(options["max_concurrency"]),
        deadline_monotonic=(started + workflow_timeout - ADAPTIVE_TIMEOUT_RETURN_MARGIN),
    )

    def invoke_with_budget(
        step: Dict[str, Any], provider: str, dependencies: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Any]:
        remaining = workflow_timeout - (time.monotonic() - started)
        if remaining < ADAPTIVE_TIMEOUT_RETURN_MARGIN:
            return {
                "success": False,
                "error": "workflow_timeout_exceeded",
                "text": "Adaptive workflow slice exhausted its time budget.",
            }
        step_args = {
            **options,
            "per_call_timeout": min(requested_per_call, remaining),
            "_provider_call_budget": provider_budget,
            "_handoff_snapshot": state.get("_handoff_snapshot"),
            "_reasoning_effort_implicit": (
                str((plan.get("planner") or {}).get("provider") or "") != "caller"
            ),
        }
        return _adaptive_step_call(
            dict(step),
            provider,
            dict(dependencies),
            args=step_args,
            goal=str(plan.get("goal") or ""),
        )

    result = orchestrator.execute_plan(
        executable,
        invoke=invoke_with_budget,
        max_concurrency=int(options["max_concurrency"]),
        max_calls=int(options["max_leaf_calls"]),
        max_elapsed_seconds=max(
            ADAPTIVE_TIMEOUT_RETURN_MARGIN,
            workflow_timeout - (time.monotonic() - started),
        ),
        initial_results=state.get("results") or {},
        initial_waves=state.get("waves") or [],
        initial_call_count=int(state.get("leaf_calls") or 0),
        max_waves=max_waves,
        call_budget=provider_budget,
    )
    successful_results = {
        step_id: item
        for step_id, item in (result.get("results") or {}).items()
        if isinstance(item, dict) and item.get("success")
    }
    result_status = str(result.get("status") or "failed")
    failure_type: str | None = None
    if result_status in {"timed_out", "budget_exhausted"}:
        persisted_status = "paused"
        pause_reason = str(result.get("error") or result_status)
    elif result_status == "blocked":
        persisted_status = "failed"
        pause_reason = None
        failure_type = "adaptive_blocked"
    elif result_status in {"completed", "failed", "paused"}:
        persisted_status = result_status
        pause_reason = "wave_limit" if result_status == "paused" else None
        failure_type = result_status if result_status == "failed" else None
    else:
        persisted_status = "failed"
        pause_reason = None
        failure_type = "adaptive_invalid_status"
    state.update(
        {
            "status": persisted_status,
            "options": options,
            "results": successful_results,
            "waves": list(result.get("waves") or []),
            "leaf_calls": int(result.get("leaf_calls") or 0),
            "updated_at": time.time(),
            "elapsed_ms_last_call": round((time.monotonic() - started) * 1000),
        }
    )
    if pause_reason:
        state["pause_reason"] = pause_reason
    else:
        state.pop("pause_reason", None)
    if persisted_status == "completed":
        state["text"] = str(result.get("text") or "")
        state.pop("error", None)
    elif persisted_status == "failed":
        state["error"] = str(
            result.get("error") or failure_type or "adaptive_step_failed"
        )
    else:
        state.pop("text", None)
        state.pop("error", None)
    committed_steps = run_events.completed_step_ids(
        completed_before,
        successful_results.keys(),
    )
    event_type = {
        "completed": "workflow_completed",
        "failed": "workflow_failed",
    }.get(persisted_status, "wave_committed")
    run_events.append_event(
        state,
        event_type,
        at=float(state["updated_at"]),
        base_revision=claim.base_revision,
        resulting_revision=claim.base_revision + 1,
        run_kind="adaptive",
        workflow_id="adaptive",
        status=persisted_status,
        success=persisted_status != "failed",
        error_type=(
            failure_type if persisted_status == "failed" else None
        ),
        retryable=persisted_status == "paused",
        elapsed_ms=int(state["elapsed_ms_last_call"]),
        wave_index=len(state["waves"]),
        leaf_calls=int(state["leaf_calls"]),
        pending_steps=max(0, len(plan.get("steps") or []) - len(successful_results)),
        pause_reason=pause_reason,
        completed_step_ids=committed_steps,
        **run_events.text_identity(
            state.get("text") or "",
            prefix="result",
        ),
    )
    public = _adaptive_public_state(store.commit_claim(claim, state))
    return envelope(
        "continue_workflow",
        {
            "success": persisted_status != "failed",
            "text": (
                str(state.get("text") or "")
                if persisted_status == "completed"
                else (
                    "Adaptive workflow paused safely; call continue for the next wave."
                    if persisted_status == "paused"
                    else str(state.get("error") or "Adaptive workflow failed.")
                )
            ),
            **public,
        },
    )


def _run_store_error_response(
    run_id: str,
    error: store.RunStoreError,
    *,
    operation: str = "continue_workflow",
) -> Dict[str, Any]:
    if isinstance(error, store.RunLeaseActive):
        error_type = "run_lease_active"
        retryable = True
        revision = error.current_revision
        details = {"retry_after_seconds": round(error.retry_after_seconds, 3)}
    elif isinstance(error, store.RunRevisionConflict):
        error_type = "run_revision_conflict"
        retryable = True
        revision = error.current
        details = {"expected": error.expected, "current": error.current}
    elif isinstance(error, store.RunLeaseLost):
        error_type = "run_lease_lost"
        retryable = True
        revision = None
        details = {}
    elif isinstance(error, store.RunStateDigestConflict):
        error_type = "run_state_digest_conflict"
        retryable = True
        revision = None
        details = {}
    elif isinstance(error, store.RunNotFound):
        error_type = "run_not_found"
        retryable = False
        revision = None
        details = {}
    else:
        error_type = "run_persistence_error"
        retryable = False
        revision = None
        details = {}
    raw_error = {
        "type": error_type,
        "message": str(error),
        "retryable": retryable,
        **details,
    }
    return envelope(
        operation,
        {
            "success": False,
            "text": str(error),
            "error": raw_error,
            "run_id": run_id,
            **({"store_revision": revision} if revision is not None else {}),
        },
    )


def _commit_background_failure(claim: store.RunClaim) -> None:
    """Persist a redacted, resumable failure when a detached continuation crashes."""

    state = deepcopy(claim.state)
    state["status"] = "paused"
    state["pause_reason"] = "background_worker_failed"
    state["error"] = "background_worker_failed"
    state["updated_at"] = time.time()
    warnings = state.setdefault("warnings", [])
    if "background_worker_failed" not in warnings:
        warnings.append("background_worker_failed")
    plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
    results = state.get("results") if isinstance(state.get("results"), dict) else {}
    run_events.append_event(
        state,
        "workflow_paused",
        at=float(state["updated_at"]),
        base_revision=claim.base_revision,
        resulting_revision=claim.base_revision + 1,
        run_kind="adaptive",
        workflow_id="adaptive",
        status="paused",
        success=False,
        error_type="background_worker_failed",
        retryable=True,
        elapsed_ms=0,
        wave_index=len(state.get("waves") or []),
        leaf_calls=int(state.get("leaf_calls") or 0),
        pending_steps=max(0, len(plan.get("steps") or []) - len(results)),
        pause_reason="background_worker_failed",
    )
    try:
        store.commit_claim(claim, state)
    except store.RunStoreError:
        try:
            store.abort_claim(claim)
        except store.RunStoreError:
            pass


def _run_background_adaptive_continue(args: Dict[str, Any], claim: store.RunClaim) -> None:
    try:
        _continue_adaptive_workflow(args, claim.state, claim)
    except Exception:  # noqa: BLE001
        _commit_background_failure(claim)
    finally:
        with _BACKGROUND_CONTINUE_LOCK:
            _BACKGROUND_CONTINUE_THREADS.pop(claim.run_id, None)
        _BACKGROUND_CONTINUE_SLOTS.release()


def _start_background_adaptive_continue(
    args: Dict[str, Any],
    claim: store.RunClaim,
) -> Dict[str, Any]:
    if not _BACKGROUND_CONTINUE_SLOTS.acquire(blocking=False):
        released = _adaptive_public_state(store.abort_claim(claim))
        return envelope(
            "continue_workflow",
            {
                "success": False,
                "text": "Background continuation capacity is currently full.",
                "error": {
                    "type": "background_capacity_exhausted",
                    "message": "Retry after another background continuation finishes.",
                    "retryable": True,
                },
                **released,
            },
        )
    worker_args = {key: value for key, value in args.items() if key != "background"}
    worker = Thread(
        target=_run_background_adaptive_continue,
        args=(worker_args, claim),
        name=f"agent-hub-continue-{claim.run_id}",
        daemon=True,
    )
    try:
        with _BACKGROUND_CONTINUE_LOCK:
            _BACKGROUND_CONTINUE_THREADS[claim.run_id] = worker
        worker.start()
    except Exception:
        with _BACKGROUND_CONTINUE_LOCK:
            _BACKGROUND_CONTINUE_THREADS.pop(claim.run_id, None)
        _BACKGROUND_CONTINUE_SLOTS.release()
        try:
            store.abort_claim(claim)
        except store.RunStoreError:
            pass
        raise
    return envelope(
        "continue_workflow",
        {
            "success": True,
            "text": (
                "Adaptive continuation accepted in the background. "
                "Poll agent_hub_get_run for the committed revision."
            ),
            "accepted": True,
            "execution": "background",
            "run_id": claim.run_id,
            "workflow_id": "adaptive",
            "run_kind": "adaptive",
            "status": "running",
            "base_revision": claim.base_revision,
            "lease_active": True,
            "next_action": {
                "type": "call_tool",
                "tool": "agent_hub_get_run",
                "arguments": {"run_id": claim.run_id},
            },
        },
    )


def _continue_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
    run_id = args.get("run_id")
    supplied_state = args.get("state") if isinstance(args.get("state"), dict) else None
    supplied_is_adaptive = bool(
        supplied_state is not None and supplied_state.get("run_kind") == "adaptive"
    )
    supplied_revision = (
        supplied_state.get("store_revision")
        if supplied_is_adaptive and supplied_state is not None
        else None
    )
    expected_revision = args.get("expected_revision")
    for field, value in (
        ("expected_revision", expected_revision),
        ("state.store_revision", supplied_revision),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"{field} must be a non-negative integer")
    if (
        expected_revision is not None
        and supplied_revision is not None
        and expected_revision != supplied_revision
    ):
        raise ValueError("expected_revision does not match state.store_revision")
    expected_revision = expected_revision if expected_revision is not None else supplied_revision

    if supplied_state is not None:
        raw_state_run_id = supplied_state.get("run_id")
        if raw_state_run_id not in (None, ""):
            state_run_id = store.validate_run_id(raw_state_run_id)
        elif supplied_state.get("run_kind") == "adaptive":
            raise ValueError("adaptive state requires a valid run_id")
        else:
            state_run_id = ""
        if run_id not in (None, ""):
            requested_run_id = store.validate_run_id(run_id)
            if state_run_id and requested_run_id != state_run_id:
                raise ValueError("run_id does not match state.run_id")
            state_run_id = state_run_id or requested_run_id
        run_id = state_run_id
        if supplied_is_adaptive:
            try:
                persisted = store.load_strict(run_id)
            except store.RunStoreError as exc:
                return _run_store_error_response(store.validate_run_id(run_id), exc)
        else:
            persisted = supplied_state
    else:
        run_id = store.validate_run_id(run_id)
        try:
            persisted = store.load_strict(run_id)
        except store.RunStoreError as exc:
            return _run_store_error_response(run_id, exc)
    if supplied_is_adaptive or (
        isinstance(persisted, dict) and persisted.get("run_kind") == "adaptive"
    ):
        validated_run_id = store.validate_run_id(run_id)
        lease_seconds = ADAPTIVE_WORKFLOW_TIMEOUT_MAX + ADAPTIVE_LEASE_GRACE_SECONDS
        try:
            claim = store.claim(
                validated_run_id,
                expected_revision=expected_revision,
                lease_seconds=lease_seconds,
            )
        except store.RunStoreError as exc:
            return _run_store_error_response(validated_run_id, exc)
        if claim.state.get("run_kind") != "adaptive":
            try:
                store.abort_claim(claim)
            except Exception:  # noqa: BLE001
                pass
            raise ValueError("persisted run is not adaptive")
        if bool(args.get("background")):
            return _start_background_adaptive_continue(args, claim)
        try:
            return _continue_adaptive_workflow(args, claim.state, claim)
        except store.RunStoreError as exc:
            try:
                store.abort_claim(claim)
            except Exception:  # noqa: BLE001
                pass
            return _run_store_error_response(validated_run_id, exc)
        except Exception:
            try:
                store.abort_claim(claim)
            except Exception:  # noqa: BLE001
                pass
            raise
    if bool(args.get("background")):
        raise ValueError("background continuation is supported only for adaptive runs")
    try:
        state = runner.continue_run(
            run_id=run_id,
            state=supplied_state,
            stage_id=str(args.get("stage_id") or ""),
            result_text=str(args.get("result_text") or ""),
            leaf_result=args.get("leaf_result")
            if isinstance(args.get("leaf_result"), dict)
            else None,
            success=bool(args.get("success", True)),
            error=str(args.get("error") or ""),
            auto_local=bool(args.get("auto_local", True)),
            expected_revision=expected_revision,
            action_id=str(args.get("action_id") or ""),
            claim_token=str(args.get("claim_token") or ""),
            base_revision=args.get("base_revision"),
            handoff_drift_policy=args.get("handoff_drift_policy"),
        )
    except handoff_state.HandoffDrift as exc:
        return envelope(
            "continue_workflow",
            {
                "success": False,
                "text": str(exc),
                "error": {
                    "type": "handoff_drift",
                    "message": str(exc),
                    "retryable": True,
                },
                "run_id": run_id,
                "handoff_drift": exc.drift,
            },
        )
    except store.RunStoreError as exc:
        return _run_store_error_response(store.validate_run_id(run_id), exc)
    return envelope(
        "continue_workflow",
        {
            "success": state.get("status")
            not in {"failed", "cancelled", "archived"},
            "text": "Workflow advanced.",
            **state,
        },
    )


def _claim_run_action(args: Dict[str, Any]) -> Dict[str, Any]:
    run_id = store.validate_run_id(args.get("run_id"))
    try:
        claimed = runner.claim_next_action(
            run_id=run_id,
            expected_revision=args.get("expected_revision"),
            action_id=str(args.get("action_id") or ""),
            lease_seconds=max(5.0, min(float(args.get("lease_seconds") or 320), 600.0)),
            handoff_drift_policy=args.get("handoff_drift_policy"),
        )
    except store.RunStoreError as exc:
        return _run_store_error_response(
            run_id,
            exc,
            operation="claim_run_action",
        )
    return envelope(
        "claim_run_action",
        {
            "success": True,
            "text": "Fixed workflow action claimed before provider dispatch.",
            **claimed.public(),
        },
        success=True,
    )


def _get_run(args: Dict[str, Any]) -> Dict[str, Any]:
    run_id = store.validate_run_id(args.get("run_id"))
    persisted = store.load_strict(run_id)
    if persisted.get("run_kind") == "adaptive":
        state = _adaptive_public_state(persisted)
    else:
        state = runner.get_run(run_id)
    return envelope("get_run", {"success": True, "text": "Run loaded.", **state})


def _list_runs(args: Dict[str, Any]) -> Dict[str, Any]:
    project_root = str(gather.validate_project_root(args.get("project_root") or "."))
    listed = store.list_run_summaries(
        project_root,
        run_kind=str(args.get("run_kind") or "") or None,
        status=str(args.get("status") or "") or None,
        limit=int(args.get("limit") or 50),
        cursor=str(args.get("cursor") or "") or None,
    )
    warnings = ["run_summary_scan_truncated"] if listed.get("truncated") else []
    return envelope(
        "list_runs",
        {
            "success": True,
            "text": f"{len(listed['runs'])} run summaries for {project_root}.",
            **listed,
            "warnings": warnings,
        },
        success=True,
    )


def _get_run_events(args: Dict[str, Any]) -> Dict[str, Any]:
    project_root = str(gather.validate_project_root(args.get("project_root") or "."))
    result = run_events.read_run_events(
        store.validate_run_id(args.get("run_id")),
        project_root=project_root,
        after_seq=args.get("after_seq", 0),
        limit=args.get("limit", 50),
    )
    return envelope(
        "get_run_events",
        {
            "success": True,
            "text": f"{len(result['events'])} committed run events loaded.",
            **result,
        },
        success=True,
    )


def _cancel_run(args: Dict[str, Any]) -> Dict[str, Any]:
    run_id = store.validate_run_id(args.get("run_id"))
    project_root = str(gather.validate_project_root(args.get("project_root") or "."))
    try:
        result = run_lifecycle.cancel_run(
            run_id,
            project_root=project_root,
            expected_revision=args.get("expected_revision"),
            reason_code=str(args.get("reason_code") or "user_requested"),
        )
    except store.RunStoreError as exc:
        return _run_store_error_response(run_id, exc, operation="cancel_run")
    return envelope(
        "cancel_run",
        {
            "success": True,
            "text": (
                "Run cancelled."
                if result["changed"]
                else "Run was already cancelled."
            ),
            **result,
        },
        success=True,
    )


def _archive_run(args: Dict[str, Any]) -> Dict[str, Any]:
    run_id = store.validate_run_id(args.get("run_id"))
    project_root = str(gather.validate_project_root(args.get("project_root") or "."))
    try:
        result = run_lifecycle.archive_run(
            run_id,
            project_root=project_root,
            expected_revision=args.get("expected_revision"),
        )
    except store.RunStoreError as exc:
        return _run_store_error_response(run_id, exc, operation="archive_run")
    return envelope(
        "archive_run",
        {
            "success": True,
            "text": (
                "Run archived."
                if result["changed"]
                else "Run was already archived."
            ),
            **result,
        },
        success=True,
    )


def _gc_run(args: Dict[str, Any]) -> Dict[str, Any]:
    run_id = store.validate_run_id(args.get("run_id"))
    project_root = str(gather.validate_project_root(args.get("project_root") or "."))
    apply = bool(args.get("apply", False))
    try:
        result = run_lifecycle.gc_run(
            run_id,
            project_root=project_root,
            apply=apply,
            expected_revision=args.get("expected_revision"),
            expected_state_sha256=args.get("expected_state_sha256"),
        )
    except store.RunStoreError as exc:
        return _run_store_error_response(run_id, exc, operation="gc_run")
    return envelope(
        "gc_run",
        {
            "success": True,
            "text": (
                "Archived run deleted."
                if result["deleted"]
                else "Archived run GC plan prepared; no state was changed."
            ),
            **result,
        },
        success=True,
    )


def _prepare_takeover(args: Dict[str, Any]) -> Dict[str, Any]:
    project_root = str(gather.validate_project_root(args.get("project_root") or "."))
    prepared = takeover_state.prepare(
        store.validate_run_id(args.get("run_id")),
        project_root=project_root,
    )
    return envelope(
        "prepare_takeover",
        {
            "success": True,
            "text": "Takeover capsule prepared from authoritative run state.",
            **prepared,
        },
        success=True,
    )


def _resume_takeover(args: Dict[str, Any]) -> Dict[str, Any]:
    project_root = str(gather.validate_project_root(args.get("project_root") or "."))
    capsule = args.get("capsule")
    if not isinstance(capsule, dict):
        raise ValueError("capsule must be an object")
    try:
        resumed = takeover_state.resume(
            capsule,
            project_root=project_root,
            lease_seconds=max(
                5.0,
                min(float(args.get("lease_seconds") or 320), 600.0),
            ),
            handoff_drift_policy=args.get("handoff_drift_policy"),
        )
    except handoff_state.HandoffDrift as exc:
        return envelope(
            "resume_takeover",
            {
                "success": False,
                "text": str(exc),
                "error": {
                    "type": "handoff_drift",
                    "message": str(exc),
                    "retryable": True,
                },
                "run_id": store.validate_run_id(capsule.get("run_id")),
                "handoff_drift": exc.drift,
            },
        )
    except store.RunStoreError as exc:
        return _run_store_error_response(
            store.validate_run_id(capsule.get("run_id")),
            exc,
            operation="resume_takeover",
        )
    return envelope(
        "resume_takeover",
        {
            "success": True,
            "text": (
                "Fixed action claimed from a revalidated takeover capsule."
                if resumed.get("resume_mode") == "claimed_fixed_action"
                else (
                    "Adaptive takeover capsule revalidated; call the returned "
                    "revision-fenced continuation to execute the next wave."
                )
            ),
            **resumed,
        },
        success=True,
    )


def _run_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
    from agent_hub.core.inprocess import make_resolver

    if _is_adaptive(args):
        execution_args = {
            **args,
            "models": _snapshot_provider_models(args.get("models")),
        }
        workflow_timeout = _adaptive_workflow_timeout(args.get("workflow_timeout"))
        started = time.monotonic()
        requested_per_call = float(
            args.get("per_call_timeout") or ADAPTIVE_PER_CALL_TIMEOUT_DEFAULT
        )
        planning_args = {
            **args,
            "per_call_timeout": min(
                requested_per_call,
                max(
                    ADAPTIVE_TIMEOUT_RETURN_MARGIN,
                    workflow_timeout - ADAPTIVE_TIMEOUT_RETURN_MARGIN,
                ),
            ),
        }
        handoff_snapshot = _load_workflow_handoff(planning_args)
        plan = _adaptive_plan(planning_args, handoff_snapshot)
        executable = {key: plan.get(key) for key in ("schema", "goal", "rationale", "steps")}
        max_leaf_calls = int(args.get("max_leaf_calls") or broker.DEFAULT_MAX_LEAF_CALLS)
        max_concurrency = int(args.get("max_concurrency") or len(PROVIDERS))
        provider_budget = orchestrator.ProviderCallBudget(
            max_leaf_calls,
            max_concurrency=max_concurrency,
            deadline_monotonic=(started + workflow_timeout - ADAPTIVE_TIMEOUT_RETURN_MARGIN),
        )

        def invoke_with_budget(
            step: Dict[str, Any], provider: str, dependencies: Dict[str, Dict[str, Any]]
        ) -> Dict[str, Any]:
            remaining = workflow_timeout - (time.monotonic() - started)
            if remaining < ADAPTIVE_TIMEOUT_RETURN_MARGIN:
                return {
                    "success": False,
                    "error": "workflow_timeout_exceeded",
                    "text": "Adaptive workflow exhausted its end-to-end time budget.",
                }
            step_args = {
                **execution_args,
                "per_call_timeout": min(requested_per_call, remaining),
                "_provider_call_budget": provider_budget,
                "_handoff_snapshot": handoff_snapshot,
                "_reasoning_effort_implicit": (
                    str((plan.get("planner") or {}).get("provider") or "") != "caller"
                ),
            }
            return _adaptive_step_call(
                dict(step),
                provider,
                dict(dependencies),
                args=step_args,
                goal=plan["goal"],
            )

        result = orchestrator.execute_plan(
            executable,
            invoke=invoke_with_budget,
            max_concurrency=max_concurrency,
            max_calls=max_leaf_calls,
            max_elapsed_seconds=max(
                ADAPTIVE_TIMEOUT_RETURN_MARGIN,
                workflow_timeout - (time.monotonic() - started),
            ),
            call_budget=provider_budget,
        )
        resumable: Dict[str, Any] = {}
        if result.get("status") == "timed_out":
            state = _new_adaptive_state(plan, execution_args, handoff_snapshot)
            persisted = {
                step_id: item
                for step_id, item in (result.get("results") or {}).items()
                if isinstance(item, dict) and item.get("success")
            }
            state.update(
                {
                    "status": "paused",
                    "pause_reason": str(
                        result.get("error") or "workflow_timeout_exceeded"
                    ),
                    "results": persisted,
                    "waves": list(result.get("waves") or []),
                    "leaf_calls": int(result.get("leaf_calls") or 0),
                    "updated_at": time.time(),
                }
            )
            public = _save_adaptive_state(state)
            resumable = {
                "resumable": True,
                "run_id": state["run_id"],
                "next_action": public["next_action"],
            }
        return envelope(
            "run_workflow",
            {
                **result,
                "workflow_id": "adaptive",
                "dynamic": True,
                "planner": plan.get("planner"),
                "handoff": handoff_state.public_snapshot(handoff_snapshot),
                "planner_calls": int(
                    (plan.get("planner") or {}).get("attempts", 0)
                    if isinstance(plan.get("planner"), dict)
                    else 0
                ),
                "workflow_timeout": workflow_timeout,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                **resumable,
            },
        )
    resolved = _workflow_resolution(args)
    bindings = _fixed_bindings(args)
    run_args = {
        k: v
        for k, v in args.items()
        if k
        not in {
            "workflow_id",
            "recipe_id",
            "preset",
            "bindings",
            "max_leaf_calls",
            "per_call_timeout",
            "workflow_timeout",
        }
    }
    run_args["_provider_models"] = _effective_provider_models()
    result = broker.run_auto(
        resolved["recipe_id"],
        args=run_args,
        bindings=bindings,
        project_root=str(args.get("project_root") or "."),
        max_leaf_calls=int(args.get("max_leaf_calls") or broker.DEFAULT_MAX_LEAF_CALLS),
        per_call_timeout=float(args.get("per_call_timeout") or broker.DEFAULT_PER_CALL_TIMEOUT),
        client_resolver=make_resolver(),
    )
    return envelope(
        "run_workflow",
        {**result, **resolved, "text": result.get("artifact") or result.get("error") or ""},
    )


def _delegate(args: Dict[str, Any]) -> Dict[str, Any]:
    prepared = runner.prepare_step(
        capability=str(args.get("capability") or "chat"),
        instruction=str(args.get("instruction") or ""),
        doc_class=str(args.get("doc_class") or "direct"),
        model=args.get("model") or None,
        leaf=args.get("leaf") or None,
        write_task=args.get("write_task") or None,
        gather_kind=args.get("gather") or None,
        project_root=str(args.get("project_root") or "."),
        context=args.get("context") or None,
        provider_models=_effective_provider_models(),
        extra_args=args.get("extra_args") if isinstance(args.get("extra_args"), dict) else None,
    )
    return envelope("delegate", {"success": True, "text": "Delegated call prepared.", **prepared})


def _verify(args: Dict[str, Any]) -> Dict[str, Any]:
    root = str(args.get("project_root") or ".")
    try:
        fact_pack = gather.gather_durable_facts(root)
    except (ValueError, OSError):
        fact_pack = None
    result = verify.verify_text(
        str(args.get("text") or ""),
        doc_class=str(args.get("doc_class") or "durable"),
        fact_pack=fact_pack if isinstance(fact_pack, dict) else None,
        user_facing=bool(args.get("user_facing", False)),
    )
    return envelope("verify", result, success=bool(result.get("ok")))


PROVIDER_SCHEMA = {
    "provider": _provider_property(all_value=True),
    "probe": {"type": "boolean", "default": False},
}
AUTH_PROVIDER_SCHEMA = {"provider": _provider_property()}


def _operation_schema(
    base: Dict[str, Any], extra: Dict[str, Any] | None = None, *, neutral_model: bool = True
) -> Dict[str, Any]:
    schema = deepcopy(base)
    schema["properties"] = {**(schema.get("properties") or {}), **(extra or {})}
    if neutral_model and isinstance(schema["properties"].get("model"), dict):
        schema["properties"]["model"].pop("default", None)
    return schema


POLICY_CONTROL_SCHEMA = {
    "policy_mode": {
        "type": "string",
        "enum": ["off", "auto", "required"],
        "default": "auto",
        "description": "Inject a canonical project policy when project_root is supplied.",
    },
    "policy_file": {
        "type": "string",
        "description": "Optional policy path inside project_root; defaults to AGENTS.md then CLAUDE.md.",
    },
    "max_policy_chars": {
        "type": "integer",
        "minimum": 1,
        "maximum": 1000000,
        "default": consistency_gate.DEFAULT_MAX_POLICY_CHARS,
    },
}
REASONING_EFFORT_SCHEMA = {
    "reasoning_effort": {
        "type": "string",
        "enum": list(
            dict.fromkeys(
                effort
                for manifest in provider_registry.MANIFESTS.values()
                for effort in manifest.capabilities.get("chat", {}).get(
                    "reasoning_effort", []
                )
            )
        ),
        "description": (
            "Provider-neutral reasoning depth. Unsupported provider/model combinations fail closed."
        ),
    }
}


CHAT_SCHEMA = _operation_schema(
    google_mcp.CHAT_SCHEMA,
    {
        "images": {
            "type": "array",
            "items": {"type": ["string", "object"]},
            "description": "Local paths, public URLs, or data URLs. Local paths require workspace_root.",
        },
        "workspace_root": {"type": "string"},
        "project_root": {"type": "string"},
        "api_mode": {"type": "string", "enum": ["chat", "responses"]},
        **REASONING_EFFORT_SCHEMA,
        **POLICY_CONTROL_SCHEMA,
    },
)
CHAT_SCHEMA["properties"]["max_tokens"] = {
    "type": "integer",
    "minimum": 1,
    "maximum": limits.MAX_OUTPUT_TOKENS,
    "default": limits.MAX_OUTPUT_TOKENS,
}
SEARCH_SCHEMA = _operation_schema(
    google_mcp.GROUNDING_SCHEMA,
    {
        "source": {"type": "string", "enum": ["web", "x", "both"], "default": "web"},
        "allowed_domains": {"type": "array", "items": {"type": "string"}},
        "blocked_domains": {"type": "array", "items": {"type": "string"}},
        "allowed_x_handles": {"type": "array", "items": {"type": "string"}},
        "from_date": {"type": "string"},
        "to_date": {"type": "string"},
        "max_tokens": {
            "type": "integer",
            "minimum": 1,
            "maximum": limits.MAX_OUTPUT_TOKENS,
            "default": limits.MAX_OUTPUT_TOKENS,
        },
    },
)
WRITING_SCHEMA = _operation_schema(
    google_mcp.WRITING_SCHEMA,
    {
        **REASONING_EFFORT_SCHEMA,
        **POLICY_CONTROL_SCHEMA,
        "quality_rewrite_attempts": {
            "type": "integer",
            "minimum": 0,
            "maximum": 2,
            "default": 2,
            "description": (
                "Bounded full-document rewrites after the mandatory local quality gate fails. "
                "Zero disables rewriting but not verification or fail-closed behavior."
            ),
        },
    },
)
WRITING_SCHEMA["properties"]["max_tokens"] = {
    "type": "integer",
    "minimum": 1,
    "maximum": limits.MAX_OUTPUT_TOKENS,
    "default": limits.MAX_OUTPUT_TOKENS,
}
IMAGE_SCHEMA = _operation_schema(
    google_mcp.IMAGE_SCHEMA,
    {
        "resolution": {"type": "string"},
        "n": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
        "response_format": {"type": "string", "enum": ["url", "b64_json"], "default": "url"},
    },
)
COMPARE_SCHEMA = _operation_schema(
    google_mcp.COMPARE_SCHEMA,
    {
        "providers": {
            "type": "array",
            "items": {"type": "string", "enum": list(PROVIDERS)},
            "minItems": 1,
            "maxItems": len(PROVIDERS),
        },
        "system": {"type": "string"},
        "project_root": {"type": "string"},
        **REASONING_EFFORT_SCHEMA,
        **POLICY_CONTROL_SCHEMA,
        "execution": {
            "type": "string",
            "enum": ["parallel", "sequential"],
            "default": "parallel",
        },
        "max_concurrency": {
            "type": "integer",
            "minimum": 1,
            "maximum": len(PROVIDERS),
            "default": len(PROVIDERS),
        },
        "min_successes": {
            "type": "integer",
            "minimum": 1,
            "maximum": len(PROVIDERS),
            "default": 2,
            "description": "Minimum successful provider responses required for an open compare.",
        },
        "consistency": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "default": True},
                "decision_labels": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                    "minItems": 2,
                    "maxItems": 20,
                    "uniqueItems": True,
                },
                "threshold": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 1.0,
                    "default": 1.0,
                },
                "require_all": {"type": "boolean", "default": True},
                "min_responses": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": len(PROVIDERS),
                    "default": 2,
                },
                "project_root": {"type": "string"},
                **POLICY_CONTROL_SCHEMA,
            },
            "required": ["decision_labels"],
            "additionalProperties": False,
        },
    },
)
COMPARE_SCHEMA["properties"]["max_tokens"] = {
    "type": "integer",
    "minimum": 1,
    "maximum": limits.MAX_OUTPUT_TOKENS,
    "default": limits.MAX_OUTPUT_TOKENS,
}
REVIEW_DIFF_SCHEMA = _operation_schema(
    google_mcp.REVIEW_DIFF_SCHEMA,
    {**REASONING_EFFORT_SCHEMA, **POLICY_CONTROL_SCHEMA},
)
REVIEW_DIFF_SCHEMA["properties"]["max_tokens"] = {
    "type": "integer",
    "minimum": 1,
    "maximum": limits.MAX_OUTPUT_TOKENS,
    "default": limits.MAX_OUTPUT_TOKENS,
}
REVIEW_DIFF_SCHEMA["properties"]["require_complete"] = {
    "type": "boolean",
    "default": False,
    "description": "Require a completed review marker; missing marker fails closed.",
}
REVIEW_DIFF_SCHEMA["properties"]["include_untracked"] = {
    "type": "boolean",
    "default": False,
    "description": (
        "Include bounded, non-binary untracked files. Adaptive review enables this explicitly."
    ),
}
RELEASE_DRAFT_SCHEMA = _operation_schema(
    google_mcp.RELEASE_DRAFT_SCHEMA,
    {**REASONING_EFFORT_SCHEMA, **POLICY_CONTROL_SCHEMA},
)
RELEASE_DRAFT_SCHEMA["properties"]["max_tokens"] = {
    "type": "integer",
    "minimum": 1,
    "maximum": limits.MAX_OUTPUT_TOKENS,
    "default": limits.MAX_OUTPUT_TOKENS,
}
WORKFLOW_BASE = {
    "workflow_id": {"type": "string"},
    "preset": {"type": "string"},
    "prompt": {"type": "string"},
    "instruction": {"type": "string"},
    "project_root": {"type": "string", "default": "."},
    "provider": {
        **_provider_property(auto=True),
        "description": (
            "Primary chat/write provider for fixed workflows. Adaptive workflows choose "
            "step providers in the reviewed plan."
        ),
    },
    "models": {
        "type": "object",
        "properties": {
            "claude": {"type": "string"},
            "grok": {"type": "string"},
            "gemini": {"type": "string"},
            "gpt": {"type": "string"},
        },
        "additionalProperties": False,
        "description": "Optional explicit model id per provider for every adaptive step.",
    },
    "bindings": {"type": "object", "additionalProperties": {"type": "string"}},
    "plan": {
        "type": "object",
        "description": "A previously reviewed agent_hub_plan_v1 object for adaptive execution.",
    },
    "planner_provider": {
        "type": "string",
        "enum": list(PROVIDERS),
        "default": "gemini",
    },
    "planner_model": {"type": "string"},
    "planner_repair_attempts": {
        "type": "integer",
        "minimum": 0,
        "maximum": limits.MAX_PLANNER_REPAIRS,
        "default": limits.MAX_PLANNER_REPAIRS,
    },
    "planner_max_tokens": {
        "type": "integer",
        "minimum": 256,
        "maximum": limits.MAX_OUTPUT_TOKENS,
        "default": limits.MAX_OUTPUT_TOKENS,
    },
    "max_steps": {
        "type": "integer",
        "minimum": 1,
        "maximum": orchestrator.MAX_PLAN_STEPS,
        "default": orchestrator.MAX_PLAN_STEPS,
    },
    "max_concurrency": {
        "type": "integer",
        "minimum": 1,
        "maximum": len(PROVIDERS),
        "default": len(PROVIDERS),
    },
    **POLICY_CONTROL_SCHEMA,
    "handoff_mode": {
        "type": "string",
        "enum": ["off", "auto", "required"],
        "default": "auto",
        "description": "Load project HANDOFF.md as untrusted operational context.",
    },
    "handoff_search": {
        "type": "string",
        "enum": ["project-only", "nearest"],
        "default": "nearest",
    },
    "handoff_file": {"type": "string"},
    "handoff_drift_policy": {
        "type": "string",
        "enum": ["pause", "use-snapshot"],
        "default": "pause",
    },
    "max_handoff_chars": {
        "type": "integer",
        "minimum": 1,
        "maximum": handoff_state.MAX_HANDOFF_CHARS,
        "default": handoff_state.DEFAULT_MAX_CHARS,
    },
}
ADAPTIVE_EXECUTION_CONTROL_SCHEMA = {
    "max_leaf_calls": {
        "type": "integer",
        "minimum": 1,
        "maximum": limits.MAX_LEAF_CALLS,
        "default": broker.DEFAULT_MAX_LEAF_CALLS,
    },
    "per_call_timeout": {
        "type": "number",
        "minimum": 5,
        "maximum": ADAPTIVE_PER_CALL_TIMEOUT_MAX,
        "default": ADAPTIVE_PER_CALL_TIMEOUT_DEFAULT,
    },
    "workflow_timeout": {
        "type": "number",
        "minimum": ADAPTIVE_WORKFLOW_TIMEOUT_MIN,
        "maximum": ADAPTIVE_WORKFLOW_TIMEOUT_MAX,
        "default": ADAPTIVE_WORKFLOW_TIMEOUT_DEFAULT,
        "description": (
            "Time budget for one end-to-end adaptive call or one resumable slice. "
            "Provider calls are clamped to the remaining budget."
        ),
    },
}

TOOL_SPECS: List[Dict[str, Any]] = [
    _spec(
        "agent_hub_status",
        "Check Agent Hub",
        "Show consent, authentication, readiness, and default models for providers.",
        _object(PROVIDER_SCHEMA),
        read_only=True,
        idempotent=True,
        open_world=True,
    ),
    _spec(
        "agent_hub_list_models",
        "List Models",
        "List provider models and optionally probe live availability.",
        _object(PROVIDER_SCHEMA),
        read_only=True,
        idempotent=True,
        open_world=True,
    ),
    _spec(
        "agent_hub_auth_start",
        "Open Login Manager",
        "Return the local GUI command; account and consent changes require direct user action.",
        _object(AUTH_PROVIDER_SCHEMA, required=("provider",)),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_auth_complete",
        "Complete Login in GUI",
        "Return the local GUI command without accepting OAuth codes through MCP.",
        _object(AUTH_PROVIDER_SCHEMA, required=("provider",)),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_auth_refresh",
        "Refresh Login in GUI",
        "Return the local GUI command without rotating provider credentials through MCP.",
        _object(AUTH_PROVIDER_SCHEMA, required=("provider",)),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_auth_logout",
        "Disconnect in GUI",
        "Return the local GUI command; credential deletion requires direct user confirmation.",
        _object(AUTH_PROVIDER_SCHEMA, required=("provider",)),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_chat",
        "Chat",
        "Chat through an explicit provider or route by model when provider=auto.",
        _with_provider(CHAT_SCHEMA),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_search",
        "Grounded Search",
        "Run a source-backed search operation.",
        _with_supported_provider(
            SEARCH_SCHEMA,
            provider_registry.providers_supporting("search"),
        ),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_write",
        "Write",
        "Draft, rewrite, translate, polish, or summarize text.",
        _with_supported_provider(WRITING_SCHEMA, PROVIDERS),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_generate_image",
        "Generate Image",
        "Generate and cache an image.",
        _with_supported_provider(IMAGE_SCHEMA, ("grok", "gemini")),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_compare_models",
        "Compare Models",
        "Run one prompt across multiple models.",
        _with_supported_provider(COMPARE_SCHEMA, PROVIDERS, allow_all=True),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_review_diff",
        "Review Diff",
        "Collect and review a Git diff.",
        _with_supported_provider(REVIEW_DIFF_SCHEMA, PROVIDERS),
        read_only=True,
        open_world=True,
    ),
    _spec(
        "agent_hub_release_snapshot",
        "Release Snapshot",
        "Collect local Git release facts without model generation.",
        deepcopy(google_mcp.RELEASE_SNAPSHOT_SCHEMA),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_release_draft",
        "Release Draft",
        "Draft release notes or a PR description from a Git snapshot.",
        _with_supported_provider(RELEASE_DRAFT_SCHEMA, PROVIDERS),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_get_settings",
        "Get Settings",
        "Read model, transport, and profile preferences.",
        _object({"provider": _provider_property(all_value=True)}),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_update_settings",
        "Update Settings",
        "Update model, transport, or profile preferences.",
        _object(
            {
                "provider": _provider_property(auto=True),
                "model": {"type": "string"},
                "temperature": {"type": "number"},
                "max_tokens": {"type": "integer", "minimum": 1},
                "api_mode": {"type": "string", "enum": ["chat", "responses"]},
                "task": {"type": "string"},
                "validate": {"type": "boolean", "default": True},
                "notes": {"type": "string"},
                "transport": {"type": "string"},
                "clear_transport": {"type": "boolean", "default": False},
                "profile": {"type": "string"},
                "apply_model_pref": {"type": "boolean", "default": True},
                "apply_provider": {"type": "boolean", "default": True},
                "save_profile": {"type": "object"},
            }
        ),
        read_only=False,
        destructive=True,
    ),
    _spec(
        "agent_hub_reset_settings",
        "Reset Settings",
        "Reset model, transport, profile, or all preferences.",
        _object(
            {
                "provider": {
                    "type": "string",
                    "enum": ["all", *PROVIDERS],
                    "default": "gemini",
                },
                "reset": {
                    "type": "string",
                    "enum": ["all", "model", "transport", "profile"],
                    "default": "all",
                },
                "task": {"type": "string"},
                "all": {"type": "boolean", "default": False},
            }
        ),
        read_only=False,
        destructive=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_get_handoff",
        "Get Project Handoff",
        "Discover and read project-scoped HANDOFF.md as explicitly untrusted operational context.",
        _object(
            {
                "project_root": {"type": "string", "default": "."},
                "mode": {
                    "type": "string",
                    "enum": ["off", "auto", "required"],
                    "default": "auto",
                },
                "search": {
                    "type": "string",
                    "enum": ["project-only", "nearest"],
                    "default": "nearest",
                },
                "file": {"type": "string"},
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": handoff_state.MAX_HANDOFF_CHARS,
                    "default": handoff_state.DEFAULT_MAX_CHARS,
                },
            }
        ),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_prepare_handoff_update",
        "Prepare Handoff Update",
        "Build a marker-managed HANDOFF.md update and SHA fence without writing it.",
        _object(
            {
                "project_root": {"type": "string", "default": "."},
                "body": {"type": "string"},
                "file": {"type": "string"},
                "base_managed_sha256": {
                    "type": ["string", "null"],
                    "pattern": "^[0-9a-f]{64}$",
                },
                "search": {
                    "type": "string",
                    "enum": ["project-only", "nearest"],
                    "default": "project-only",
                },
            },
            required=("body",),
        ),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_apply_handoff_update",
        "Apply Handoff Update",
        "Atomically apply a prepared HANDOFF.md update when the whole-file SHA matches.",
        _object(
            {
                "project_root": {"type": "string", "default": "."},
                "file": {"type": "string"},
                "content": {"type": "string"},
                "expected_sha256": {
                    "type": ["string", "null"],
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            required=("file", "content", "expected_sha256"),
        ),
        read_only=False,
        destructive=True,
    ),
    _spec(
        "agent_hub_list_workflows",
        "List Workflows",
        "List real multi-stage workflow templates and presets.",
        _object(),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_get_workflow",
        "Get Workflow",
        "Explain a workflow, preset, context policy, and bindings.",
        _object(
            {"workflow_id": {"type": "string"}, "preset": {"type": "string"}},
            required=("workflow_id",),
        ),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_plan_workflow",
        "Plan Workflow",
        "Resolve a static workflow or ask a planner LLM for a validated adaptive DAG.",
        _object(
            {
                **WORKFLOW_BASE,
                "max_leaf_calls": deepcopy(ADAPTIVE_EXECUTION_CONTROL_SCHEMA["max_leaf_calls"]),
            },
            required=("workflow_id",),
            additional=True,
        ),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_start_workflow",
        "Start Workflow",
        "Start and persist a supervised workflow, including a resumable adaptive run.",
        _object(
            {
                **WORKFLOW_BASE,
                **ADAPTIVE_EXECUTION_CONTROL_SCHEMA,
                "auto_local": {"type": "boolean", "default": True},
            },
            required=("workflow_id",),
            additional=True,
        ),
        read_only=False,
    ),
    _spec(
        "agent_hub_claim_run_action",
        "Claim Run Action",
        "Claim one fixed provider action before dispatch and return a fenced commit token.",
        _object(
            {
                "run_id": {
                    "type": "string",
                    "pattern": store.RUN_ID_PATTERN,
                },
                "expected_revision": {"type": "integer", "minimum": 0},
                "action_id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "lease_seconds": {
                    "type": "number",
                    "minimum": 5,
                    "maximum": 600,
                    "default": 320,
                },
                "handoff_drift_policy": {
                    "type": "string",
                    "enum": ["pause", "use-snapshot"],
                    "default": "pause",
                },
            },
            required=("run_id", "expected_revision", "action_id"),
        ),
        read_only=False,
    ),
    _spec(
        "agent_hub_continue_workflow",
        "Continue Workflow",
        "Advance a fixed run with a leaf result or execute the next adaptive wave.",
        _object(
            {
                "run_id": {
                    "type": "string",
                    "pattern": store.RUN_ID_PATTERN,
                },
                "state": {
                    "type": "object",
                    "description": (
                        "Optional echoed state; it must contain the same run_id and cannot "
                        "replace persisted state authority."
                    ),
                },
                "expected_revision": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Optional CAS fence for persisted state. A stale revision is "
                        "rejected before any provider call."
                    ),
                },
                "action_id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                    "description": "Claimed fixed action id.",
                },
                "claim_token": {
                    "type": "string",
                    "pattern": store.CLAIM_TOKEN_PATTERN,
                    "description": (
                        "Capability returned by agent_hub_claim_run_action. "
                        "Never persist it in HANDOFF or logs."
                    ),
                },
                "base_revision": {
                    "type": "integer",
                    "minimum": 0,
                },
                "stage_id": {"type": "string"},
                "result_text": {"type": "string"},
                "leaf_result": {
                    "type": "object",
                    "description": (
                        "Optional provider-neutral result metadata. Unknown or sensitive "
                        "fields are discarded before persistence."
                    ),
                    "additionalProperties": True,
                },
                "success": {"type": "boolean", "default": True},
                "error": {"type": "string"},
                "auto_local": {"type": "boolean", "default": True},
                "background": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "For adaptive runs, claim the revision and return immediately while "
                        "the wave executes in a bounded background worker. Poll "
                        "agent_hub_get_run until lease_active is false."
                    ),
                },
                "handoff_drift_policy": {
                    "type": "string",
                    "enum": ["pause", "use-snapshot"],
                    "default": "pause",
                },
                **ADAPTIVE_EXECUTION_CONTROL_SCHEMA,
                "max_waves_per_call": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": ADAPTIVE_MAX_WAVES_PER_CALL,
                    "default": ADAPTIVE_MAX_WAVES_PER_CALL,
                    "description": (
                        "Dependency-ready adaptive waves to execute before persisting and returning."
                    ),
                },
            }
        ),
        read_only=False,
    ),
    _spec(
        "agent_hub_prepare_takeover",
        "Prepare Takeover",
        "Prepare a redacted capsule bound to the current run revision and project.",
        _object(
            {
                "project_root": {"type": "string", "default": "."},
                "run_id": {
                    "type": "string",
                    "pattern": store.RUN_ID_PATTERN,
                }
            },
            required=("project_root", "run_id"),
        ),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_resume_takeover",
        "Resume Takeover",
        (
            "Revalidate a capsule, then claim its fixed action or return a "
            "revision-fenced adaptive continuation without executing it."
        ),
        _object(
            {
                "project_root": {"type": "string"},
                "capsule": takeover_state.CAPSULE_JSON_SCHEMA,
                "lease_seconds": {
                    "type": "number",
                    "minimum": 5,
                    "maximum": 600,
                    "default": 320,
                },
                "handoff_drift_policy": {
                    "type": "string",
                    "enum": ["pause", "use-snapshot"],
                    "default": "pause",
                },
            },
            required=("project_root", "capsule"),
        ),
        read_only=False,
    ),
    _spec(
        "agent_hub_list_runs",
        "List Project Runs",
        "List bounded, redacted workflow summaries for one canonical project root.",
        _object(
            {
                "project_root": {"type": "string", "default": "."},
                "run_kind": {
                    "type": "string",
                    "enum": ["fixed", "adaptive"],
                },
                "status": {
                    "type": "string",
                    "enum": [
                        "running",
                        "paused",
                        "completed",
                        "failed",
                        "cancelled",
                        "archived",
                    ],
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": store.MAX_SUMMARY_LIMIT,
                    "default": 50,
                },
                "cursor": {"type": "string", "maxLength": 512},
            }
        ),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_get_run",
        "Get Run",
        "Load a workflow run from memory or the file-backed store.",
        _object(
            {
                "run_id": {
                    "type": "string",
                    "pattern": store.RUN_ID_PATTERN,
                }
            },
            required=("run_id",),
        ),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_get_run_events",
        "Get Run Events",
        (
            "Load a bounded page of committed, redacted events for one "
            "project-scoped workflow run."
        ),
        _object(
            {
                "project_root": {"type": "string"},
                "run_id": {
                    "type": "string",
                    "pattern": store.RUN_ID_PATTERN,
                },
                "after_seq": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": run_events.MAX_EVENT_LIMIT,
                    "default": 50,
                },
            },
            required=("project_root", "run_id"),
        ),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_cancel_run",
        "Cancel Run",
        (
            "Revision-fenced cancellation for an active project-scoped run. "
            "No provider call is started by this operation."
        ),
        _object(
            {
                "project_root": {"type": "string"},
                "run_id": {
                    "type": "string",
                    "pattern": store.RUN_ID_PATTERN,
                },
                "expected_revision": {
                    "type": "integer",
                    "minimum": 0,
                },
                "reason_code": {
                    "type": "string",
                    "enum": sorted(run_lifecycle.CANCEL_REASONS),
                    "default": "user_requested",
                },
            },
            required=("project_root", "run_id", "expected_revision"),
        ),
        read_only=False,
        destructive=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_archive_run",
        "Archive Run",
        "Revision-fenced archival for a completed, failed, or cancelled run.",
        _object(
            {
                "project_root": {"type": "string"},
                "run_id": {
                    "type": "string",
                    "pattern": store.RUN_ID_PATTERN,
                },
                "expected_revision": {
                    "type": "integer",
                    "minimum": 0,
                },
            },
            required=("project_root", "run_id", "expected_revision"),
        ),
        read_only=False,
        destructive=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_gc_run",
        "Garbage Collect Run",
        (
            "Prepare or explicitly apply deletion of one archived run. "
            "Apply requires both revision and complete-state SHA fences."
        ),
        _object(
            {
                "project_root": {"type": "string"},
                "run_id": {
                    "type": "string",
                    "pattern": store.RUN_ID_PATTERN,
                },
                "apply": {
                    "type": "boolean",
                    "default": False,
                },
                "expected_revision": {
                    "type": "integer",
                    "minimum": 0,
                },
                "expected_state_sha256": {
                    "type": "string",
                    "pattern": store.STATE_SHA256_PATTERN,
                },
            },
            required=("project_root", "run_id"),
        ),
        read_only=False,
        destructive=True,
    ),
    _spec(
        "agent_hub_run_workflow",
        "Run Workflow",
        "Run a workflow end-to-end with in-process provider adapters.",
        _object(
            {
                **WORKFLOW_BASE,
                **ADAPTIVE_EXECUTION_CONTROL_SCHEMA,
            },
            required=("workflow_id",),
            additional=True,
        ),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_delegate",
        "Prepare Delegation",
        "Prepare one provider call with routing, context, and fallback information.",
        _object(
            {
                "capability": {
                    "type": "string",
                    "enum": [
                        "chat",
                        "write",
                        "grounded_search",
                        "image",
                        "review_diff",
                        "release",
                        "compare",
                    ],
                },
                "instruction": {"type": "string"},
                "doc_class": {
                    "type": "string",
                    "enum": sorted(policy.DOC_CLASSES),
                    "default": "direct",
                },
                "leaf": {"type": "string"},
                "model": {"type": "string"},
                "write_task": {"type": "string"},
                "gather": {"type": "string", "enum": ["facts", "code", "git"]},
                "project_root": {"type": "string", "default": "."},
                "context": {"type": "string"},
                "extra_args": {"type": "object"},
            },
            required=("instruction",),
        ),
        read_only=True,
    ),
    _spec(
        "agent_hub_verify",
        "Verify Output",
        "Check generated text against its document context policy and repository facts.",
        _object(
            {
                "text": {"type": "string"},
                "doc_class": {
                    "type": "string",
                    "enum": sorted(policy.DOC_CLASSES),
                    "default": "durable",
                },
                "project_root": {"type": "string", "default": "."},
                "user_facing": {
                    "type": "boolean",
                    "default": False,
                    "description": "Apply stricter natural-language checks for a public README.",
                },
            },
            required=("text",),
        ),
        read_only=True,
        idempotent=True,
    ),
]


TOOL_HANDLERS: Dict[str, ProviderHandler] = {
    "agent_hub_status": _status,
    "agent_hub_list_models": _list_models,
    "agent_hub_auth_start": _auth_start,
    "agent_hub_auth_complete": _auth_complete,
    "agent_hub_auth_refresh": _auth_refresh,
    "agent_hub_auth_logout": _auth_logout,
    "agent_hub_chat": _chat,
    "agent_hub_search": _search,
    "agent_hub_write": _write,
    "agent_hub_generate_image": _generate_image,
    "agent_hub_compare_models": _compare_models,
    "agent_hub_review_diff": _review_diff,
    "agent_hub_release_snapshot": _release_snapshot,
    "agent_hub_release_draft": _release_draft,
    "agent_hub_get_settings": _get_settings,
    "agent_hub_update_settings": _update_settings,
    "agent_hub_reset_settings": _reset_settings,
    "agent_hub_get_handoff": _get_handoff,
    "agent_hub_prepare_handoff_update": _prepare_handoff_update,
    "agent_hub_apply_handoff_update": _apply_handoff_update,
    "agent_hub_list_workflows": _list_workflows,
    "agent_hub_get_workflow": _get_workflow,
    "agent_hub_plan_workflow": _plan_workflow,
    "agent_hub_start_workflow": _start_workflow,
    "agent_hub_claim_run_action": _claim_run_action,
    "agent_hub_continue_workflow": _continue_workflow,
    "agent_hub_prepare_takeover": _prepare_takeover,
    "agent_hub_resume_takeover": _resume_takeover,
    "agent_hub_list_runs": _list_runs,
    "agent_hub_get_run": _get_run,
    "agent_hub_get_run_events": _get_run_events,
    "agent_hub_cancel_run": _cancel_run,
    "agent_hub_archive_run": _archive_run,
    "agent_hub_gc_run": _gc_run,
    "agent_hub_run_workflow": _run_workflow,
    "agent_hub_delegate": _delegate,
    "agent_hub_verify": _verify,
}


def _build_operation_registry() -> Dict[str, Dict[str, Any]]:
    specs = {spec["name"]: spec for spec in TOOL_SPECS}
    handlers = set(TOOL_HANDLERS)
    if set(specs) != handlers:
        raise RuntimeError(
            f"canonical tool registry mismatch: missing={sorted(set(specs) - handlers)}, "
            f"unlisted={sorted(handlers - set(specs))}"
        )
    return {name: {"spec": spec, "handler": TOOL_HANDLERS[name]} for name, spec in specs.items()}


OPERATION_REGISTRY = _build_operation_registry()


def tool_definitions() -> List[Dict[str, Any]]:
    return [deepcopy(entry["spec"]) for entry in OPERATION_REGISTRY.values()]


def dispatch_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    entry = OPERATION_REGISTRY.get(name)
    if entry is None:
        raise ValueError(f"unknown canonical tool: {name}")
    try:
        return entry["handler"](arguments or {})
    except Exception as exc:  # noqa: BLE001
        return envelope(
            name.removeprefix("agent_hub_"),
            {
                "success": False,
                "text": str(exc),
                "error": str(exc),
                "error_type": getattr(exc, "code", type(exc).__name__),
            },
            success=False,
        )
