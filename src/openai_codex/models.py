"""Model discovery through the official Codex app-server catalog."""

from __future__ import annotations

from typing import Any, Dict, List

from . import auth, client, response, security

DEFAULT_MODEL = "gpt-5.6-sol"


def list_models(arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
    security.require_consent()
    arguments = arguments or {}
    timeout = float(arguments.get("timeout_sec") or 30)
    auth.require_subscription(timeout=timeout)
    result = client.app_server_request(
        "model/list",
        {"includeHidden": bool(arguments.get("include_hidden", False)), "limit": 100},
        timeout=timeout,
    )
    include_hidden = bool(arguments.get("include_hidden", False))
    items = result.get("data") if isinstance(result.get("data"), list) else []
    models: List[Dict[str, Any]] = []
    default_model = DEFAULT_MODEL
    for item in items:
        if not isinstance(item, dict) or (item.get("hidden") is True and not include_hidden):
            continue
        model_id = str(item.get("model") or item.get("id") or "").strip()
        if not model_id:
            continue
        if item.get("isDefault"):
            default_model = model_id
        efforts = []
        for option in item.get("supportedReasoningEfforts") or []:
            if isinstance(option, dict) and option.get("reasoningEffort"):
                efforts.append(str(option["reasoningEffort"]))
        models.append(
            {
                "id": model_id,
                "display": str(item.get("displayName") or model_id),
                "description": str(item.get("description") or ""),
                "reasoning_efforts": efforts,
                "input_modalities": list(item.get("inputModalities") or []),
                "is_default": bool(item.get("isDefault")),
                "source": "codex-app-server",
            }
        )
    warnings = ["model_catalog_truncated"] if result.get("nextCursor") else []
    return {
        "text": f"{len(models)} GPT models from the signed-in Codex catalog.",
        "source": "codex-app-server",
        "default_model": default_model,
        "models": models,
        "text_models": models,
        "image_models": [],
        **response.standard_fields(
            provider="openai",
            backend="codex-app-server",
            warnings=warnings,
        ),
    }
