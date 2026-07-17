"""Canonical Agent Hub public-tool adapter."""

from __future__ import annotations

from typing import Any, Dict, List

from agent_hub import operations
from agent_hub.core.rpc import RpcError

from .base import Provider, text_content_result


class HubProvider(Provider):
    name = "agent_hub"

    def tool_specs(self) -> List[Dict[str, Any]]:
        return operations.tool_definitions()

    def call(self, name: str, arguments: Any, progress=None) -> Dict[str, Any]:
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "tool arguments must be an object")
        payload = operations.dispatch_tool(name, arguments)
        result = text_content_result(payload)
        result["structuredContent"] = payload
        return result


hub_provider = HubProvider()
