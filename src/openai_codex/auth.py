"""Redacted account facade owned by the official Codex app-server."""

from __future__ import annotations

from typing import Any, Dict

from . import client
from .errors import CodexAuthenticationRequired, CodexSubscriptionRequired

SUBSCRIPTION_AUTH_TYPES = {"chatgpt", "personalAccessToken"}


def _cli_status(*, timeout: float) -> Dict[str, Any] | None:
    """Use the official read-only CLI status when app-server startup is unavailable."""

    try:
        stdout, stderr, returncode = client.run_bounded(
            [client.codex_binary(), "login", "status"],
            input_text="",
            timeout=max(1.0, min(float(timeout), 10.0)),
        )
    except Exception:  # noqa: BLE001
        return None
    if returncode != 0:
        return None
    output = f"{stdout}\n{stderr}".lower()
    logged_in = "logged in" in output and "not logged in" not in output
    subscription = "chatgpt" in output or "personal access token" in output
    api_key = "api key" in output
    return {
        "installed": True,
        "configured": logged_in and subscription,
        "logged_in": logged_in,
        "subscription_login": logged_in and subscription,
        "auth_mode": (
            "chatgpt" if logged_in and subscription else "apiKey" if logged_in and api_key else None
        ),
        "plan_type": None,
        "requires_openai_auth": True,
        "warning": (
            None if not logged_in or subscription else "codex_api_key_mode_not_subscription"
        ),
        "status_source": "codex_cli",
    }


def status(*, refresh: bool = False, timeout: float = 20.0) -> Dict[str, Any]:
    try:
        result = client.app_server_request(
            "account/read",
            {"refreshToken": bool(refresh)},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        fallback = _cli_status(timeout=timeout)
        if fallback is not None:
            fallback["status_warning"] = "codex_app_server_unavailable"
            return fallback
        return {
            "installed": False if getattr(exc, "code", "") == "codex_unavailable" else True,
            "configured": False,
            "logged_in": False,
            "subscription_login": False,
            "auth_mode": None,
            "plan_type": None,
            "error": str(exc),
            "error_type": getattr(exc, "code", type(exc).__name__),
            "status_source": "codex_app_server",
        }
    account = result.get("account") if isinstance(result.get("account"), dict) else None
    auth_mode = str(account.get("type") or "") if account else ""
    logged_in = bool(account)
    subscription = auth_mode in SUBSCRIPTION_AUTH_TYPES
    return {
        "installed": True,
        "configured": subscription,
        "logged_in": logged_in,
        "subscription_login": subscription,
        "auth_mode": auth_mode or None,
        "plan_type": account.get("planType") if account else None,
        "requires_openai_auth": bool(result.get("requiresOpenaiAuth", True)),
        "status_source": "codex_app_server",
        "warning": (
            "codex_api_key_mode_not_subscription" if logged_in and not subscription else None
        ),
    }


def require_subscription(*, refresh: bool = False, timeout: float = 20.0) -> Dict[str, Any]:
    current = status(refresh=refresh, timeout=timeout)
    if not current.get("installed"):
        raise CodexAuthenticationRequired(str(current.get("error") or "Codex is unavailable"))
    if not current.get("logged_in"):
        raise CodexAuthenticationRequired(
            "Codex is not signed in. Run `codex login` or `codex login --device-auth`."
        )
    if not current.get("subscription_login"):
        raise CodexSubscriptionRequired(
            "Codex is using API-key mode. Sign in with ChatGPT via `codex login` to use "
            "the subscription-backed GPT provider without OpenAI API billing."
        )
    return current


def login_action(*, device: bool = False) -> Dict[str, Any]:
    command = "codex login --device-auth" if device else "codex login"
    return {
        "success": True,
        "text": f"Run `{command}` in a terminal, then call agent_hub_status for provider=gpt.",
        "next_action": {"type": "external_cli", "command": command},
        "shared_codex_login": True,
    }
