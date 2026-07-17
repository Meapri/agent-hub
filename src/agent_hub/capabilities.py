"""Provider capability truth for the unified Agent Hub surface.

Capabilities describe what the adapters implement, not what a vendor product
might support somewhere else.  Keeping this table close to the operation
registry prevents the README and automatic routing from drifting apart.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


CAPABILITIES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "claude": {
        "chat": {"supported": True},
        "vision": {"supported": True},
        "search": {"supported": True, "native": True, "auth_note": "API entitlement required"},
        "write": {"supported": True},
        "image_generation": {"supported": False, "reason": "Claude models return text"},
        "compare": {"supported": True},
        "review_diff": {"supported": True},
        "release_draft": {"supported": True},
        "settings": {"supported": True, "scope": ["model", "temperature", "max_tokens"]},
    },
    "grok": {
        "chat": {"supported": True},
        "vision": {"supported": True},
        "search": {"supported": True, "native": True, "auth_note": "API entitlement required"},
        "write": {"supported": True},
        "image_generation": {"supported": True, "native": True, "auth_note": "API entitlement required"},
        "compare": {"supported": True},
        "review_diff": {"supported": True},
        "release_draft": {"supported": True},
        "settings": {
            "supported": True,
            "scope": ["model", "temperature", "max_tokens", "api_mode"],
        },
    },
    "gemini": {
        "chat": {"supported": True},
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
    "local": {
        "release_snapshot": {"supported": True, "native": True},
    },
}


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

