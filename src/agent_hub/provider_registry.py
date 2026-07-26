"""Static provider metadata shared by routing, schemas, and private leaf loading.

The registry deliberately contains no authentication or HTTP callbacks.  A
provider may use an API key, its own OAuth store, or an official local broker;
the public Agent Hub contract stays the same while each adapter owns its
security boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Tuple


@dataclass(frozen=True)
class ProviderManifest:
    """Metadata that is safe to import without initializing a provider."""

    id: str
    aliases: Tuple[str, ...]
    model_prefixes: Tuple[str, ...]
    capabilities: Mapping[str, Mapping[str, Any]]
    chat_tool: str
    settings_fields: Tuple[str, ...]
    planner_enabled: bool = True
    default_compare: bool = True


_MANIFESTS: Tuple[ProviderManifest, ...] = (
    ProviderManifest(
        id="claude",
        aliases=("anthropic",),
        model_prefixes=("claude",),
        capabilities={
            "chat": {"supported": True, "reasoning_effort": ["low", "medium", "high"]},
            "vision": {"supported": True},
            "search": {
                "supported": True,
                "native": True,
                "auth_note": "API entitlement required",
            },
            "write": {"supported": True},
            "image_generation": {
                "supported": False,
                "reason": "Claude models return text",
            },
            "compare": {"supported": True},
            "review_diff": {"supported": True},
            "release_draft": {"supported": True},
            "settings": {
                "supported": True,
                "scope": ["model", "temperature", "max_tokens"],
            },
        },
        chat_tool="claude_codex_chat",
        settings_fields=("model", "temperature", "max_tokens"),
    ),
    ProviderManifest(
        id="grok",
        aliases=("xai",),
        model_prefixes=("grok",),
        capabilities={
            "chat": {"supported": True, "reasoning_effort": ["low", "medium", "high"]},
            "vision": {"supported": True},
            "search": {
                "supported": True,
                "native": True,
                "auth_note": "API entitlement required",
            },
            "write": {"supported": True},
            "image_generation": {
                "supported": True,
                "native": True,
                "auth_note": "API entitlement required",
            },
            "compare": {"supported": True},
            "review_diff": {"supported": True},
            "release_draft": {"supported": True},
            "settings": {
                "supported": True,
                "scope": ["model", "temperature", "max_tokens", "api_mode"],
            },
        },
        chat_tool="grok_codex_chat",
        settings_fields=("model", "temperature", "max_tokens", "api_mode"),
    ),
    ProviderManifest(
        id="gemini",
        aliases=("google", "antigravity", "google-antigravity"),
        model_prefixes=("gemini", "models/gemini"),
        capabilities={
            "chat": {"supported": True, "reasoning_effort": ["low", "medium", "high"]},
            "vision": {"supported": True},
            "search": {"supported": True, "native": True},
            "write": {"supported": True},
            "image_generation": {"supported": True, "native": True},
            "compare": {"supported": True},
            "review_diff": {"supported": True},
            "release_draft": {"supported": True},
            "settings": {
                "supported": True,
                "scope": ["model", "transport", "profile", "temperature", "max_tokens"],
            },
        },
        chat_tool="google_antigravity_chat",
        settings_fields=("model", "transport", "profile", "temperature", "max_tokens"),
    ),
    ProviderManifest(
        id="gpt",
        aliases=("codex", "chatgpt", "openai-codex"),
        model_prefixes=("gpt", "codex"),
        capabilities={
            "chat": {
                "supported": True,
                "reasoning_effort": ["low", "medium", "high", "xhigh", "max", "ultra"],
            },
            "vision": {"supported": True, "remote_images": False},
            "search": {
                "supported": False,
                "reason": "The isolated GPT leaf disables web search",
            },
            "write": {"supported": True},
            "image_generation": {
                "supported": False,
                "reason": "Codex exec returns text",
            },
            "compare": {"supported": True},
            "review_diff": {"supported": True},
            "release_draft": {"supported": True},
            "settings": {"supported": True, "scope": ["model"]},
        },
        chat_tool="openai_codex_chat",
        settings_fields=("model",),
        planner_enabled=True,
        default_compare=False,
    ),
)

MANIFESTS: Mapping[str, ProviderManifest] = {item.id: item for item in _MANIFESTS}
AVAILABLE_PROVIDERS: Tuple[str, ...] = tuple(MANIFESTS)
DEFAULT_COMPARE_PROVIDERS: Tuple[str, ...] = tuple(
    item.id for item in _MANIFESTS if item.default_compare
)
PLANNER_PROVIDERS: Tuple[str, ...] = tuple(item.id for item in _MANIFESTS if item.planner_enabled)
ALIASES: Mapping[str, str] = {alias: item.id for item in _MANIFESTS for alias in item.aliases}


def manifest(provider: str) -> ProviderManifest:
    try:
        return MANIFESTS[str(provider)]
    except KeyError as exc:
        raise ValueError(f"unknown provider: {provider}") from exc


def normalize(
    value: Any,
    *,
    allow_all: bool = False,
    allow_auto: bool = False,
    default: str = "",
) -> str:
    provider = str(value or default).strip().lower()
    provider = ALIASES.get(provider, provider)
    allowed = set(AVAILABLE_PROVIDERS)
    if allow_all:
        allowed.add("all")
    if allow_auto:
        allowed.add("auto")
    if provider not in allowed:
        raise ValueError(f"provider must be one of: {', '.join(sorted(allowed))}")
    return provider


def providers_supporting(capability: str, *, planner_only: bool = False) -> Tuple[str, ...]:
    allowed = PLANNER_PROVIDERS if planner_only else AVAILABLE_PROVIDERS
    return tuple(
        provider
        for provider in allowed
        if bool(MANIFESTS[provider].capabilities.get(capability, {}).get("supported"))
    )


def provider_for_model(model: str, *, default: str = "gemini") -> str:
    lowered = str(model or "").strip().lower()
    for item in _MANIFESTS:
        if any(lowered.startswith(prefix) for prefix in item.model_prefixes):
            return item.id
    return default


def chat_tools(*, planner_only: bool = False) -> Tuple[str, ...]:
    providers: Iterable[str] = PLANNER_PROVIDERS if planner_only else AVAILABLE_PROVIDERS
    return tuple(MANIFESTS[provider].chat_tool for provider in providers)
