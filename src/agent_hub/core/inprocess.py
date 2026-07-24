"""In-process leaf transport for the orchestrate broker.

Replaces subprocess spawn + JSON-RPC with a direct call to the owning provider
adapter, then reuses the leaf_client result interpretation so the broker sees an
identical structured result. CRITICAL invariant: consent is enforced *inside* each
adapter/dispatch handler, so an in-process orchestrated call has NO privileged
bypass — a revoked-consent leaf tool still fails.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, Dict, Optional, Tuple

from agent_hub import provider_registry
from orchestrate_codex.leaf_client import _interpret_result
from orchestrate_codex.results import OperationResult


class InProcessLeafClient:
    """Same legacy and structured contracts as ``leaf_client.LeafClient``."""

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def call_tool(
        self, tool: str, arguments: Dict[str, Any], *, timeout: Optional[float] = None
    ) -> Tuple[bool, str]:
        result = self.call_tool_result(tool, arguments, timeout=timeout)
        return result.success, result.text

    def call_tool_result(
        self, tool: str, arguments: Dict[str, Any], *, timeout: Optional[float] = None
    ) -> OperationResult:
        try:
            result = self._owner.call(tool, arguments or {})
        except Exception as exc:  # noqa: BLE001 — a raised tool/consent error maps to (ok=False),
            return OperationResult.from_result(
                {},
                success=False,
                text=str(exc),
                error={"type": type(exc).__name__, "message": str(exc)},
            )
        return _interpret_result(result)


def make_resolver() -> Callable[[str], Optional[InProcessLeafClient]]:
    """Resolve a leaf tool name to an in-process client backed by its adapter.

    Leaf names are intentionally kept in a private registry. They are execution
    details for workflows and are not exposed by the public Agent Hub MCP server.
    """
    registry: Dict[str, Any] = {}
    for item in provider_registry.MANIFESTS.values():
        module_name, separator, attribute = item.adapter.partition(":")
        if not separator:
            raise RuntimeError(f"invalid provider adapter reference: {item.adapter}")
        owner = getattr(import_module(module_name), attribute)
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
