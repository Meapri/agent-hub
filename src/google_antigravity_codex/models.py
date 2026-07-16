"""Model listing helpers."""

from __future__ import annotations

from typing import Any, Dict, List

from . import provider, response


def _static_model_catalog() -> List[Dict[str, str]]:
    # Curated catalog aligned with hermes-google-antigravity fallback_models
    # plus stable Gemini display aliases used by agy models.
    return [
        {"id": model_id, "source": "static"}
        for model_id in (
            "gemini-3.5-flash-high",
            "gemini-3.5-flash-medium",
            "gemini-3.5-flash-low",
            "gemini-3.1-pro-high",
            "gemini-3.1-pro-low",
            "gemini-3-flash-high",
            "gemini-3-flash-low",
            "claude-opus-4-6-thinking",
            "claude-sonnet-4-6-thinking",
            "gpt-oss-120b",
            "gemini-3.5-flash",
            "gemini-3.1-pro-preview",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        )
    ]


def _provider_models() -> Dict[str, Any]:
    state = provider.status(probe=False)
    visible = provider.list_models()
    text_models: List[Dict[str, Any]] = []
    image_models: List[Dict[str, Any]] = []
    source = str(state.get("provider") or "agy-session")
    for item in visible:
        methods = {str(value).lower() for value in item.get("methods", [])}
        model_id = str(item.get("id") or "")
        is_image = "generateimages" in methods or "image" in model_id.lower()
        normalized = {
            "id": model_id,
            "display": str(item.get("display") or model_id),
            "source": source,
        }
        if is_image:
            image_models.append(normalized)
        else:
            text_models.append(normalized)
    return {
        "text": f"Listed {len(text_models)} text models and {len(image_models)} image models.",
        "text_models": text_models,
        "image_models": image_models,
        "source": source,
        **response.standard_fields(
            backend=str(state.get("backend") or "agy-session"),
            warnings=[] if visible else ["provider_model_list_empty"],
        ),
    }


def list_models(_: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if provider.configured():
        return _provider_models()
    text_models = _static_model_catalog()
    image_models: List[Dict[str, str]] = []
    return {
        "text": f"Listed {len(text_models)} text models and {len(image_models)} image models (static fallback).",
        "text_models": text_models,
        "image_models": image_models,
        "source": "static_fallback",
        **response.standard_fields(
            backend="static-model-catalog",
            warnings=["oauth_not_configured", "model_list_static_fallback"],
        ),
    }
