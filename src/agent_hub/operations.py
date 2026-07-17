"""Canonical Agent Hub operations and their single public registry."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Callable, Dict, Iterable, List

from agent_hub import (
    capabilities,
    consistency as consistency_gate,
    orchestrator,
    provider_settings,
)
from agent_hub.core import media, parallel
from claude_codex import auth as claude_auth
from claude_codex import models as claude_models
from claude_codex import mcp_server as claude_mcp
from claude_codex import search as claude_search
from claude_codex import security as claude_security
from claude_codex import subscription_auth as claude_subscription
from grok_codex import auth as grok_auth
from grok_codex import models as grok_models
from grok_codex import mcp_server as grok_mcp
from grok_codex import image as grok_image
from grok_codex import oauth_login as grok_oauth
from grok_codex import search as grok_search
from grok_codex import security as grok_security
from google_antigravity_codex import account as google_account
from google_antigravity_codex import agy_auth as google_auth
from google_antigravity_codex import mcp_server as google_mcp
from google_antigravity_codex import model_prefs as google_model_prefs
from google_antigravity_codex import oauth_login as google_oauth
from google_antigravity_codex import profiles as google_profiles
from google_antigravity_codex import provider as google_provider
from google_antigravity_codex import diff_review as google_diff_review
from google_antigravity_codex import release as google_release
from google_antigravity_codex import security as google_security
from google_antigravity_codex import session_prefs as google_session_prefs
from google_antigravity_codex import writing as google_writing
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
                "settings": provider_settings.get("claude"),
                "capabilities": capabilities.provider_capabilities("claude"),
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
                "settings": provider_settings.get("grok"),
                "capabilities": capabilities.provider_capabilities("grok"),
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
                "capabilities": capabilities.provider_capabilities("gemini"),
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
    "images",
    "workspace_root",
    "api_mode",
    "session_id",
    "tools",
}


def _operation_provider(
    args: Dict[str, Any], capability: str, *, default: str = "gemini"
) -> str:
    requested = _normalize_provider(args.get("provider") or "auto", allow_auto=True)
    if requested != "auto":
        capabilities.require(requested, capability)
        return requested
    model = str(args.get("model") or "").lower()
    if model.startswith("claude"):
        selected = "claude"
    elif model.startswith("grok"):
        selected = "grok"
    elif model.startswith("gemini") or model.startswith("models/gemini"):
        selected = "gemini"
    elif capability == "search" and str(args.get("source") or "").lower() in {"x", "both"}:
        selected = "grok"
    else:
        selected = default
    capabilities.require(selected, capability)
    return selected


def _prepare_multimodal(call_args: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(call_args)
    images = media.normalize_images(out.pop("images", None), workspace_root=out.get("workspace_root"))
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


def _chat_raw(provider: str, args: Dict[str, Any]) -> Dict[str, Any]:
    capabilities.require(provider, "chat")
    call_args = {k: v for k, v in args.items() if k != "provider" and v is not None}
    if provider in {"claude", "grok"}:
        defaults = provider_settings.get(provider)
        for key, value in defaults.items():
            call_args.setdefault(key, value)
    call_args = _prepare_multimodal(call_args)
    call_args, provenance = consistency_gate.prepare_provider_call(call_args)
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
    result = dict(raw)
    existing = result.get("consistency") if isinstance(result.get("consistency"), dict) else {}
    result["consistency"] = {**existing, **provenance}
    return result


def _chat(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _auto_chat_provider(args)
    raw = _chat_raw(provider, args)
    return envelope("chat", raw, provider=provider)


def _search(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _operation_provider(args, "search", default="gemini")
    call_args = {key: value for key, value in args.items() if key != "provider" and value is not None}
    if provider == "claude":
        raw = claude_search.run_search(call_args)
    elif provider == "grok":
        raw = grok_search.run_search(call_args)
    else:
        raw = _unwrap_mcp_result(google_mcp.dispatch_tool("google_grounded_search", call_args))
    return envelope("search", raw, provider=provider)


def _write(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _operation_provider(args, "write", default="gemini")
    built = google_writing.build_prompt(args)
    raw_chat = _chat_raw(
        provider,
        {
            "prompt": built["prompt"],
            "system": built["system"],
            "model": args.get("model"),
            "temperature": args.get("temperature", 0.35),
            "max_tokens": args.get("max_tokens"),
            "timeout_sec": args.get("timeout_sec") or 180,
            "project_root": args.get("project_root"),
            "policy_mode": args.get("policy_mode"),
            "policy_file": args.get("policy_file"),
            "max_policy_chars": args.get("max_policy_chars"),
        },
    )
    text = str(raw_chat.get("text") or "").strip()
    warnings = google_writing.review_text(text, durable=bool(built.get("durable")))
    warnings.extend(
        str(item) for item in raw_chat.get("warnings") or [] if str(item) not in warnings
    )
    raw = {
        **raw_chat,
        "text": text,
        "task": built["task"],
        "profiles": built["profiles"],
        "doc_class": built.get("doc_class"),
        "project_context_used": built.get("project_context_used"),
        "fact_pack_used": built.get("fact_pack_used"),
        "warnings": warnings,
    }
    return envelope("write", raw, provider=provider)


def _generate_image(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _operation_provider(args, "image_generation", default="gemini")
    call_args = {key: value for key, value in args.items() if key != "provider" and value is not None}
    if provider == "grok":
        raw = grok_image.generate_image(call_args)
    else:
        raw = _unwrap_mcp_result(
            google_mcp.dispatch_tool("google_antigravity_generate_image", call_args)
        )
    return envelope("generate_image", raw, provider=provider)


def _model_provider(model: str) -> str:
    lowered = model.lower()
    if lowered.startswith("claude"):
        return "claude"
    if lowered.startswith("grok"):
        return "grok"
    return "gemini"


def _compare_models(args: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    raw_models = args.get("models")
    if isinstance(raw_models, str):
        models = [item.strip() for item in raw_models.replace(";", ",").split(",") if item.strip()]
    elif isinstance(raw_models, list):
        models = [str(item).strip() for item in raw_models if str(item).strip()]
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
    if not providers and models:
        providers = [_model_provider(model.split("/", 1)[-1]) for model in models]
    if not providers:
        providers = list(PROVIDERS)
    targets: List[tuple[str, str | None]] = []
    for index, provider in enumerate(providers[:3]):
        model = models[index] if index < len(models) else None
        if model and "/" in model and model.split("/", 1)[0] in PROVIDERS:
            prefix, model = model.split("/", 1)
            provider = prefix
        capabilities.require(provider, "compare")
        targets.append((provider, model))
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
                "timeout_sec": args.get("timeout_sec") or 180,
                "project_root": project_root if (gate_enabled or args.get("project_root")) else None,
                "policy_mode": policy_mode,
                "policy_file": policy_file or None,
                "max_policy_chars": max_policy_chars,
            },
        )

    execution = str(args.get("execution") or "parallel")
    max_concurrency = int(args.get("max_concurrency") or 3)
    outcomes = parallel.run_ordered(
        [lambda p=provider, m=model: call_target(p, m) for provider, model in targets],
        execution=execution,
        max_workers=max_concurrency,
    )
    results: List[Dict[str, Any]] = []
    for (provider, model), outcome in zip(targets, outcomes):
        if outcome.error is not None:
            provider_result: Dict[str, Any] = {
                "provider": provider,
                "model": model,
                "success": False,
                "text": "",
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
                "text": full_text[:4000],
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
    warnings = [] if ok == len(results) else ["partial_compare_failures"]
    consistency_report: Dict[str, Any] | None = None
    success = ok > 0
    text = f"Compared {len(results)} provider/model targets ({ok} succeeded)."
    if gate_enabled:
        consistency_report = consistency_gate.evaluate_decisions(
            results,
            threshold=float(gate_config.get("threshold", 1.0)),
            require_all=bool(gate_config.get("require_all", True)),
            min_responses=int(gate_config.get("min_responses", 2)),
        )
        policy_values = [
            item.get("provenance", {}).get("policy_sha256") for item in results
        ]
        request_values = [
            item.get("provenance", {}).get("request_sha256") for item in results
        ]
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
        success = bool(consistency_report["passed"])
        if success:
            text = (
                f'Consistency Gate passed with decision "{consistency_report["decision"]}" '
                f'({consistency_report["valid_responses"]}/{len(results)} valid).'
            )
        else:
            warnings.append("consistency_gate_human_review")
            text = "Consistency Gate requires human review: " + ", ".join(
                consistency_report["review_reasons"]
            )
    raw = {
        "success": success,
        "text": text,
        "results": results,
        "execution": execution,
        "warnings": list(dict.fromkeys(warnings)),
        **({"consistency": consistency_report} if consistency_report is not None else {}),
    }
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
            "timeout_sec": args.get("timeout_sec") or 180,
            "project_root": str(repo),
            "policy_mode": args.get("policy_mode") or "auto",
            "policy_file": args.get("policy_file"),
            "max_policy_chars": args.get("max_policy_chars"),
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
    call_args = {key: value for key, value in args.items() if key != "provider" and value is not None}
    return envelope("release_snapshot", google_release.release_snapshot(call_args), provider="local")


def _release_draft(args: Dict[str, Any]) -> Dict[str, Any]:
    call_args = {key: value for key, value in args.items() if key != "provider" and value is not None}
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
            "timeout_sec": args.get("timeout_sec") or 180,
            "project_root": str(args.get("repo") or "."),
            "policy_mode": args.get("policy_mode") or "auto",
            "policy_file": args.get("policy_file"),
            "max_policy_chars": args.get("max_policy_chars"),
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
    if "gemini" in providers:
        values["gemini"] = {
            "model_preferences": google_model_prefs.get_prefs_tool({}),
            "session": google_session_prefs.get_session_prefs({}),
            "profiles": google_profiles.list_profiles_tool({}),
            "scope": capabilities.provider_capabilities("gemini")["settings"]["scope"],
        }
    raw = {"success": True, "text": "Agent Hub settings loaded.", "providers": values}
    return envelope("get_settings", raw, success=True)


def _update_settings(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = _normalize_provider(args.get("provider") or "gemini", allow_auto=True)
    if provider == "auto":
        provider = "gemini"
    if provider in {"claude", "grok"}:
        changes = {
            key: args.get(key)
            for key in ("model", "temperature", "max_tokens", "api_mode")
            if args.get(key) is not None and (provider == "grok" or key != "api_mode")
        }
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
    provider = str(args.get("provider") or "gemini").lower()
    if provider in {"claude", "grok"}:
        removed = provider_settings.reset(provider)
        return envelope(
            "reset_settings",
            {"success": True, "text": f"Reset {provider} settings.", **removed},
            provider=provider,
        )
    if provider == "all":
        removed = [provider_settings.reset(item) for item in ("claude", "grok")]
    else:
        removed = []
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
        "provider_settings_removed": removed,
    }
    return envelope("reset_settings", raw, provider="multiple" if provider == "all" else "gemini")


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


def _adaptive_plan(args: Dict[str, Any]) -> Dict[str, Any]:
    supplied = args.get("plan")
    max_steps = int(args.get("max_steps") or orchestrator.MAX_PLAN_STEPS)
    max_calls = int(args.get("max_leaf_calls") or broker.DEFAULT_MAX_LEAF_CALLS)
    root = str(args.get("project_root") or ".")
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
    if isinstance(supplied, dict):
        raw_supplied = {
            key: supplied.get(key) for key in ("schema", "goal", "rationale", "steps")
        }
        plan = orchestrator.validate_plan(
            raw_supplied, max_steps=max_steps, max_calls=max_calls
        )
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
    repairs = max(0, min(int(args.get("planner_repair_attempts", 1)), 2))
    attempts: List[Dict[str, Any]] = []
    previous_text = ""
    validation_error = ""
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
                "max_tokens": int(args.get("planner_max_tokens") or 12000),
                "timeout_sec": int(args.get("per_call_timeout") or 240),
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
            plan = orchestrator.validate_plan(parsed, max_steps=max_steps, max_calls=max_calls)
        except ValueError as exc:
            validation_error = str(exc)
            attempts.append(
                {"attempt": attempt + 1, "success": False, "error": validation_error}
            )
            continue
        attempts.append({"attempt": attempt + 1, "success": True})
        provenance = response.get("consistency") if isinstance(response.get("consistency"), dict) else {}
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


def _adaptive_context(
    goal: str, step: Dict[str, Any], dependencies: Dict[str, Dict[str, Any]]
) -> str:
    parts = [f"Overall goal:\n{goal}", f"Current step:\n{step['instruction']}"]
    for step_id, result in dependencies.items():
        parts.append(f"Dependency output — {step_id}:\n{str(result.get('text') or '')[:24000]}")
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
    policy_args = {
        "policy_mode": args.get("policy_mode") or "required",
        "policy_file": args.get("policy_file"),
        "max_policy_chars": args.get("max_policy_chars"),
    }
    context = _adaptive_context(goal, step, dependencies)
    common = {
        "provider": provider,
        "model": None,
        "max_tokens": args.get("max_tokens"),
        "timeout_sec": args.get("per_call_timeout") or 180,
        "project_root": root,
        **policy_args,
    }
    capability = step["capability"]
    if capability == "chat":
        return _chat({**common, "prompt": context})
    if capability == "search":
        return _search(
            {
                "provider": provider,
                "query": context,
                "max_tokens": args.get("max_tokens"),
                "timeout_sec": args.get("per_call_timeout") or 180,
            }
        )
    if capability == "write":
        source = "\n\n".join(str(result.get("text") or "") for result in dependencies.values())
        return _write(
            {
                **common,
                "task": "custom",
                "instruction": context,
                "source_text": source or None,
            }
        )
    if capability == "review_diff":
        return _review_diff(
            {
                **common,
                "cwd": root,
                "instruction": step["instruction"],
                "focus": context if dependencies else None,
                "require_complete": True,
                "include_untracked": True,
            }
        )
    if capability == "compare":
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
                "providers": step.get("participants") or ["claude", "grok", "gemini"],
                "project_root": root,
                "execution": "parallel",
                "max_concurrency": args.get("max_concurrency") or 3,
                "consistency": gate,
                "max_tokens": args.get("max_tokens"),
                "timeout_sec": args.get("per_call_timeout") or 180,
                **policy_args,
            }
        )
    if capability == "verify":
        verified = _verify(
            {"text": context, "doc_class": "durable", "project_root": root}
        )
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
        plan = _adaptive_plan(args)
        return envelope(
            "plan_workflow",
            {
                "success": True,
                "text": f"Adaptive plan ready with {len(plan['steps'])} LLM-chosen steps.",
                "workflow_id": "adaptive",
                "dynamic": True,
                "plan": plan,
            },
        )
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
    if _is_adaptive(args):
        plan = _adaptive_plan(args)
        return envelope(
            "start_workflow",
            {
                "success": True,
                "text": "Adaptive plan is ready for review; run it with agent_hub_run_workflow.",
                "workflow_id": "adaptive",
                "dynamic": True,
                "status": "planned",
                "plan": plan,
                "next_action": {
                    "type": "call_tool",
                    "tool": "agent_hub_run_workflow",
                    "arguments": {
                        "workflow_id": "adaptive",
                        "plan": plan,
                        "project_root": str(args.get("project_root") or "."),
                    },
                },
            },
        )
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

    if _is_adaptive(args):
        plan = _adaptive_plan(args)
        executable = {
            key: plan.get(key) for key in ("schema", "goal", "rationale", "steps")
        }
        result = orchestrator.execute_plan(
            executable,
            invoke=lambda step, provider, dependencies: _adaptive_step_call(
                dict(step), provider, dict(dependencies), args=args, goal=plan["goal"]
            ),
            max_concurrency=int(args.get("max_concurrency") or 3),
            max_calls=int(args.get("max_leaf_calls") or broker.DEFAULT_MAX_LEAF_CALLS),
        )
        return envelope(
            "run_workflow",
            {
                **result,
                "workflow_id": "adaptive",
                "dynamic": True,
                "planner": plan.get("planner"),
            },
        )
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
        **POLICY_CONTROL_SCHEMA,
    },
)
SEARCH_SCHEMA = _operation_schema(
    google_mcp.GROUNDING_SCHEMA,
    {
        "source": {"type": "string", "enum": ["web", "x", "both"], "default": "web"},
        "allowed_domains": {"type": "array", "items": {"type": "string"}},
        "blocked_domains": {"type": "array", "items": {"type": "string"}},
        "allowed_x_handles": {"type": "array", "items": {"type": "string"}},
        "from_date": {"type": "string"},
        "to_date": {"type": "string"},
        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 131072},
    },
)
WRITING_SCHEMA = _operation_schema(google_mcp.WRITING_SCHEMA, POLICY_CONTROL_SCHEMA)
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
            "maxItems": 3,
        },
        "system": {"type": "string"},
        "project_root": {"type": "string"},
        **POLICY_CONTROL_SCHEMA,
        "execution": {
            "type": "string",
            "enum": ["parallel", "sequential"],
            "default": "parallel",
        },
        "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 3, "default": 3},
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
                "min_responses": {"type": "integer", "minimum": 2, "maximum": 3, "default": 2},
                "project_root": {"type": "string"},
                **POLICY_CONTROL_SCHEMA,
            },
            "required": ["decision_labels"],
            "additionalProperties": False,
        },
    },
)
REVIEW_DIFF_SCHEMA = _operation_schema(google_mcp.REVIEW_DIFF_SCHEMA, POLICY_CONTROL_SCHEMA)
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
    google_mcp.RELEASE_DRAFT_SCHEMA, POLICY_CONTROL_SCHEMA
)
WORKFLOW_BASE = {
    "workflow_id": {"type": "string"},
    "preset": {"type": "string"},
    "prompt": {"type": "string"},
    "instruction": {"type": "string"},
    "project_root": {"type": "string", "default": "."},
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
        "maximum": 2,
        "default": 1,
    },
    "planner_max_tokens": {
        "type": "integer",
        "minimum": 256,
        "maximum": 131072,
        "default": 12000,
    },
    "max_steps": {
        "type": "integer",
        "minimum": 1,
        "maximum": orchestrator.MAX_PLAN_STEPS,
        "default": orchestrator.MAX_PLAN_STEPS,
    },
    "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 3, "default": 3},
    **POLICY_CONTROL_SCHEMA,
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
        _with_provider(CHAT_SCHEMA),
        read_only=False,
        open_world=True,
    ),
    _spec(
        "agent_hub_search",
        "Grounded Search",
        "Run a source-backed search operation.",
        _with_supported_provider(SEARCH_SCHEMA, PROVIDERS),
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
        _object(WORKFLOW_BASE, required=("workflow_id",), additional=True),
        read_only=False,
        open_world=True,
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
