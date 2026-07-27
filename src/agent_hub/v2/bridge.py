"""Thin stdio MCP bridge to the long-lived v2 daemon."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Mapping

from agent_hub import __version__
from agent_hub.core.limits import MAX_PROVIDER_TIMEOUT_SECONDS

from .daemon import DEFAULT_SOCKET_PATH, HubDaemonClient
from .errors import HubV2Error, public_failure
from .tools import tool_definitions

SERVER_NAME = "agent-hub-v2"
PROTOCOL_VERSION = "2024-11-05"
DEFAULT_DAEMON_CALL_TIMEOUT_SECONDS = 30.0
CATALOG_DAEMON_CALL_TIMEOUT_SECONDS = 180.0


def _daemon_call_timeout(name: str, arguments: Mapping[str, Any]) -> float:
    if name == "agent_hub_plan" and arguments.get("mode") == "apply":
        return float(MAX_PROVIDER_TIMEOUT_SECONDS)
    if name == "agent_hub_execute":
        return float(MAX_PROVIDER_TIMEOUT_SECONDS)
    if name == "agent_hub_catalog":
        return (
            float(MAX_PROVIDER_TIMEOUT_SECONDS)
            if arguments.get("refresh") is True
            else CATALOG_DAEMON_CALL_TIMEOUT_SECONDS
        )
    if name in {"agent_hub_status", "agent_hub_doctor"}:
        return 60.0
    return DEFAULT_DAEMON_CALL_TIMEOUT_SECONDS


def _mcp_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": dict(payload),
        "isError": payload.get("success") is False,
    }


def _tool_list(client: HubDaemonClient) -> list[dict[str, Any]]:
    """Ask the running daemon what its tools are, rather than answering locally.

    The bridge and the daemon are separate installs that can be different
    releases: switching the daemon does not move the bridge, so a bridge
    answering from its own copy describes a version that is not executing.
    That is not hypothetical -- on the 2.4.1 to 3.0.0 switch the daemon accepted
    task.input_images and artifact include_base64 while the tool list, served by
    the older bridge, never mentioned either. A caller reading the list is
    reading the only documentation it has.

    tools/call is already forwarded to the daemon untouched, so the daemon has
    always been the thing that decides what a call does. This makes it decide
    what a call *is* as well.

    The local copy is used only when the daemon cannot be reached. Every call
    would fail with daemon_unavailable in that state anyway, so the list is a
    description of what will exist once it is up, not a promise about now.
    """

    try:
        payload = client.request("tools/list", timeout=DEFAULT_DAEMON_CALL_TIMEOUT_SECONDS)
    except HubV2Error:
        return tool_definitions()
    data = payload.get("data") if isinstance(payload, Mapping) else None
    tools = data.get("tools") if isinstance(data, Mapping) else None
    if not isinstance(tools, list) or not tools:
        # A daemon that answers without tools is malfunctioning, not empty.
        return tool_definitions()
    return [dict(item) for item in tools if isinstance(item, Mapping)]


def handle_request(
    message: Mapping[str, Any],
    *,
    client: HubDaemonClient,
) -> dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None:
        return None
    method = str(message.get("method") or "")
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        }
    elif method == "ping":
        try:
            client.request("ping")
        except HubV2Error as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _mcp_result(public_failure(exc, operation="ping")),
            }
        result = {}
    elif method == "tools/list":
        result = {"tools": _tool_list(client)}
    elif method == "tools/call":
        params = message.get("params")
        if not isinstance(params, Mapping):
            payload = public_failure(
                HubV2Error(
                    "invalid_request",
                    "tools/call params must be an object.",
                    scope="mcp",
                ),
                operation="tools/call",
            )
        else:
            name = str(params.get("name") or "")
            arguments = (
                params.get("arguments") if isinstance(params.get("arguments"), Mapping) else {}
            )
            try:
                payload = client.request(
                    "tools/call",
                    {
                        "name": name,
                        "arguments": arguments,
                    },
                    timeout=_daemon_call_timeout(name, arguments),
                )
            except HubV2Error as exc:
                payload = public_failure(
                    exc,
                    operation=name or "tools/call",
                )
        result = _mcp_result(payload)
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "unsupported method"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve() -> int:
    socket_path = os.environ.get("AGENT_HUB_V2_SOCKET", str(DEFAULT_SOCKET_PATH))
    client = HubDaemonClient(socket_path)
    try:
        for line in sys.stdin:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, Mapping):
                continue
            response = handle_request(parsed, client=client)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
