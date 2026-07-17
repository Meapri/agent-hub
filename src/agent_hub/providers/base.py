"""Common interface for canonical and internal provider adapters."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from agent_hub.core.rpc import RpcError


def text_content_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a leaf dispatch payload as an MCP-compliant tools/call result.

    Leaf dispatch payloads are raw structured dicts with no ``content[]`` — which
    strict MCP clients (e.g. Codex under the modern protocol) reject as a response
    format error. Add the required content[]+isError while keeping the structured
    fields spread on top for the internal workflow transport.
    """
    text = payload.get("text")
    if not isinstance(text, str):
        text = json.dumps(payload, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": not bool(payload.get("success", True)),
        **payload,
    }


class Provider:
    name: str = ""

    def tool_specs(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def tool_names(self) -> List[str]:
        return [spec["name"] for spec in self.tool_specs()]

    def call(self, name: str, arguments: Any, progress=None) -> Dict[str, Any]:
        raise NotImplementedError


class RawLeafProvider(Provider):
    """Leaf whose tools/call result is the dispatch payload verbatim (claude/grok)."""

    def __init__(self, name: str, module: Any) -> None:
        self.name = name
        self._module = module

    def tool_specs(self) -> List[Dict[str, Any]]:
        return self._module.tool_definitions()

    def call(self, name: str, arguments: Any, progress=None) -> Dict[str, Any]:
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "tool arguments must be an object")
        return text_content_result(self._module.dispatch_tool(name, arguments))
