"""Antigravity provider adapter — uses the shared core streaming side-channel.

Its dispatch already returns an MCP-shaped {content, structuredContent, isError}
result. For streaming chat it forwards the server-provided ``progress`` emitter
(core.mcp.emit_notification); modern-protocol completion is applied server-side,
so this leaf is now a peer adapter rather than a special delegated case.
"""

from __future__ import annotations

from typing import Any, Dict, List

from google_antigravity_codex import mcp_server as _agy

from agent_hub.core.rpc import RpcError

from .base import Provider


class AntigravityProvider(Provider):
    name = "antigravity"

    def tool_specs(self) -> List[Dict[str, Any]]:
        return _agy.tool_definitions()

    def call(self, name: str, arguments: Any, progress=None) -> Dict[str, Any]:
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "tool arguments must be an object")
        stream = name == "google_antigravity_chat" and bool(arguments.get("stream"))
        return _agy.dispatch_tool(name, arguments, progress=progress if stream else None)


antigravity_provider = AntigravityProvider()
