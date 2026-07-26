"""Provider capability truth for the unified Agent Hub surface.

Capabilities describe what the adapters implement, not what a vendor product
might support somewhere else.  Keeping this table close to the operation
registry prevents the README and automatic routing from drifting apart.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

from agent_hub import provider_registry

CAPABILITIES: Dict[str, Dict[str, Dict[str, Any]]] = {
    provider: deepcopy(dict(item.capabilities))
    for provider, item in provider_registry.MANIFESTS.items()
}
CAPABILITIES.update(
    {
        "local": {
            "release_snapshot": {"supported": True, "native": True},
        },
    }
)


def provider_capabilities(provider: str) -> Dict[str, Dict[str, Any]]:
    return deepcopy(CAPABILITIES.get(provider, {}))


def supports(provider: str, capability: str) -> bool:
    return bool(CAPABILITIES.get(provider, {}).get(capability, {}).get("supported"))


def require(provider: str, capability: str) -> None:
    if supports(provider, capability):
        return
    detail = CAPABILITIES.get(provider, {}).get(capability, {})
    reason = str(detail.get("reason") or "adapter does not implement this capability")
    raise ValueError(f"{provider} does not support {capability}: {reason}")


def supports_reasoning_effort(provider: str, model: str) -> bool:
    """Return the selected adapter/model's actual reasoning control support."""

    normalized_provider = provider_registry.normalize(provider)
    normalized_model = str(model or "").strip()
    if not normalized_model:
        return False
    if normalized_provider == "claude":
        from claude_codex import chat

        return chat.supports_reasoning_effort(normalized_model)
    if normalized_provider == "grok":
        from grok_codex import chat

        return chat.supports_reasoning_effort(normalized_model)
    if normalized_provider == "gemini":
        from google_antigravity_codex import chat

        return chat.supports_thinking_level(normalized_model)
    if normalized_provider == "gpt":
        return True
    return False
