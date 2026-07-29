"""Private GPT leaf tools backed by the user's official Codex login."""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, List

from agent_hub.core import limits
from agent_hub.core import response as shared_response

from . import __version__, auth, chat, models, response, security

SERVER_NAME = "openai-codex"
SERVER_VERSION = __version__
PROTOCOL_VERSION = "2024-11-05"


class RpcError(ValueError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


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
        },
        "temperature": {"type": "number"},
        "reasoning_effort": {
            "type": "string",
            "enum": ["low", "medium", "high", "xhigh", "max", "ultra"],
        },
        "timeout_sec": {
            "type": "number",
            "minimum": 5,
            "maximum": limits.MAX_PROVIDER_TIMEOUT_SECONDS,
            "default": limits.MAX_PROVIDER_TIMEOUT_SECONDS,
        },
        "messages": {"type": "array", "items": {"type": "object"}},
        "images": {"type": "array", "items": {"type": ["string", "object"]}},
        "workspace_root": {"type": "string"},
        "session_id": {"type": "string"},
    },
    "additionalProperties": False,
}


def tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "name": "openai_codex_consent_status",
            "description": "Read explicit GPT/Codex consent state. Cannot grant consent.",
            "inputSchema": _empty_schema(),
        },
        {
            "name": "openai_codex_provider_status",
            "description": "Check official Codex installation and ChatGPT login without secrets.",
            "inputSchema": _empty_schema(),
        },
        {
            "name": "openai_codex_chat",
            "description": "Run an isolated, ephemeral GPT turn using the official Codex login.",
            "inputSchema": CHAT_SCHEMA,
        },
        {
            "name": "openai_codex_list_models",
            "description": "List models from the signed-in official Codex catalog.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "probe": {"type": "boolean", "default": False},
                    "include_hidden": {"type": "boolean", "default": False},
                    "timeout_sec": {"type": "number", "minimum": 5, "maximum": 120},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "openai_codex_login_status",
            "description": "Read redacted official Codex login status.",
            "inputSchema": _empty_schema(),
        },
        {
            "name": "openai_codex_login_start",
            "description": "Return the official Codex browser or device login command.",
            "inputSchema": {
                "type": "object",
                "properties": {"device": {"type": "boolean", "default": False}},
                "additionalProperties": False,
            },
        },
        {
            "name": "openai_codex_login_complete",
            "description": "Recheck official Codex login after the external command completes.",
            "inputSchema": _empty_schema(),
        },
        {
            "name": "openai_codex_login_refresh",
            "description": "Ask official Codex to refresh and validate its shared login.",
            "inputSchema": _empty_schema(),
        },
        {
            "name": "openai_codex_logout",
            "description": "Refuse shared Codex logout and return the required external action.",
            "inputSchema": _empty_schema(),
        },
        {
            "name": "openai_codex_doctor",
            "description": "Diagnose consent, Codex installation, and subscription login.",
            "inputSchema": _empty_schema(),
        },
    ]


def _provider_status(_args: Dict[str, Any]) -> Dict[str, Any]:
    consent = security.consent_status()
    state = auth.status()
    ready = bool(consent.get("user_consent") and state.get("configured"))
    warnings = [state["warning"]] if state.get("warning") else []
    if state.get("error_type"):
        warnings.append(str(state["error_type"]))
    return {
        "text": "GPT provider is ready." if ready else "GPT provider is not ready.",
        "consent": bool(consent.get("user_consent")),
        "authenticated": bool(state.get("logged_in")),
        "subscription_login": bool(state.get("subscription_login")),
        "auth_mode": state.get("auth_mode"),
        "plan_type": state.get("plan_type"),
        **response.standard_fields(
            success=ready,
            provider="gpt",
            backend="codex-app-server",
            warnings=warnings,
        ),
    }


def _login_status(_args: Dict[str, Any]) -> Dict[str, Any]:
    state = auth.status()
    return {
        "text": "Official Codex subscription login is ready."
        if state.get("configured")
        else "Official Codex subscription login is not ready.",
        **state,
        **response.standard_fields(
            success=bool(state.get("configured")),
            provider="gpt",
            backend="codex-app-server",
        ),
    }


def _login_start(args: Dict[str, Any]) -> Dict[str, Any]:
    security.require_consent()
    return {
        **auth.login_action(device=bool(args.get("device"))),
        **response.standard_fields(
            success=True,
            provider="gpt",
            backend="official-codex-login",
        ),
    }


def _login_complete(_args: Dict[str, Any]) -> Dict[str, Any]:
    security.require_consent()
    return _login_status({})


def _login_refresh(_args: Dict[str, Any]) -> Dict[str, Any]:
    security.require_consent()
    state = auth.require_subscription(refresh=True)
    return {
        "text": "Official Codex refreshed and validated the shared ChatGPT login.",
        "auth_mode": state.get("auth_mode"),
        "plan_type": state.get("plan_type"),
        **response.standard_fields(
            success=True,
            provider="gpt",
            backend="codex-app-server",
        ),
    }


def _logout(_args: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "text": (
            "Agent Hub will not log out the shared Codex account. "
            "Run `codex logout` explicitly if you intend to sign every Codex client out."
        ),
        "error": "shared_codex_logout_refused",
        "next_action": {"type": "external_cli", "command": "codex logout"},
        **response.standard_fields(
            success=False,
            provider="gpt",
            backend="official-codex-login",
        ),
    }


def _doctor(_args: Dict[str, Any]) -> Dict[str, Any]:
    return _provider_status({})


def dispatch_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    table: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
        "openai_codex_consent_status": lambda _a: {
            "text": json.dumps(security.consent_status(), ensure_ascii=False, indent=2),
            **security.consent_status(),
        },
        "openai_codex_provider_status": _provider_status,
        "openai_codex_chat": chat.run_chat,
        "openai_codex_list_models": models.list_models,
        "openai_codex_login_status": _login_status,
        "openai_codex_login_start": _login_start,
        "openai_codex_login_complete": _login_complete,
        "openai_codex_login_refresh": _login_refresh,
        "openai_codex_logout": _logout,
        "openai_codex_doctor": _doctor,
    }
    if name not in table:
        raise ValueError(f"unknown tool: {name}")
    try:
        return table[name](arguments or {})
    except Exception as exc:  # noqa: BLE001
        return shared_response.failure_payload(exc, provider="gpt", backend="official-codex")


def handle_request(message: Dict[str, Any]) -> Dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None:
        return None
    try:
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_definitions()}
        elif method == "tools/call":
            params = message.get("params") or {}
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise RpcError(-32602, "tool arguments must be an object")
            name = str(params.get("name") or "")
            if name not in {item["name"] for item in tool_definitions()}:
                raise RpcError(-32602, f"unknown tool: {name}")
            result = dispatch_tool(name, arguments)
        else:
            raise RpcError(-32601, f"unsupported method: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except RpcError as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": exc.code, "message": str(exc)},
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32000, "message": str(exc)},
        }


def serve() -> int:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(message, dict) or message.get("id") is None:
            continue
        response_message = handle_request(message)
        if response_message is not None:
            sys.stdout.write(json.dumps(response_message, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
