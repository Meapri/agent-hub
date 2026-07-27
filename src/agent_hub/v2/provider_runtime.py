"""V2-native provider execution boundary.

Provider workers use this module directly instead of routing through the
retired multi-tool Agent Hub server.  It intentionally exposes only the
operations required by the provider worker ABI and the local connection GUI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from agent_hub import capabilities, consistency, orchestrator, provider_settings
from agent_hub.core import limits, media
from claude_codex import auth as claude_auth
from claude_codex import models as claude_models
from claude_codex import mcp_server as claude_mcp
from claude_codex import search as claude_search
from claude_codex import security as claude_security
from google_antigravity_codex import mcp_server as google_mcp
from google_antigravity_codex import model_prefs as google_model_prefs
from google_antigravity_codex import paths as google_paths
from google_antigravity_codex import oauth_login as google_oauth
from google_antigravity_codex import profiles as google_profiles
from google_antigravity_codex import provider as google_provider
from google_antigravity_codex import security as google_security
from google_antigravity_codex import session_prefs as google_session_prefs
from google_antigravity_codex import writing as google_writing
from grok_codex import auth as grok_auth
from grok_codex import image as grok_image
from grok_codex import paths as grok_paths
from grok_codex import models as grok_models
from grok_codex import mcp_server as grok_mcp
from grok_codex import search as grok_search
from grok_codex import security as grok_security
from openai_codex import auth as openai_auth
from openai_codex import models as openai_models
from openai_codex import mcp_server as openai_mcp
from openai_codex import security as openai_security

from .contracts import ensure_public_model_id
from .errors import HubV2Error
from .failure_classes import UNCLASSIFIED_PROVIDER_FAILURE

PROVIDERS = ("claude", "grok", "gemini", "gpt")
PLANNER_REPAIR_LIMIT = limits.MAX_PLANNER_REPAIRS
RUNTIME_PLANNER_CAPABILITIES = (
    "chat",
    "inspect_codebase",
    "search",
    "write",
    "review_text",
)
_BASIC_CHAT_KEYS = {
    "prompt",
    "system",
    "model",
    "max_tokens",
    "temperature",
    "timeout_sec",
    "messages",
    "images",
    "api_mode",
    "session_id",
    "tools",
    "reasoning_effort",
}


def _envelope(
    operation: str,
    raw: Mapping[str, Any] | None,
    *,
    provider: str | None = None,
    success: bool | None = None,
) -> dict[str, Any]:
    payload = dict(raw or {})
    ok = bool(payload.get("success", payload.get("ok", not payload.get("error"))))
    if success is not None:
        ok = success
    text = payload.get("text")
    if not isinstance(text, str):
        text = json.dumps(payload, ensure_ascii=False)
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    error = payload.get("error")
    if not ok and not isinstance(error, Mapping):
        error = {
            "type": str(payload.get("error_type") or UNCLASSIFIED_PROVIDER_FAILURE),
            "message": text,
        }
    return {
        "success": ok,
        "operation": operation,
        "provider": provider or payload.get("provider") or None,
        "model": payload.get("model") or None,
        "text": text,
        "finish_reason": payload.get("finish_reason") or None,
        "usage": payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {},
        "warnings": [str(item) for item in warnings],
        "error": None if ok else dict(error or {}),
        "artifacts": (
            list(payload.get("artifacts") or [])
            if isinstance(payload.get("artifacts"), list)
            else []
        ),
        "data": payload,
    }


def _unwrap_mcp_result(result: Mapping[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if not isinstance(structured, Mapping):
        return dict(result)
    raw = dict(structured)
    content = result.get("content")
    if isinstance(content, list) and content and isinstance(content[0], Mapping):
        raw.setdefault("text", str(content[0].get("text") or ""))
    raw.setdefault("success", not bool(result.get("isError")))
    return raw


def _model_state(
    provider: str,
    *,
    fallback: str,
    environment_name: str | None = None,
) -> dict[str, Any]:
    settings, settings_error = provider_settings.inspect(provider)
    saved = str(settings.get("model") or "").strip()
    environment = str(os.getenv(environment_name, "") or "").strip() if environment_name else ""
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


def _gemini_model_state() -> dict[str, Any]:
    prefs, settings_error = google_model_prefs.inspect_prefs()
    tasks = prefs.get("task_models") if isinstance(prefs.get("task_models"), Mapping) else {}
    task_model = str(tasks.get("chat") or "").strip()
    saved_default = str(prefs.get("default_model") or "").strip()
    environment = str(os.getenv("GOOGLE_ANTIGRAVITY_DEFAULT_MODEL", "") or "").strip()
    profile = google_profiles.active_profile()
    profile_model = str((profile or {}).get("model") or "").strip()
    profile_name = str((profile or {}).get("name") or "").strip()
    selected = (
        task_model or saved_default or environment or profile_model or "gemini-3.5-flash-high"
    )
    return {
        "default_model": google_model_prefs.normalize_model_id(selected),
        "base_default_model": "gemini-3.5-flash-high",
        "model_overridden": bool(task_model or saved_default),
        "model_managed_by_environment": bool(environment and not task_model and not saved_default),
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


def _auth_lifecycle(
    *,
    account_present: bool,
    logged_in: bool,
    auth_ready: bool,
    refresh_supported: bool,
) -> dict[str, bool]:
    ready = bool(auth_ready)
    logged_in = bool(logged_in or ready)
    account_present = bool(account_present or logged_in)
    refreshable = bool(logged_in and not ready and refresh_supported)
    return {
        "account_present": account_present,
        "logged_in": logged_in,
        "auth_ready": ready,
        "refresh_supported": bool(refresh_supported),
        "refreshable": refreshable,
        "relogin_required": bool(account_present and not ready and not refreshable),
    }


def _auth_warnings(authenticated: bool, lifecycle: Mapping[str, bool]) -> list[str]:
    if authenticated:
        return []
    if lifecycle["refreshable"]:
        return ["auth_refresh_available"]
    if lifecycle["relogin_required"]:
        return ["reauthentication_required"]
    return ["credentials_missing"]


def status(provider: str, *, probe: bool = False) -> dict[str, Any]:
    """Return one provider's redacted readiness state."""

    if provider not in PROVIDERS:
        raise HubV2Error(
            "unknown_provider",
            "The provider is not supported.",
            scope="provider",
            safe_details={"provider": provider},
        )
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
                subscription.get("logged_in") and subscription.get("has_refresh_token")
            ),
        )
        model_state = _model_state(
            "claude",
            fallback=claude_models.DEFAULT_MODEL,
            environment_name="CLAUDE_CODEX_MODEL",
        )
        state = {
            "consent": bool(consent.get("user_consent")),
            "configured": bool(auth.get("configured")),
            "authenticated": authenticated,
            "ready": bool(consent.get("user_consent") and authenticated),
            "auth_mode": auth.get("active_mode"),
            "requested_auth_mode": auth.get("requested_mode"),
            **lifecycle,
            **model_state,
            "capabilities": capabilities.provider_capabilities("claude"),
            "warnings": [
                *_auth_warnings(authenticated, lifecycle),
                # Two lanes bill differently and send different headers, so a
                # substitution is a fact about this run, not a detail.
                *(["auth_lane_substituted"] if auth.get("lane_substituted") else []),
            ],
        }
    elif provider == "grok":
        consent = grok_security.consent_status()
        auth = grok_auth.status()
        authenticated = bool(auth.get("ready"))
        subscription = auth.get("subscription") or {}
        lifecycle = _auth_lifecycle(
            account_present=bool(
                auth.get("credentials_present") or subscription.get("token_file_present")
            ),
            logged_in=bool(auth.get("credentials_present")),
            auth_ready=authenticated,
            refresh_supported=bool(
                subscription.get("logged_in") and subscription.get("has_refresh_token")
            ),
        )
        model_state = _model_state(
            "grok",
            fallback=grok_models.DEFAULT_MODEL,
            environment_name="GROK_CODEX_MODEL",
        )
        state = {
            "consent": bool(consent.get("user_consent")),
            "configured": bool(auth.get("configured")),
            "authenticated": authenticated,
            "ready": bool(consent.get("user_consent") and authenticated),
            "auth_mode": auth.get("active_mode"),
            **lifecycle,
            "local_credentials_present": bool(subscription.get("token_file_present")),
            "pending_login_present": bool(subscription.get("pending_login_present")),
            **model_state,
            "capabilities": capabilities.provider_capabilities("grok"),
            "warnings": _auth_warnings(authenticated, lifecycle),
        }
    elif provider == "gpt":
        consent = openai_security.consent_status()
        auth = openai_auth.status(refresh=False)
        authenticated = bool(auth.get("configured"))
        lifecycle = _auth_lifecycle(
            account_present=bool(auth.get("logged_in")),
            logged_in=bool(auth.get("logged_in")),
            auth_ready=authenticated,
            refresh_supported=False,
        )
        model_state = _model_state("gpt", fallback=openai_models.DEFAULT_MODEL)
        warnings = [
            str(auth[key]) for key in ("warning", "status_warning", "error_type") if auth.get(key)
        ]
        state = {
            "consent": bool(consent.get("user_consent")),
            "configured": authenticated,
            "authenticated": authenticated,
            "ready": bool(consent.get("user_consent") and authenticated),
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
        authenticated = bool(login.get("credentials_readable") and login.get("expired") is not True)
        lifecycle = _auth_lifecycle(
            account_present=bool(login.get("token_file_present")),
            logged_in=bool(login.get("credentials_readable")),
            auth_ready=authenticated,
            refresh_supported=bool(login.get("refresh_token_present")),
        )
        configured = bool(provider_state.get("configured"))
        ready = bool(
            (consent.get("user_consent") or consent.get("agy_session_enabled"))
            and authenticated
            and configured
            and provider_state.get("healthy") is not False
        )
        model_state = _gemini_model_state()
        state = {
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
                else _auth_warnings(authenticated, lifecycle)
                or [str(provider_state.get("error_type") or "provider_not_ready")]
            ),
        }
    auto_refresh_on_invoke = bool(
        provider == "gemini"
        and state.get("consent")
        and state.get("configured")
        and state.get("refreshable")
    )
    state["auto_refresh_on_invoke"] = auto_refresh_on_invoke
    state["invocation_ready"] = bool(state.get("ready") or auto_refresh_on_invoke)
    if state.get("settings_error"):
        state["warnings"] = [*state.get("warnings", []), state["settings_error"]]
    return _envelope(
        "status",
        {
            "success": True,
            "text": f"{provider} provider status loaded.",
            "providers": {provider: state},
            "probe": probe,
        },
        success=True,
    )


def catalog(provider: str, *, refresh: bool = False) -> dict[str, Any]:
    """Return one provider model catalog without placeholder model IDs."""

    try:
        if provider == "claude":
            listed = claude_models.list_models({"probe": refresh})
        elif provider == "grok":
            listed = grok_models.list_models({"probe": refresh})
        elif provider == "gpt":
            listed = openai_models.list_models({"probe": refresh})
        elif provider == "gemini":
            listed = _unwrap_mcp_result(
                google_mcp.dispatch_tool("google_antigravity_list_models", {})
            )
        else:
            raise HubV2Error(
                "unknown_provider",
                "The provider is not supported.",
                scope="provider",
            )
    except HubV2Error:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HubV2Error(
            "model_list_failed",
            "The provider model catalog is unavailable.",
            scope="provider",
            retryable=True,
            safe_details={"exception_type": type(exc).__name__},
        ) from None
    models = listed.get("text_models")
    if not isinstance(models, list):
        models = listed.get("models")
    if isinstance(models, list):
        for model in models:
            if isinstance(model, Mapping) and isinstance(model.get("id"), str):
                ensure_public_model_id(model["id"])
    return _envelope(
        "list_models",
        {
            "success": True,
            "text": f"Model catalog returned for {provider}.",
            "models": {provider: listed},
        },
        success=True,
    )


def _prepare_multimodal(arguments: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(arguments)
    images = media.normalize_images(
        output.pop("images", None),
        workspace_root=output.get("workspace_root"),
    )
    output.pop("workspace_root", None)
    if not images:
        return output
    prompt = str(output.get("prompt") or "")
    messages = output.get("messages")
    messages = (
        [dict(item) for item in messages if isinstance(item, Mapping)]
        if isinstance(messages, list)
        else []
    )
    if not messages and output.get("system"):
        messages.append({"role": "system", "content": str(output["system"])})
    messages.append({"role": "user", "content": media.user_content(prompt, images)})
    output["messages"] = messages
    output.pop("prompt", None)
    output.pop("system", None)
    return output


def _effective_model(provider: str) -> str:
    if provider == "gemini":
        return str(_gemini_model_state()["default_model"])
    defaults = {
        "claude": (claude_models.DEFAULT_MODEL, "CLAUDE_CODEX_MODEL"),
        "grok": (grok_models.DEFAULT_MODEL, "GROK_CODEX_MODEL"),
        "gpt": (openai_models.DEFAULT_MODEL, None),
    }
    fallback, environment = defaults[provider]
    return str(
        _model_state(
            provider,
            fallback=fallback,
            environment_name=environment,
        )["default_model"]
    )


def chat(provider: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Invoke a provider chat leaf through its private adapter."""

    capabilities.require(provider, "chat")
    call_args = {
        key: value for key, value in arguments.items() if key != "provider" and value is not None
    }
    if provider in {"claude", "grok", "gpt"}:
        for key, value in provider_settings.get(provider).items():
            call_args.setdefault(key, value)
    model = str(call_args.get("model") or _effective_model(provider))
    if call_args.get("reasoning_effort") is not None and not capabilities.supports_reasoning_effort(
        provider, model
    ):
        call_args.pop("reasoning_effort", None)
    call_args = _prepare_multimodal(call_args)
    call_args, provenance = consistency.prepare_provider_call(call_args)
    if provider == "claude":
        raw = claude_mcp.dispatch_tool(
            "claude_codex_chat",
            {key: value for key, value in call_args.items() if key in _BASIC_CHAT_KEYS},
        )
    elif provider == "grok":
        raw = grok_mcp.dispatch_tool(
            "grok_codex_chat",
            {key: value for key, value in call_args.items() if key in _BASIC_CHAT_KEYS},
        )
    elif provider == "gpt":
        raw = openai_mcp.dispatch_tool(
            "openai_codex_chat",
            {key: value for key, value in call_args.items() if key in _BASIC_CHAT_KEYS},
        )
    else:
        if call_args.get("reasoning_effort") is not None:
            call_args["thinking_level"] = call_args.pop("reasoning_effort")
        raw = _unwrap_mcp_result(google_mcp.dispatch_tool("google_antigravity_chat", call_args))
    result = dict(raw)
    existing = result.get("consistency")
    result["consistency"] = {
        **(dict(existing) if isinstance(existing, Mapping) else {}),
        **provenance,
    }
    return _envelope("chat", result, provider=provider)


def search(provider: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    capabilities.require(provider, "search")
    call_args = {
        key: value for key, value in arguments.items() if key != "provider" and value is not None
    }
    if provider == "claude":
        raw = claude_search.run_search(call_args)
    elif provider == "grok":
        raw = grok_search.run_search(call_args)
    elif provider == "gemini":
        raw = _unwrap_mcp_result(google_mcp.dispatch_tool("google_grounded_search", call_args))
    else:
        raise HubV2Error(
            "unsupported_worker_capability",
            "The provider does not support search.",
            scope="provider",
        )
    return _envelope("search", raw, provider=provider)


def write(provider: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    capabilities.require(provider, "write")
    built = google_writing.build_prompt(dict(arguments))
    response = chat(
        provider,
        {
            "prompt": built["prompt"],
            "system": built["system"],
            "model": arguments.get("model"),
            "max_tokens": arguments.get("max_tokens"),
            "timeout_sec": arguments.get("timeout_sec"),
            "reasoning_effort": arguments.get("reasoning_effort"),
            "temperature": arguments.get("temperature", 0.35),
        },
    )
    data = response.get("data")
    raw = dict(data) if isinstance(data, Mapping) else dict(response)
    raw["text"] = str(response.get("text") or raw.get("text") or "").strip()
    raw["task"] = built["task"]
    raw["profiles"] = built["profiles"]
    raw["call_usage"] = {"provider_calls": 1}
    return _envelope("write", raw, provider=provider)


def generated_image_root(provider: str) -> Path:
    """Where this provider's image adapter is allowed to have written its file.

    The worker checks the reported path against this before reading it, so that
    a provider response cannot turn into a read of an arbitrary file.
    """

    if provider == "grok":
        return Path(grok_paths.cache_dir()) / "images"
    if provider == "gemini":
        return Path(google_paths.images_dir())
    raise HubV2Error(
        "unsupported_worker_capability",
        "The provider does not support image generation.",
        scope="provider",
    )


def generate_image(provider: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    capabilities.require(provider, "image_generation")
    call_args = {
        key: value for key, value in arguments.items() if key != "provider" and value is not None
    }
    if provider == "grok":
        raw = grok_image.generate_image(call_args)
    elif provider == "gemini":
        raw = _unwrap_mcp_result(
            google_mcp.dispatch_tool("google_antigravity_generate_image", call_args)
        )
    else:
        raise HubV2Error(
            "unsupported_worker_capability",
            "The provider does not support image generation.",
            scope="provider",
        )
    return _envelope("generate_image", raw, provider=provider)


def invoke(
    provider: str,
    capability: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    # vision is chat with the image as the subject. _prepare_multimodal turns
    # the data URLs into whatever message shape this provider wants.
    if capability in {"chat", "review", "decide", "vision"}:
        return chat(provider, arguments)
    if capability == "search":
        return search(provider, arguments)
    if capability == "write":
        return write(provider, arguments)
    if capability == "image":
        return generate_image(provider, arguments)
    raise HubV2Error(
        "unsupported_worker_capability",
        "This capability must be handled by the local runtime.",
        scope="provider",
        safe_details={"capability": capability},
    )


def plan(
    provider: str,
    *,
    prompt: str,
    model: str | None,
    max_steps: int,
    max_leaf_calls: int,
    max_tokens: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Generate and locally validate the provider-independent planner DAG."""

    initial = orchestrator.planner_prompt(
        prompt,
        facts="",
        max_steps=max_steps,
        allowed_capabilities=RUNTIME_PLANNER_CAPABILITIES,
    )
    previous = ""
    validation_error = ""
    attempts: list[dict[str, Any]] = []
    for attempt in range(PLANNER_REPAIR_LIMIT + 1):
        planner_input = initial
        if attempt:
            planner_input += (
                "\n\nThe previous JSON plan failed local validation. "
                f"Reason: {validation_error}. Return a corrected JSON object only.\n"
                f"Rejected plan:\n{previous[:12000]}"
            )
        response = chat(
            provider,
            {
                "prompt": planner_input,
                "model": model,
                "temperature": 0.1,
                "max_tokens": max_tokens,
                "timeout_sec": timeout_seconds,
            },
        )
        previous = str(response.get("text") or "")
        if not response.get("success"):
            validation_error = "provider_call_failed"
            attempts.append({"attempt": attempt + 1, "success": False})
            continue
        try:
            parsed = orchestrator.parse_plan(previous)
            parsed["goal"] = prompt
            validated = orchestrator.validate_plan(
                parsed,
                max_steps=max_steps,
                max_calls=max_leaf_calls,
                allowed_capabilities=RUNTIME_PLANNER_CAPABILITIES,
            )
        except ValueError as exc:
            validation_error = str(exc)[:200]
            attempts.append({"attempt": attempt + 1, "success": False})
            continue
        attempts.append({"attempt": attempt + 1, "success": True})
        return _envelope(
            "plan_workflow",
            {
                "success": True,
                "text": f"Planner returned {len(validated['steps'])} validated steps.",
                "plan": validated,
                "planner": {
                    "provider": provider,
                    "model": response.get("model") or model,
                    "attempts": len(attempts),
                },
            },
            provider=provider,
        )
    raise HubV2Error(
        "planner_execution_failed",
        "The planner could not produce a valid plan.",
        scope="planner",
        retryable=True,
        safe_details={"reason_code": "plan_validation_failed"},
    )


def set_default_model(provider: str, model: str) -> dict[str, Any]:
    model = ensure_public_model_id(model)
    if provider == "gemini":
        result = google_model_prefs.set_model_tool(
            {"model": model, "task": "chat", "validate": False, "notes": ""}
        )
        return dict(result)
    if provider not in {"claude", "grok", "gpt"}:
        raise HubV2Error(
            "unknown_provider",
            "The provider is not supported.",
            scope="provider",
        )
    return {
        "success": True,
        "provider": provider,
        "settings": provider_settings.update(provider, {"model": model}),
    }


def reset_default_model(provider: str, *, gemini_task: str | None = None) -> dict[str, Any]:
    if provider == "gemini":
        return dict(
            google_model_prefs.clear_prefs_tool(
                {
                    "task": gemini_task,
                    "all": False,
                    "default_scopes": not bool(gemini_task),
                }
            )
        )
    if provider not in {"claude", "grok", "gpt"}:
        raise HubV2Error(
            "unknown_provider",
            "The provider is not supported.",
            scope="provider",
        )
    return {
        "success": True,
        "provider": provider,
        "remaining": provider_settings.remove(provider, {"model"}),
    }


def reset_all_settings(provider: str) -> dict[str, Any]:
    if provider == "gemini":
        changes = [
            google_model_prefs.clear_prefs_tool({"all": True}),
            google_session_prefs.clear_provider(),
            google_profiles.use_profile_tool({"name": ""}),
        ]
        return {"success": all(item.get("success", True) for item in changes)}
    return {"success": True, **provider_settings.reset(provider)}
