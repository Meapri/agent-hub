"""Minimal MCP stdio server for Claude Codex."""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, List

from . import __version__, auth, chat, models, response, security, subscription_auth

SERVER_NAME = "claude-codex"
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
        "max_tokens": {"type": "integer", "minimum": 1, "default": 4096},
        "temperature": {"type": "number"},
        "timeout_sec": {"type": "integer", "minimum": 5, "maximum": 600, "default": 120},
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["role", "content"],
            },
        },
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
            "name": "claude_codex_consent_status",
            "description": "Read explicit-consent state. Cannot grant consent.",
            "inputSchema": _empty_schema(),
        },
        {
            "name": "claude_codex_provider_status",
            "description": "Check credentials and readiness without exposing secrets.",
            "inputSchema": _empty_schema(),
        },
        {
            "name": "claude_codex_chat",
            "description": "Chat via Anthropic Messages. Prefers Claude subscription OAuth (Claude Code login) with plan-lane fingerprint; falls back to API key.",
            "inputSchema": CHAT_SCHEMA,
        },
        {
            "name": "claude_codex_list_models",
            "description": "List curated (and optionally live) models.",
            "inputSchema": LIST_MODELS_SCHEMA,
        },

        {
            "name": "claude_codex_login_status",
            "description": "Claude Code / subscription OAuth credential status (no secrets).",
            "inputSchema": _empty_schema(),
        },
        {
            "name": "claude_codex_login_refresh",
            "description": "Refresh Claude Code OAuth access token using stored refresh token.",
            "inputSchema": _empty_schema(),
        },
        {
            "name": "claude_codex_mirror_keychain",
            "description": "macOS: copy Claude Code Keychain credentials into ~/.claude/.credentials.json.",
            "inputSchema": _empty_schema(),
        },
        {
            "name": "claude_codex_doctor",
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
            success=bool(status.get("configured")),
            provider="anthropic",
            backend="anthropic-messages",
        ),
    }


def _doctor(_args: Dict[str, Any]) -> Dict[str, Any]:
    consent = security.consent_status()
    auth_state = auth.status()
    ok = bool(consent.get("user_consent") and auth_state.get("configured"))
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
            provider="anthropic",
            backend="anthropic-messages",
        ),
    }




def _login_status(_args):
    st = subscription_auth.status()
    return {"text": json.dumps(st, indent=2), **st, **response.standard_fields(provider="anthropic", backend="subscription-oauth", success=bool(st.get("logged_in")))}


def _login_refresh(_args):
    security.require_consent()
    creds = subscription_auth.read_credentials()
    if not creds or not creds.get("refreshToken"):
        raise RuntimeError("No refresh token. Run: claude auth login --claudeai")
    refreshed = subscription_auth.refresh_token_pure(creds["refreshToken"])
    subscription_auth.write_credentials(
        refreshed["access_token"], refreshed["refresh_token"], refreshed["expires_at_ms"]
    )
    out = {"success": True, "expires_at_ms": refreshed["expires_at_ms"]}
    return {"text": json.dumps(out, indent=2), **out, **response.standard_fields(provider="anthropic", backend="subscription-oauth")}


def _mirror_keychain(_args):
    out = subscription_auth.mirror_keychain_to_file()
    return {"text": json.dumps(out, indent=2), **out, **response.standard_fields(success=bool(out.get("success")), provider="anthropic", backend="subscription-oauth")}

def dispatch_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    table: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
        "claude_codex_consent_status": lambda a: {
            "text": json.dumps(security.consent_status(), indent=2),
            **security.consent_status(),
        },
        "claude_codex_provider_status": _provider_status,
        "claude_codex_chat": chat.run_chat,
        "claude_codex_list_models": models.list_models,
        "claude_codex_login_status": _login_status,
        "claude_codex_login_refresh": _login_refresh,
        "claude_codex_mirror_keychain": _mirror_keychain,
        "claude_codex_doctor": _doctor,
    }
    if name not in table:
        raise ValueError(f"unknown tool: {name}")
    try:
        return table[name](arguments or {})
    except Exception as exc:  # noqa: BLE001
        return {
            "text": str(exc),
            "success": False,
            "error": str(exc),
            "error_type": getattr(exc, "code", type(exc).__name__),
            "provider": "anthropic",
            "backend": "anthropic-messages",
            "warnings": [],
        }


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
