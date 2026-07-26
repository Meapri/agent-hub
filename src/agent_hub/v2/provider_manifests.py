"""Built-in v2 provider manifests without importing provider credentials."""

from __future__ import annotations

from typing import Any, Mapping

from agent_hub import __version__
from agent_hub.provider_registry import MANIFESTS

from .contracts import PROVIDER_MANIFEST_SCHEMA, validate_provider_manifest

_AUTH = {
    "claude": ("claude-code", "subscription_oauth"),
    "grok": ("agent-hub", "subscription_oauth"),
    "gemini": ("agent-hub", "plugin_oauth_login"),
    "gpt": ("codex", "chatgpt"),
}
_DOMAINS = {
    "claude": [
        "api.anthropic.com",
        "platform.claude.com",
        "console.anthropic.com",
    ],
    "grok": ["api.x.ai"],
    "gemini": [
        "accounts.google.com",
        "cloudcode-pa.googleapis.com",
        "oauth2.googleapis.com",
    ],
    "gpt": ["chatgpt.com"],
}
_CONTEXT_LIMITS = {
    # Anthropic Models API/docs: current long-context models expose 1M;
    # unlisted and older models fall back to the conservative 200k window.
    "claude": {
        "default_max_input_tokens": 200_000,
        "model_overrides": [
            {"model_prefix": model_prefix, "max_input_tokens": 1_000_000}
            for model_prefix in (
                "claude-opus-5",
                "claude-fable-5",
                "claude-sonnet-5",
                "claude-opus-4-8",
                "claude-opus-4-7",
                "claude-opus-4-6",
                "claude-sonnet-4-6",
            )
        ],
    },
    # xAI publishes 500k for Grok 4.5 and 1M for Grok 4.20.
    "grok": {
        "default_max_input_tokens": 256_000,
        "model_overrides": [
            {"model_prefix": "grok-4.20", "max_input_tokens": 1_000_000},
            {"model_prefix": "grok-4.5", "max_input_tokens": 500_000},
        ],
    },
    # Antigravity can expose non-Gemini backends. Unknown IDs therefore use
    # the shared conservative default instead of inheriting Gemini's 1M limit.
    "gemini": {
        "default_max_input_tokens": 131_072,
        "model_overrides": [
            {"model_prefix": "gemini-", "max_input_tokens": 1_048_576},
        ],
    },
    # Codex reserves 5% of its catalog context for harness/output overhead.
    "gpt": {
        "default_max_input_tokens": 121_600,
        "model_overrides": [
            {"model_prefix": "gpt-5.6-", "max_input_tokens": 258_400},
            {"model_prefix": "gpt-5.5", "max_input_tokens": 258_400},
            {"model_prefix": "gpt-5.4", "max_input_tokens": 258_400},
            {"model_prefix": "gpt-5.3-codex-spark", "max_input_tokens": 121_600},
        ],
    },
}


def _v2_capability(name: str) -> str | None:
    return {
        "image_generation": "image",
        "review_diff": "review",
        "release_draft": "write",
        "compare": "decide",
    }.get(name, name if name in {"chat", "vision", "search", "write"} else None)


def builtin_provider_manifests() -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    for manifest in MANIFESTS.values():
        capabilities = []
        efforts: list[str] = []
        for name, details in manifest.capabilities.items():
            mapped = _v2_capability(name)
            if mapped and details.get("supported") is True:
                capabilities.append(mapped)
            if name == "chat":
                efforts = list(details.get("reasoning_effort") or [])
        auth_owner, auth_mode = _AUTH[manifest.id]
        values.append(
            validate_provider_manifest(
                {
                    "schema": PROVIDER_MANIFEST_SCHEMA,
                    "provider_id": manifest.id,
                    "adapter_version": __version__,
                    "protocol_version": "2.0",
                    "capabilities": capabilities,
                    "reasoning_effort": efforts,
                    "auth_owner": auth_owner,
                    "auth_mode": auth_mode,
                    "allowed_domains": _DOMAINS[manifest.id],
                    "supports_cancel": manifest.id in {"grok", "gemini"},
                    "supports_streaming": manifest.id in {"gemini"},
                    "supports_idempotency": False,
                    "context_limits": _CONTEXT_LIMITS[manifest.id],
                    "settings_schema": {
                        "type": "object",
                        "properties": {
                            field: {"type": ["string", "number", "null"]}
                            for field in manifest.settings_fields
                        },
                        "additionalProperties": False,
                    },
                }
            )
        )
    return tuple(values)


def manifest_for(provider_id: str) -> dict[str, Any]:
    for manifest in builtin_provider_manifests():
        if manifest["provider_id"] == provider_id:
            return manifest
    raise KeyError(provider_id)


def model_input_limit(
    provider_id: str,
    model: str | None = None,
    *,
    observed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one model's safe input limit from its provider manifest."""

    manifest = manifest_for(provider_id)
    limits = manifest["context_limits"]
    selected_model = str(model or "")
    if (
        isinstance(observed, Mapping)
        and observed.get("provider") == provider_id
        and observed.get("model") == selected_model
        and isinstance(observed.get("max_input_tokens"), int)
        and not isinstance(observed.get("max_input_tokens"), bool)
        and 1 <= int(observed["max_input_tokens"]) <= 10_000_000
    ):
        return {
            "provider": provider_id,
            "model": selected_model or None,
            "max_input_tokens": int(observed["max_input_tokens"]),
            "source": str(observed.get("source") or "live_catalog"),
            "matched_prefix": None,
            "catalog_revision": str(observed.get("catalog_revision") or "") or None,
        }
    for override in limits["model_overrides"]:
        if selected_model.startswith(override["model_prefix"]):
            return {
                "provider": provider_id,
                "model": selected_model or None,
                "max_input_tokens": int(override["max_input_tokens"]),
                "source": "model_override",
                "matched_prefix": override["model_prefix"],
            }
    return {
        "provider": provider_id,
        "model": selected_model or None,
        "max_input_tokens": int(limits["default_max_input_tokens"]),
        "source": "provider_default",
        "matched_prefix": None,
    }
