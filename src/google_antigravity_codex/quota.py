"""agy provider status exposed through the historical quota tool name.

The supported agy transports do not expose a unified quota-bucket response.
Returning an invented value would be misleading, so this reports provider
readiness and points users to the provider's own billing and quota controls.
"""

from __future__ import annotations

from typing import Any, Dict

from . import provider, response


def quota_status(_: Dict[str, Any] | None = None) -> Dict[str, Any]:
    state = provider.status(probe=True)
    ready = bool(state.get("healthy"))
    if not state.get("configured"):
        text = "A model provider is not configured; quota is unavailable."
        warnings = ["agy_provider_not_configured", "quota_not_exposed_by_agy_provider"]
    elif ready:
        text = "The selected model provider is ready; quota buckets are not exposed by this tool."
        warnings = ["quota_not_exposed_by_agy_provider"]
    else:
        text = "The selected model provider is not ready; quota is unavailable."
        warnings = [
            str(state.get("error_type") or "agy_provider_not_ready"),
            "quota_not_exposed_by_agy_provider",
        ]
    return {
        "text": text,
        "provider_status": state,
        "buckets": [],
        "quota_available": False,
        **response.standard_fields(
            success=state.get("healthy") is True,
            backend=str(state.get("backend") or "agy-session"),
            warnings=warnings,
        ),
    }
