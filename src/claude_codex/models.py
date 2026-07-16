"""Static + live model listing."""

from __future__ import annotations

from typing import Any, Dict, List

from . import api, auth, response, security

DEFAULT_MODEL = "claude-sonnet-4-6"
CURATED: List[Dict[str, str]] = [{'id': 'claude-sonnet-4-6', 'display': 'Claude Sonnet 4.6', 'source': 'curated'}, {'id': 'claude-opus-4-6', 'display': 'Claude Opus 4.6', 'source': 'curated'}, {'id': 'claude-haiku-4-5', 'display': 'Claude Haiku 4.5', 'source': 'curated'}, {'id': 'claude-sonnet-4-5', 'display': 'Claude Sonnet 4.5', 'source': 'curated'}, {'id': 'claude-opus-4-5', 'display': 'Claude Opus 4.5', 'source': 'curated'}]


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
            provider="anthropic",
            backend="anthropic-messages",
            warnings=warnings,
        ),
    }
