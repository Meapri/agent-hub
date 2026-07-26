"""Model discovery through the official Codex app-server catalog."""

from __future__ import annotations

from typing import Any, Dict, List

from . import auth, client, response, security

DEFAULT_MODEL = "gpt-5.6-sol"
MAX_CATALOG_PAGES = 5


def list_models(arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
    security.require_consent()
    arguments = arguments or {}
    timeout = float(arguments.get("timeout_sec") or 30)
    auth.require_subscription(timeout=timeout)
    include_hidden = bool(arguments.get("include_hidden", False))
    items: List[Dict[str, Any]] = []
    cursor = ""
    for _page in range(MAX_CATALOG_PAGES):
        params: Dict[str, Any] = {
            "includeHidden": include_hidden,
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor
        result = client.app_server_request(
            "model/list",
            params,
            timeout=timeout,
        )
        page = result.get("data") if isinstance(result.get("data"), list) else []
        items.extend(item for item in page if isinstance(item, dict))
        cursor = str(result.get("nextCursor") or "").strip()
        if not cursor:
            break
    models: List[Dict[str, Any]] = []
    default_model = DEFAULT_MODEL
    seen: set[str] = set()
    for item in items:
        if item.get("hidden") is True and not include_hidden:
            continue
        model_id = str(item.get("model") or item.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        if item.get("isDefault"):
            default_model = model_id
        efforts = []
        for option in item.get("supportedReasoningEfforts") or []:
            if isinstance(option, dict) and option.get("reasoningEffort"):
                efforts.append(str(option["reasoningEffort"]))
        model = {
            "id": model_id,
            "display": str(item.get("displayName") or model_id),
            "description": str(item.get("description") or ""),
            "reasoning_efforts": efforts,
            "input_modalities": list(item.get("inputModalities") or []),
            "is_default": bool(item.get("isDefault")),
            "source": "codex-app-server",
        }
        context_window = item.get("contextWindow")
        effective_percent = item.get("effectiveContextWindowPercent", 100)
        if (
            isinstance(context_window, int)
            and not isinstance(context_window, bool)
            and context_window > 0
        ):
            model["context_window_tokens"] = context_window
            if (
                not isinstance(effective_percent, int)
                or isinstance(effective_percent, bool)
                or not 1 <= effective_percent <= 100
            ):
                effective_percent = 100
            model["max_input_tokens"] = context_window * effective_percent // 100
        models.append(model)
    warnings = ["model_catalog_truncated"] if cursor else []
    return {
        "text": f"{len(models)} GPT models from the signed-in Codex catalog.",
        "source": "codex-app-server",
        "default_model": default_model,
        "models": models,
        "text_models": models,
        "image_models": [],
        **response.standard_fields(
            provider="gpt",
            backend="codex-app-server",
            warnings=warnings,
        ),
    }
