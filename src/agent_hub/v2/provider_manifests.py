"""Built-in v2 provider manifests without importing provider credentials."""

from __future__ import annotations

from typing import Any

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
    "claude": ["api.anthropic.com"],
    "grok": ["api.x.ai"],
    "gemini": [
        "accounts.google.com",
        "cloudcode-pa.googleapis.com",
        "oauth2.googleapis.com",
    ],
    "gpt": ["chatgpt.com"],
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
