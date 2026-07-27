"""Minimal MCP stdio server for Grok Codex."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Dict, List

from agent_hub.core import limits
from agent_hub.core import response as shared_response

from . import __version__, auth, chat, models, oauth_login, response, security

SERVER_NAME = "grok-codex"
SERVER_VERSION = __version__
MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_PROTOCOL_VERSIONS = (MODERN_PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS)
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
DISCOVERY_TTL_MS = 300_000
MODERN_META_PROTOCOL = "io.modelcontextprotocol/protocolVersion"


class RpcError(ValueError):
    def __init__(self, code: int, message: str, *, data: Dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def _empty_schema() -> Dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


CHAT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "system": {"type": "string"},
        "model": {"type": "string"},
        "max_tokens": {
            "type": "integer",
            "minimum": 1,
            "maximum": limits.MAX_OUTPUT_TOKENS,
            "default": chat.DEFAULT_MAX_TOKENS,
        },
        "temperature": {"type": "number"},
        "reasoning_effort": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "Uses Grok 4.5 Responses reasoning.effort; unsupported models fail closed.",
        },
        "timeout_sec": {
            "type": "integer",
            "minimum": 5,
            "maximum": limits.MAX_PROVIDER_TIMEOUT_SECONDS,
            "default": limits.MAX_PROVIDER_TIMEOUT_SECONDS,
        },
        "messages": {
            "type": "array",
            "items": {"type": "object"},
        },
        "images": {"type": "array", "items": {"type": ["string", "object"]}},
        "workspace_root": {"type": "string"},
        "api_mode": {"type": "string", "enum": ["chat", "responses"]},
        "session_id": {"type": "string"},
    },
    "additionalProperties": False,
}

LIST_MODELS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "probe": {"type": "boolean", "default": False},
    },
    "additionalProperties": False,
}


def tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "name": "grok_codex_consent_status",
            "description": "Read explicit-consent state. Cannot grant consent.",
            "inputSchema": _empty_schema(),
        },
        {
            "name": "grok_codex_provider_status",
            "description": "Check credentials and readiness without exposing secrets.",
            "inputSchema": _empty_schema(),
        },
        {
            "name": "grok_codex_chat",
            "description": "Chat with Grok. Prefers SuperGrok OAuth; falls back to XAI_API_KEY.",
            "inputSchema": CHAT_SCHEMA,
        },
        {
            "name": "grok_codex_list_models",
            "description": "List curated (and optionally live) models.",
            "inputSchema": LIST_MODELS_SCHEMA,
        },
        {
            "name": "grok_codex_login_status",
            "description": "SuperGrok / xAI OAuth login status (no secrets).",
            "inputSchema": _empty_schema(),
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        },
        {
            "name": "grok_codex_login_start",
            "description": (
                "Return the Agent Hub connection-manager command. xAI login starts only "
                "from a visible user action in the local GUI."
            ),
            "inputSchema": _empty_schema(),
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        },
        {
            "name": "grok_codex_login_complete",
            "description": (
                "Return the Agent Hub connection-manager command without completing "
                "OAuth through MCP."
            ),
            "inputSchema": _empty_schema(),
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        },
        {
            "name": "grok_codex_logout",
            "description": (
                "Return the Agent Hub connection-manager command. Local credential "
                "deletion requires visible confirmation in the GUI."
            ),
            "inputSchema": _empty_schema(),
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        },
        {
            "name": "grok_codex_doctor",
            "description": "Quick local diagnosis: consent, credentials, default model.",
            "inputSchema": _empty_schema(),
        },
    ]


def _provider_status(_args: Dict[str, Any]) -> Dict[str, Any]:
    status = auth.status()
    return {
        "text": status.get("text") or json.dumps(status, indent=2),
        **status,
        **response.standard_fields(
            success=bool(status.get("ready")),
            provider="xai",
            backend="xai-chat-completions",
        ),
    }


def _doctor(_args: Dict[str, Any]) -> Dict[str, Any]:
    consent = security.consent_status()
    auth_state = auth.status()
    ok = bool(consent.get("user_consent") and auth_state.get("ready"))
    payload = {
        "ok": ok,
        "consent": consent,
        "auth": auth_state,
        "default_model": models.DEFAULT_MODEL,
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }
    return {
        "text": json.dumps(payload, indent=2),
        **payload,
        **response.standard_fields(
            success=ok,
            provider="xai",
            backend="xai-chat-completions",
        ),
    }


def _login_status(_args: Dict[str, Any]) -> Dict[str, Any]:
    state = oauth_login.status()
    return {
        "text": json.dumps(state, indent=2),
        **state,
        **response.standard_fields(
            success=bool(state.get("logged_in")),
            provider="xai",
            backend="subscription-oauth",
        ),
    }


def _provider_gui_required(_args: Dict[str, Any]) -> Dict[str, Any]:
    console_script = Path(sys.executable).with_name("agent-hub-connect")
    if console_script.is_file() and os.access(console_script, os.X_OK):
        command = str(console_script)
        command_args: list[str] = []
    else:
        command = sys.executable
        command_args = ["-m", "agent_hub.connect_app"]
    return {
        "success": False,
        "error": "provider_gui_required",
        "error_type": "provider_gui_required",
        "text": (
            "Open the local Agent Hub connection manager and confirm the account "
            "action in its browser tab."
        ),
        "next_action": {
            "type": "local_gui",
            "command": command,
            "args": command_args,
            "provider": "grok",
        },
        **response.standard_fields(
            success=False,
            provider="xai",
            backend="local-connection-manager",
        ),
    }


def dispatch_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    table: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
        "grok_codex_consent_status": lambda a: {
            "text": json.dumps(security.consent_status(), indent=2),
            **security.consent_status(),
        },
        "grok_codex_provider_status": _provider_status,
        "grok_codex_chat": chat.run_chat,
        "grok_codex_list_models": models.list_models,
        "grok_codex_login_status": _login_status,
        "grok_codex_login_start": _provider_gui_required,
        "grok_codex_login_complete": _provider_gui_required,
        "grok_codex_logout": _provider_gui_required,
        "grok_codex_doctor": _doctor,
    }
    if name not in table:
        raise ValueError(f"unknown tool: {name}")
    try:
        return table[name](arguments or {})
    except Exception as exc:  # noqa: BLE001
        return shared_response.failure_payload(
            exc,
            provider="xai",
            backend="xai-chat-completions",
        )


def handle_request(message: Dict[str, Any]) -> Dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None:
        return None
    method = message.get("method")
    try:
        if method == "initialize":
            params = message.get("params") or {}
            requested = str(params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION)
            selected = (
                requested if requested in LEGACY_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
            )
            result = {
                "protocolVersion": selected,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_definitions()}
        elif method == "tools/call":
            params = message.get("params") or {}
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise RpcError(-32602, "tool arguments must be an object")
            if name not in {t["name"] for t in tool_definitions()}:
                raise RpcError(-32602, f"unknown tool: {name}")
            result = dispatch_tool(name, arguments)
        else:
            raise RpcError(-32601, f"unsupported method: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except RpcError as exc:
        err: Dict[str, Any] = {"code": exc.code, "message": str(exc)}
        if exc.data:
            err["data"] = exc.data
        return {"jsonrpc": "2.0", "id": request_id, "error": err}
    except Exception as exc:  # noqa: BLE001
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32000, "message": str(exc)},
        }


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
        # notifications
        if message.get("id") is None and message.get("method"):
            continue
        response_msg = handle_request(message)
        if response_msg is not None:
            sys.stdout.write(json.dumps(response_msg, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
