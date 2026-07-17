"""Unified single MCP server exposing only the canonical Agent Hub API."""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

from . import __version__
from .core import mcp as _mcp
from .core.rpc import RpcError
from .providers.base import Provider
from .providers.hub import hub_provider

from orchestrate_codex import mcp_server as _orchestrate  # for prompts/resources delegation

SERVER_NAME = "agent-hub"
SERVER_VERSION = __version__
DEFAULT_PROTOCOL_VERSION = _mcp.DEFAULT_PROTOCOL_VERSION
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_PROTOCOL_VERSIONS = (_mcp.MODERN_PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS)

_OWNERS: List[Provider] = [hub_provider]
_ORCHESTRATE = _orchestrate


def _build_registry() -> Dict[str, Provider]:
    registry: Dict[str, Provider] = {}
    for owner in _OWNERS:
        for spec in owner.tool_specs():
            name = spec["name"]
            if name in registry:
                raise RuntimeError(f"tool name collision: {name}")
            registry[name] = owner
    return registry


_REGISTRY: Dict[str, Provider] = _build_registry()


def tool_definitions() -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for owner in _OWNERS:
        merged.extend(owner.tool_specs())
    return merged


def _discovery_result() -> Dict[str, Any]:
    return {
        "resultType": "complete",
        "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "Use the agent_hub_* tools for the stable cross-provider API. "
            "Provider-specific leaf tools are internal implementation details."
        ),
        "ttlMs": _mcp.DISCOVERY_TTL_MS,
        "cacheScope": "public",
    }


def handle_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    request_id = message.get("id")
    if request_id is None:
        return None
    method = message.get("method")
    try:
        protocol = _mcp.modern_request_protocol(message, SUPPORTED_PROTOCOL_VERSIONS)
        modern = protocol == _mcp.MODERN_PROTOCOL_VERSION
        if modern and method in ("initialize", "ping"):
            raise RpcError(-32601, f"{method} is not available in stateless MCP {protocol}")
        if method == "server/discover":
            if not modern:
                raise RpcError(-32602, "server/discover requires the modern MCP protocol")
            result = _discovery_result()
        elif method == "initialize":
            params = message.get("params") or {}
            requested = str(params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION)
            selected = (
                requested if requested in LEGACY_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
            )
            result = {
                "protocolVersion": selected,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                    "resources": {"listChanged": True},
                },
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_definitions()}
        elif method == "tools/call":
            params = message.get("params") or {}
            name = str(params.get("name") or "")
            owner = _REGISTRY.get(name)
            if owner is None:
                raise RpcError(-32602, f"unknown tool: {name}")
            result = owner.call(
                name, params.get("arguments") or {}, progress=_mcp.emit_notification
            )
        elif method in ("prompts/list", "prompts/get", "resources/list", "resources/read"):
            return _ORCHESTRATE.handle_request(message)
        else:
            raise RpcError(-32601, f"unsupported method: {method}")
        if modern:
            result = _mcp.complete_modern_result(str(method), result)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except RpcError as exc:
        error: Dict[str, Any] = {"code": exc.code, "message": str(exc)}
        if getattr(exc, "data", None) is not None:
            error["data"] = exc.data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}
    except Exception as exc:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}}


def serve() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("id") is None and message.get("method"):
            continue
        response = handle_request(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
