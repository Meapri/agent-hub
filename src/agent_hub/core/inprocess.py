"""In-process leaf transport for the orchestrate broker.

Replaces subprocess spawn + JSON-RPC with a direct call to the owning provider
adapter, then reuses the leaf_client result interpretation so the broker sees an
identical ``(ok, text)``. CRITICAL invariant: consent is enforced *inside* each
adapter/dispatch handler, so an in-process orchestrated call has NO privileged
bypass — a revoked-consent leaf tool still fails.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from orchestrate_codex.leaf_client import _interpret_result

class InProcessLeafClient:
    """Same call_tool(name, args) -> (ok, text) contract as leaf_client.LeafClient."""

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def call_tool(
        self, tool: str, arguments: Dict[str, Any], *, timeout: Optional[float] = None
    ) -> Tuple[bool, str]:
        try:
            result = self._owner.call(tool, arguments or {})
        except Exception as exc:  # noqa: BLE001 — a raised tool/consent error maps to (ok=False),
            return False, str(exc)  # exactly as a subprocess leaf's JSON-RPC error would.
        return _interpret_result(result)


def make_resolver() -> Callable[[str], Optional[InProcessLeafClient]]:
    """Resolve a leaf tool name to an in-process client backed by its adapter.

    Leaf names are intentionally kept in a private registry. They are execution
    details for workflows and are not exposed by the public Agent Hub MCP server.
    """
    from agent_hub.providers.antigravity import antigravity_provider
    from agent_hub.providers.claude import claude_provider
    from agent_hub.providers.grok import grok_provider

    registry: Dict[str, Any] = {}
    for owner in (claude_provider, grok_provider, antigravity_provider):
        for spec in owner.tool_specs():
            name = str(spec["name"])
            if name in registry:
                raise RuntimeError(f"internal leaf tool collision: {name}")
            registry[name] = owner

    def resolve(tool: str) -> Optional[InProcessLeafClient]:
        owner = registry.get(tool)
        if owner is None:
            return None
        return InProcessLeafClient(owner)

    return resolve
