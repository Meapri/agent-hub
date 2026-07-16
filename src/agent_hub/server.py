"""Unified single MCP server (Phase 1: fan-in multiplexer).

One JSON-RPC/stdio process exposes every tool from the four co-located packages.
`tools/call` is delegated to the *owning* package's ``handle_request`` verbatim,
so the result envelope (content[] + isError, and orchestrate's load-bearing
top-level payload spread) is produced by the original, tested code — no behavior
changes. Tool names keep their existing prefixes (they are already globally
unique and orchestrate's routing keys off the exact strings).

Later phases swap each registry target from the legacy package to an
``agent_hub.providers`` adapter, one at a time, with the tool names and result
shapes held byte-stable.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, List, Optional

from claude_codex import mcp_server as _claude
from grok_codex import mcp_server as _grok
from google_antigravity_codex import mcp_server as _antigravity
from orchestrate_codex import mcp_server as _orchestrate

from . import __version__

SERVER_NAME = "agent-hub"
SERVER_VERSION = __version__
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

# Fixed registration order. Orchestrate owns prompts/resources.
_MODULES = [_orchestrate, _claude, _grok, _antigravity]
_ORCHESTRATE = _orchestrate


class RpcError(ValueError):
    """JSON-RPC error with a numeric code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _build_registry() -> Dict[str, Any]:
    """Map every tool name to the module that owns it (fail loud on collision)."""
    registry: Dict[str, Any] = {}
    for module in _MODULES:
        for spec in module.tool_definitions():
            name = spec["name"]
            if name in registry:
                raise RuntimeError(
                    f"tool name collision: {name} in {module.__name__} and "
                    f"{registry[name].__name__}"
                )
            registry[name] = module
    return registry


_REGISTRY: Dict[str, Any] = _build_registry()


def tool_definitions() -> List[Dict[str, Any]]:
    """Merged tool list across all packages, in registration order."""
    merged: List[Dict[str, Any]] = []
    for module in _MODULES:
        merged.extend(module.tool_definitions())
    return merged


def _supported_protocol_versions() -> set:
    versions: set = {DEFAULT_PROTOCOL_VERSION}
    for module in _MODULES:
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
        # Notification (e.g. notifications/initialized) — no response.
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
            # Delegate to the owning package verbatim so its exact result
            # assembly (content[]/isError, orchestrate's top-level spread) runs.
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
