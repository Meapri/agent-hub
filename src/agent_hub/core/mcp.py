"""Shared MCP protocol mechanics: streaming side-channel + modern-protocol
negotiation + discovery/result completion.

Lifted from the antigravity leaf (the only one that implemented them) so they
become server-wide capabilities: every provider adapter can stream and every
tool is reachable under the stateless modern protocol, not just one leaf.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Sequence

from .rpc import RpcError

MODERN_PROTOCOL_VERSION = "2026-07-28"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
DISCOVERY_TTL_MS = 300_000
MODERN_META_PROTOCOL = "io.modelcontextprotocol/protocolVersion"
MODERN_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
MODERN_META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"


def emit_notification(method: str, params: Dict[str, Any]) -> None:
    """Write a JSON-RPC notification to stdout (MCP stream/progress side-channel)."""
    print(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False), flush=True)


def modern_request_protocol(message: Dict[str, Any], supported: Sequence[str]) -> str:
    """Return the negotiated protocol version from the request _meta (or '')."""
    params = message.get("params") or {}
    if not isinstance(params, dict):
        raise RpcError(-32602, "params must be an object")
    metadata = params.get("_meta") or {}
    if not isinstance(metadata, dict):
        raise RpcError(-32602, "params._meta must be an object")
    version = str(metadata.get(MODERN_META_PROTOCOL) or "")
    if not version:
        if message.get("method") == "server/discover":
            raise RpcError(-32602, "server/discover requires the modern protocol _meta")
        return ""
    if version not in supported:
        raise RpcError(
            -32022,
            f"Unsupported MCP protocol version: {version}",
            data={"supported": list(supported), "requested": version},
        )
    if version == MODERN_PROTOCOL_VERSION:
        missing = [
            key
            for key in (MODERN_META_CLIENT_INFO, MODERN_META_CLIENT_CAPABILITIES)
            if key not in metadata
        ]
        if missing:
            raise RpcError(-32602, f"modern MCP _meta is missing: {', '.join(missing)}")
    return version


def complete_modern_result(method: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Add stateless modern-protocol result fields (resultType, cache hints)."""
    modern = dict(result)
    modern.setdefault("resultType", "complete")
    if method in ("tools/list", "server/discover"):
        modern.setdefault("ttlMs", DISCOVERY_TTL_MS)
        modern.setdefault("cacheScope", "public")
    return modern
