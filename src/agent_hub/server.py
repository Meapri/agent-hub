"""Unified single MCP server.

One JSON-RPC/stdio process exposes every tool from the co-located stack. Each
tool is owned by either a provider *adapter* (agent_hub.providers) or, until its
adapter lands, its legacy package module (delegated verbatim). Tool names keep
their existing prefixes — already globally unique and used by orchestrate's
routing — so external clients and internal routing are byte-stable.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

from google_antigravity_codex import mcp_server as _antigravity
from orchestrate_codex import mcp_server as _orchestrate

from . import __version__
from .core.rpc import RpcError
from .providers.base import Provider
from .providers.claude import claude_provider
from .providers.orchestrate import orchestrate_provider
from .providers.grok import grok_provider

SERVER_NAME = "agent-hub"
SERVER_VERSION = __version__
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

# Owners in fixed order. Adapters (Provider instances) own their tools directly;
# modules are delegated to their legacy handle_request until adapted.
# Migration flips one entry from module -> adapter at a time.
_OWNERS: List[Any] = [orchestrate_provider, claude_provider, grok_provider, _antigravity]
_ORCHESTRATE = _orchestrate


def _specs(owner: Any) -> List[Dict[str, Any]]:
    if isinstance(owner, Provider):
        return owner.tool_specs()
    return owner.tool_definitions()


def _build_registry() -> Dict[str, Any]:
    registry: Dict[str, Any] = {}
    for owner in _OWNERS:
        for spec in _specs(owner):
            name = spec["name"]
            if name in registry:
                raise RuntimeError(f"tool name collision: {name}")
            registry[name] = owner
    return registry


_REGISTRY: Dict[str, Any] = _build_registry()


def tool_definitions() -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for owner in _OWNERS:
        merged.extend(_specs(owner))
    return merged


def _supported_protocol_versions() -> set:
    versions: set = {DEFAULT_PROTOCOL_VERSION}
    for owner in _OWNERS:
        module = owner if not isinstance(owner, Provider) else None
        if module is None:
            continue
        default = getattr(module, "DEFAULT_PROTOCOL_VERSION", None)
        if isinstance(default, str):
            versions.add(default)
        for attr in ("LEGACY_PROTOCOL_VERSIONS", "MODERN_PROTOCOL_VERSIONS"):
            value = getattr(module, attr, ())
            if isinstance(value, (list, tuple)):
                versions.update(v for v in value if isinstance(v, str))
        modern = getattr(module, "MODERN_PROTOCOL_VERSION", None)
        if isinstance(modern, str):
            versions.add(modern)
    return versions


def handle_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    request_id = message.get("id")
    if request_id is None:
        return None
    method = message.get("method")
    try:
        if method == "initialize":
            params = message.get("params") or {}
            requested = str(params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION)
            supported = _supported_protocol_versions()
            selected = requested if requested in supported else DEFAULT_PROTOCOL_VERSION
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
            if isinstance(owner, Provider):
                result = owner.call(name, params.get("arguments") or {})
            else:
                # Delegate verbatim to the legacy package (its exact envelope).
                return owner.handle_request(message)
        elif method in ("prompts/list", "prompts/get", "resources/list", "resources/read"):
            return _ORCHESTRATE.handle_request(message)
        else:
            raise RpcError(-32601, f"unsupported method: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except RpcError as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": str(exc)}}
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
