"""Static + live model listing."""

from __future__ import annotations

from typing import Any, Dict, List

from . import api, auth, response, security

DEFAULT_MODEL = "grok-4.5"
CURATED: List[Dict[str, str]] = [
    {"id": "grok-4.5", "display": "Grok 4.5", "source": "curated"},
    {"id": "grok-4.3", "display": "Grok 4.3", "source": "curated"},
    {"id": "grok-4.20-0309-reasoning", "display": "Grok 4.20 Reasoning", "source": "curated"},
    {"id": "grok-4.20-0309-non-reasoning", "display": "Grok 4.20", "source": "curated"},
    {"id": "grok-4.20-multi-agent-0309", "display": "Grok 4.20 Multi-Agent", "source": "curated"},
    {"id": "grok-build-0.1", "display": "Grok Build 0.1", "source": "curated"},
    {"id": "grok-imagine-image", "display": "Grok Imagine Image", "source": "curated"},
    {
        "id": "grok-imagine-image-quality",
        "display": "Grok Imagine Image Quality",
        "source": "curated",
    },
]


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
            warnings.append(f"live_list_failed:{type(exc).__name__}")
    models = live or list(CURATED)
    image_models = [item for item in models if "imagine-image" in str(item.get("id") or "")]
    text_models = [item for item in models if item not in image_models and "imagine-video" not in str(item.get("id") or "")]
    return {
        "text": f"{len(models)} models (source={source})",
        "source": source,
        "default_model": DEFAULT_MODEL,
        "models": models,
        "text_models": text_models,
        "image_models": image_models,
        **response.standard_fields(
            provider="xai",
            backend="xai-chat-completions",
            warnings=warnings,
        ),
    }
