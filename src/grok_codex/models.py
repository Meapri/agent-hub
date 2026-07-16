"""Static + live model listing."""

from __future__ import annotations

from typing import Any, Dict, List

from . import api, auth, response, security

DEFAULT_MODEL = "grok-4-fast-reasoning"
CURATED: List[Dict[str, str]] = [{'id': 'grok-4', 'display': 'Grok 4', 'source': 'curated'}, {'id': 'grok-4-fast-reasoning', 'display': 'Grok 4 Fast Reasoning', 'source': 'curated'}, {'id': 'grok-3', 'display': 'Grok 3', 'source': 'curated'}, {'id': 'grok-3-mini', 'display': 'Grok 3 Mini', 'source': 'curated'}, {'id': 'grok-2', 'display': 'Grok 2', 'source': 'curated'}]


def list_models(arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
    security.require_consent()
    arguments = arguments or {}
    probe = bool(arguments.get("probe"))
    live: List[Dict[str, Any]] = []
    source = "curated"
    warnings: List[str] = []
    if probe and auth.has_credentials():
        try:
            live = api.list_models_live()
            if live:
                source = "live"
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"live_list_failed: {type(exc).__name__}: {exc}")
    models = live or list(CURATED)
    return {
        "text": f"{len(models)} models (source={source})",
        "source": source,
        "default_model": DEFAULT_MODEL,
        "models": models,
        "text_models": models,
        "image_models": [],
        **response.standard_fields(
            provider="xai",
            backend="xai-chat-completions",
            warnings=warnings,
        ),
    }
