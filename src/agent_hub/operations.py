"""Canonical Agent Hub operations and their single public registry."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Callable, Dict, Iterable, List

from claude_codex import auth as claude_auth
from claude_codex import models as claude_models
from claude_codex import mcp_server as claude_mcp
from claude_codex import security as claude_security
from claude_codex import subscription_auth as claude_subscription
from grok_codex import auth as grok_auth
from grok_codex import models as grok_models
from grok_codex import mcp_server as grok_mcp
from grok_codex import oauth_login as grok_oauth
from grok_codex import security as grok_security
from google_antigravity_codex import account as google_account
from google_antigravity_codex import agy_auth as google_auth
from google_antigravity_codex import mcp_server as google_mcp
from google_antigravity_codex import model_prefs as google_model_prefs
from google_antigravity_codex import oauth_login as google_oauth
from google_antigravity_codex import profiles as google_profiles
from google_antigravity_codex import provider as google_provider
from google_antigravity_codex import security as google_security
from google_antigravity_codex import session_prefs as google_session_prefs
from orchestrate_codex import broker, gather, policy, recipes, runner, verify


ProviderHandler = Callable[[Dict[str, Any]], Dict[str, Any]]

PROVIDERS = ("claude", "grok", "gemini")
PROVIDER_ALIASES = {
    "anthropic": "claude",
    "xai": "grok",
    "google": "gemini",
    "antigravity": "gemini",
    "google-antigravity": "gemini",
}

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


def _with_gemini_provider(schema: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(schema)
    out["properties"] = {
        "provider": {
            "type": "string",
            "enum": ["auto", "gemini"],
            "default": "auto",
        },
        **(out.get("properties") or {}),
    }
    out["additionalProperties"] = False
    return out


def _normalize_provider(value: Any, *, allow_all: bool = False, allow_auto: bool = False) -> str:
    provider = str(value or ("all" if allow_all else "auto" if allow_auto else "")).strip().lower()
    provider = PROVIDER_ALIASES.get(provider, provider)
    allowed = set(PROVIDERS)
    if allow_all:
        allowed.add("all")
    if allow_auto:
        allowed.add("auto")
    if provider not in allowed:
        raise ValueError(f"provider must be one of: {', '.join(sorted(allowed))}")
    return provider


def _selected_providers(value: Any) -> List[str]:
    provider = _normalize_provider(value or "all", allow_all=True)
    return list(PROVIDERS) if provider == "all" else [provider]


def _require_google_consent() -> None:
    if not google_security.agy_session_enabled():
        raise RuntimeError(
            "Google Antigravity consent is required. Grant consent or set "
            "GOOGLE_ANTIGRAVITY_ENABLE_AGY_SESSION=1."
        )


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


def _status(args: Dict[str, Any]) -> Dict[str, Any]:
    probe = bool(args.get("probe", False))
    states: Dict[str, Any] = {}
    for provider in _selected_providers(args.get("provider")):
        if provider == "claude":
            consent = claude_security.consent_status()
            auth = claude_auth.status()
            login = claude_subscription.status()
            states[provider] = {
                "consent": bool(consent.get("user_consent")),
                "authenticated": bool(auth.get("configured")),
                "ready": bool(consent.get("user_consent") and auth.get("configured")),
                "auth_mode": auth.get("mode") or login.get("mode"),
                "default_model": claude_models.DEFAULT_MODEL,
                "warnings": [],
            }
        elif provider == "grok":
            consent = grok_security.consent_status()
            auth = grok_auth.status()
            login = grok_oauth.status()
            states[provider] = {
                "consent": bool(consent.get("user_consent")),
                "authenticated": bool(auth.get("configured")),
                "ready": bool(consent.get("user_consent") and auth.get("configured")),
                "auth_mode": auth.get("mode") or login.get("mode"),
                "default_model": grok_models.DEFAULT_MODEL,
                "warnings": [],
            }
        else:
            consent = google_security.consent_status()
            login = google_oauth.login_status()
            provider_state = google_provider.status(probe=probe)
            authenticated = bool(
                login.get("credentials_readable") and login.get("expired") is not True
            )
            configured = bool(provider_state.get("configured"))
            healthy = provider_state.get("healthy")
            ready = bool(
                (consent.get("user_consent") or consent.get("agy_session_enabled"))
                and authenticated
                and configured
                and healthy is not False
            )
            states[provider] = {
                "consent": bool(consent.get("user_consent") or consent.get("agy_session_enabled")),
                "authenticated": authenticated,
                "ready": ready,
                "auth_mode": provider_state.get("auth_method") or "plugin_oauth_login",
                "default_model": google_model_prefs.resolve_model(
                    task="chat", fallback="gemini-3.5-flash-high"
                ),
                "identity": {"email": login.get("email")} if login.get("email") else None,
                "quota_available": False,
                "warnings": []
                if ready
                else [str(provider_state.get("error_type") or "provider_not_ready")],
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


def _auth_start(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _normalize_provider(args.get("provider"))
    if provider == "claude":
        raw = {
            "success": True,
            "text": "Run `claude auth login --claudeai`, then mirror Keychain credentials on macOS.",
            "next_action": {
                "type": "external_cli",
                "command": "claude auth login --claudeai",
            },
        }
    elif provider == "grok":
        grok_security.require_consent()
        raw = grok_oauth.start_login(open_browser=bool(args.get("open_browser", True)))
    else:
        _require_google_consent()
        raw = google_oauth.start_login(
            use_local_redirect=bool(args.get("use_local_redirect", True))
        )
    return envelope("auth_start", raw, provider=provider)


def _auth_complete(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _normalize_provider(args.get("provider"))
    if provider == "claude":
        raise ValueError(
            "Claude login completes in the external Claude CLI; use agent_hub_status afterward."
        )
    if provider == "grok":
        grok_security.require_consent()
        raw = grok_oauth.complete_login()
    else:
        _require_google_consent()
        value = str(args.get("code_or_url") or args.get("code") or args.get("url") or "")
        raw = google_oauth.complete_login(value, probe=bool(args.get("probe", True)))
    return envelope("auth_complete", raw, provider=provider)


def _auth_refresh(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _normalize_provider(args.get("provider"))
    if provider == "claude":
        raw = claude_mcp.dispatch_tool("claude_codex_login_refresh", {})
    elif provider == "gemini":
        raw = google_auth.refresh_tool({})
    else:
        token = grok_oauth.resolve_access_token()
        raw = {
            "success": bool(token),
            "text": "SuperGrok OAuth token is ready."
            if token
            else "No refreshable SuperGrok token found.",
            "error": "token_missing" if not token else None,
        }
    return envelope("auth_refresh", raw, provider=provider)


def _auth_logout(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _normalize_provider(args.get("provider"))
    if provider == "claude":
        raw = {
            "success": False,
            "text": "Claude credentials are managed by Claude Code. Use its logout command.",
            "error": "external_logout_required",
        }
    elif provider == "grok":
        removed = grok_oauth.clear_tokens()
        raw = {"success": True, "removed": removed, "text": "Local SuperGrok tokens removed."}
    else:
        raw = google_account.logout({"forget_client": bool(args.get("forget_client", False))})
    return envelope("auth_logout", raw, provider=provider)


def _auto_chat_provider(args: Dict[str, Any]) -> str:
    requested = _normalize_provider(args.get("provider") or "auto", allow_auto=True)
    if requested != "auto":
        return requested
    model = str(args.get("model") or "").lower()
    if model.startswith("grok"):
        return "grok"
    if model.startswith("gemini") or model.startswith("models/gemini"):
        return "gemini"
    return "claude"


_BASIC_CHAT_KEYS = {
    "prompt",
    "system",
    "model",
    "max_tokens",
    "temperature",
    "timeout_sec",
    "messages",
}


def _chat(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _auto_chat_provider(args)
    call_args = {k: v for k, v in args.items() if k != "provider" and v is not None}
    if provider == "claude":
        raw = claude_mcp.dispatch_tool(
            "claude_codex_chat", {k: v for k, v in call_args.items() if k in _BASIC_CHAT_KEYS}
        )
    elif provider == "grok":
        raw = grok_mcp.dispatch_tool(
            "grok_codex_chat", {k: v for k, v in call_args.items() if k in _BASIC_CHAT_KEYS}
        )
    else:
        raw = _unwrap_mcp_result(google_mcp.dispatch_tool("google_antigravity_chat", call_args))
    return envelope("chat", raw, provider=provider)


def _google_operation(operation: str, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _normalize_provider(args.get("provider") or "auto", allow_auto=True)
    if provider not in {"auto", "gemini"}:
        raise ValueError(f"{operation} is currently supported only by the Gemini adapter")
    call_args = {k: v for k, v in args.items() if k != "provider" and v is not None}
    raw = _unwrap_mcp_result(google_mcp.dispatch_tool(tool, call_args))
    return envelope(operation, raw, provider="gemini")


def _get_settings(_args: Dict[str, Any]) -> Dict[str, Any]:
    raw = {
        "success": True,
        "text": "Agent Hub settings loaded.",
        "model_preferences": google_model_prefs.get_prefs_tool({}),
        "session": google_session_prefs.get_session_prefs({}),
        "profiles": google_profiles.list_profiles_tool({}),
        "scope": "gemini",
    }
    return envelope("get_settings", raw, success=True)


def _update_settings(args: Dict[str, Any]) -> Dict[str, Any]:
    changes: List[Dict[str, Any]] = []
    if args.get("model"):
        changes.append(
            google_model_prefs.set_model_tool(
                {
                    "model": args["model"],
                    "task": args.get("task"),
                    "validate": bool(args.get("validate", True)),
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
    reset = str(args.get("reset") or "all")
    changes: List[Dict[str, Any]] = []
    if reset in {"all", "model"}:
        changes.append(
            google_model_prefs.clear_prefs_tool(
                {"task": args.get("task"), "all": reset == "all" or bool(args.get("all"))}
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
    }
    return envelope("reset_settings", raw, provider="gemini")


def _workflow_resolution(args: Dict[str, Any]) -> Dict[str, Any]:
    return recipes.resolve_workflow(
        str(args.get("workflow_id") or args.get("recipe_id") or ""),
        str(args.get("preset") or ""),
    )


def _list_workflows(_args: Dict[str, Any]) -> Dict[str, Any]:
    workflows = recipes.list_workflows()
    return envelope(
        "list_workflows",
        {"success": True, "text": f"{len(workflows)} workflow templates.", "workflows": workflows},
        success=True,
    )


def _get_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
    resolved = recipes.explain_workflow(
        str(args.get("workflow_id") or ""), str(args.get("preset") or "")
    )
    return envelope("get_workflow", {"success": True, "text": "Workflow resolved.", **resolved})


def _plan_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
    resolved = _workflow_resolution(args)
    bindings = args.get("bindings") if isinstance(args.get("bindings"), dict) else None
    plan_args = {
        k: v for k, v in args.items() if k not in {"workflow_id", "recipe_id", "preset", "bindings"}
    }
    planned = recipes.plan_recipe(resolved["recipe_id"], args=plan_args, bindings=bindings)
    return envelope(
        "plan_workflow",
        {"success": True, "text": "Workflow plan ready.", **resolved, "plan": planned},
    )


def _start_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
    resolved = _workflow_resolution(args)
    bindings = args.get("bindings") if isinstance(args.get("bindings"), dict) else None
    run_args = {
        k: v
        for k, v in args.items()
        if k not in {"workflow_id", "recipe_id", "preset", "bindings", "auto_local"}
    }
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


def _continue_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
    state = runner.continue_run(
        run_id=str(args.get("run_id") or ""),
        state=args.get("state") if isinstance(args.get("state"), dict) else None,
        stage_id=str(args.get("stage_id") or ""),
        result_text=str(args.get("result_text") or ""),
        success=bool(args.get("success", True)),
        error=str(args.get("error") or ""),
        auto_local=bool(args.get("auto_local", True)),
    )
    return envelope(
        "continue_workflow",
        {"success": state.get("status") != "failed", "text": "Workflow advanced.", **state},
    )


def _get_run(args: Dict[str, Any]) -> Dict[str, Any]:
    state = runner.get_run(str(args.get("run_id") or ""))
    return envelope("get_run", {"success": True, "text": "Run loaded.", **state})


def _run_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
    from agent_hub.core.inprocess import make_resolver

    resolved = _workflow_resolution(args)
    bindings = args.get("bindings") if isinstance(args.get("bindings"), dict) else None
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
        }
    }
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
    )
    return envelope("verify", result, success=True)


PROVIDER_SCHEMA = {
    "provider": _provider_property(all_value=True),
    "probe": {"type": "boolean", "default": False},
}
AUTH_PROVIDER_SCHEMA = {"provider": _provider_property()}
WORKFLOW_BASE = {
    "workflow_id": {"type": "string"},
    "preset": {"type": "string"},
    "prompt": {"type": "string"},
    "instruction": {"type": "string"},
    "project_root": {"type": "string", "default": "."},
    "bindings": {"type": "object", "additionalProperties": {"type": "string"}},
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
        "Start Login",
        "Start the provider-specific login flow or return the required external action.",
        _object(
            {
                **AUTH_PROVIDER_SCHEMA,
                "open_browser": {"type": "boolean", "default": True},
                "use_local_redirect": {"type": "boolean", "default": True},
            },
            required=("provider",),
        ),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_auth_complete",
        "Complete Login",
        "Complete a provider login flow.",
        _object(
            {
                **AUTH_PROVIDER_SCHEMA,
                "code_or_url": {"type": "string"},
                "code": {"type": "string"},
                "url": {"type": "string"},
                "probe": {"type": "boolean", "default": True},
            },
            required=("provider",),
        ),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_auth_refresh",
        "Refresh Login",
        "Refresh or validate a provider OAuth token.",
        _object(AUTH_PROVIDER_SCHEMA, required=("provider",)),
        read_only=False,
        idempotent=True,
        open_world=True,
    ),
    _spec(
        "agent_hub_auth_logout",
        "Log Out",
        "Delete local OAuth credentials for a provider.",
        _object(
            {**AUTH_PROVIDER_SCHEMA, "forget_client": {"type": "boolean", "default": False}},
            required=("provider",),
        ),
        read_only=False,
        destructive=True,
    ),
    _spec(
        "agent_hub_chat",
        "Chat",
        "Chat through an explicit provider or route by model when provider=auto.",
        _with_provider(google_mcp.CHAT_SCHEMA),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_search",
        "Grounded Search",
        "Run a source-backed search operation.",
        _with_gemini_provider(google_mcp.GROUNDING_SCHEMA),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_write",
        "Write",
        "Draft, rewrite, translate, polish, or summarize text.",
        _with_gemini_provider(google_mcp.WRITING_SCHEMA),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_generate_image",
        "Generate Image",
        "Generate and cache an image.",
        _with_gemini_provider(google_mcp.IMAGE_SCHEMA),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_compare_models",
        "Compare Models",
        "Run one prompt across multiple models.",
        _with_gemini_provider(google_mcp.COMPARE_SCHEMA),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_review_diff",
        "Review Diff",
        "Collect and review a Git diff.",
        _with_gemini_provider(google_mcp.REVIEW_DIFF_SCHEMA),
        read_only=True,
        open_world=True,
    ),
    _spec(
        "agent_hub_release_snapshot",
        "Release Snapshot",
        "Collect local Git release facts without model generation.",
        _with_gemini_provider(google_mcp.RELEASE_SNAPSHOT_SCHEMA),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_release_draft",
        "Release Draft",
        "Draft release notes or a PR description from a Git snapshot.",
        _with_gemini_provider(google_mcp.RELEASE_DRAFT_SCHEMA),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_get_settings",
        "Get Settings",
        "Read model, transport, and profile preferences.",
        _object(),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_update_settings",
        "Update Settings",
        "Update model, transport, or profile preferences.",
        _object(
            {
                "model": {"type": "string"},
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
    ),
    _spec(
        "agent_hub_reset_settings",
        "Reset Settings",
        "Reset model, transport, profile, or all preferences.",
        _object(
            {
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
        "Resolve a workflow into concrete steps without starting it.",
        _object(WORKFLOW_BASE, required=("workflow_id",), additional=True),
        read_only=True,
    ),
    _spec(
        "agent_hub_start_workflow",
        "Start Workflow",
        "Start a supervised workflow and return its next action.",
        _object(
            {**WORKFLOW_BASE, "auto_local": {"type": "boolean", "default": True}},
            required=("workflow_id",),
            additional=True,
        ),
        read_only=False,
    ),
    _spec(
        "agent_hub_continue_workflow",
        "Continue Workflow",
        "Advance a supervised workflow with a completed leaf result.",
        _object(
            {
                "run_id": {"type": "string"},
                "state": {"type": "object"},
                "stage_id": {"type": "string"},
                "result_text": {"type": "string"},
                "success": {"type": "boolean", "default": True},
                "error": {"type": "string"},
                "auto_local": {"type": "boolean", "default": True},
            }
        ),
        read_only=False,
    ),
    _spec(
        "agent_hub_get_run",
        "Get Run",
        "Load a workflow run from memory or the file-backed store.",
        _object({"run_id": {"type": "string"}}, required=("run_id",)),
        read_only=True,
        idempotent=True,
    ),
    _spec(
        "agent_hub_run_workflow",
        "Run Workflow",
        "Run a workflow end-to-end with in-process provider adapters.",
        _object(
            {
                **WORKFLOW_BASE,
                "max_leaf_calls": {"type": "integer", "minimum": 1, "maximum": 100, "default": 24},
                "per_call_timeout": {
                    "type": "number",
                    "minimum": 5,
                    "maximum": 600,
                    "default": 180,
                },
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
    "agent_hub_search": lambda args: _google_operation("search", "google_grounded_search", args),
    "agent_hub_write": lambda args: _google_operation("write", "google_antigravity_write", args),
    "agent_hub_generate_image": lambda args: _google_operation(
        "generate_image", "google_antigravity_generate_image", args
    ),
    "agent_hub_compare_models": lambda args: _google_operation(
        "compare_models", "google_antigravity_compare_models", args
    ),
    "agent_hub_review_diff": lambda args: _google_operation(
        "review_diff", "google_antigravity_review_diff", args
    ),
    "agent_hub_release_snapshot": lambda args: _google_operation(
        "release_snapshot", "google_antigravity_release_snapshot", args
    ),
    "agent_hub_release_draft": lambda args: _google_operation(
        "release_draft", "google_antigravity_release_draft", args
    ),
    "agent_hub_get_settings": _get_settings,
    "agent_hub_update_settings": _update_settings,
    "agent_hub_reset_settings": _reset_settings,
    "agent_hub_list_workflows": _list_workflows,
    "agent_hub_get_workflow": _get_workflow,
    "agent_hub_plan_workflow": _plan_workflow,
    "agent_hub_start_workflow": _start_workflow,
    "agent_hub_continue_workflow": _continue_workflow,
    "agent_hub_get_run": _get_run,
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
