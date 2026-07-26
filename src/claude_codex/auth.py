"""Auth resolution: Claude subscription OAuth first, API key fallback."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from . import paths, subscription_auth

API_KEY_ENV = "ANTHROPIC_API_KEY"
API_KEY_FILE = "api-key"


def api_key_path() -> Path:
    return paths.config_dir() / API_KEY_FILE


def get_api_key() -> str:
    env = os.getenv(API_KEY_ENV, "").strip()
    if env:
        return env
    path = api_key_path()
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def prefer_subscription() -> bool:
    """Default True: use Claude Code subscription when available."""
    raw = os.getenv("CLAUDE_CODEX_AUTH_MODE", "subscription").strip().lower()
    if raw in {"api_key", "key", "apikey"}:
        return False
    if raw in {"subscription", "oauth", "claude_code", "auto", ""}:
        return True
    return True


def resolve_auth() -> Dict[str, Any]:
    """Return auth context for API calls.

    Keys: mode (subscription_oauth|api_key), access_token or api_key, source
    """
    if prefer_subscription():
        sub = subscription_auth.resolve_access_token()
        if sub and sub.get("access_token"):
            return {
                "mode": "subscription_oauth",
                "access_token": sub["access_token"],
                "source": sub.get("source"),
            }
    key = get_api_key()
    if key:
        # OAuth-shaped keys from env still use Bearer
        if key.startswith("sk-ant-oat") or key.startswith("sk-ant-ort"):
            return {"mode": "subscription_oauth", "access_token": key, "source": API_KEY_ENV}
        return {
            "mode": "api_key",
            "api_key": key,
            "source": API_KEY_ENV if os.getenv(API_KEY_ENV) else str(api_key_path()),
        }
    # last resort: subscription even if prefer was api_key
    sub = subscription_auth.resolve_access_token()
    if sub and sub.get("access_token"):
        return {
            "mode": "subscription_oauth",
            "access_token": sub["access_token"],
            "source": sub.get("source"),
        }
    raise RuntimeError(
        "No Anthropic credentials. For subscription: run `claude auth login --claudeai` "
        "(macOS: python3 scripts/claude_codex_login.py mirror-keychain). "
        f"Or set {API_KEY_ENV}."
    )


def has_credentials() -> bool:
    try:
        resolve_auth()
        return True
    except RuntimeError:
        return False


def _api_key_context(key: str) -> Dict[str, Any] | None:
    if not key:
        return None
    mode = "subscription_oauth" if key.startswith(("sk-ant-oat", "sk-ant-ort")) else "api_key"
    return {
        "mode": mode,
        "source": API_KEY_ENV if os.getenv(API_KEY_ENV) else str(api_key_path()),
    }


def status() -> Dict[str, Any]:
    """Report credential readiness without refreshing or writing credentials."""

    sub = subscription_auth.status()
    key = get_api_key()
    subscription_ready = bool(sub.get("logged_in") and sub.get("token_valid") is True)
    subscription_context = (
        {
            "mode": "subscription_oauth",
            "source": sub.get("source"),
        }
        if subscription_ready
        else None
    )
    key_context = _api_key_context(key)
    active = (
        subscription_context or key_context
        if prefer_subscription()
        else key_context or subscription_context
    )
    credentials_present = bool(sub.get("logged_in") or key)
    ready = active is not None
    return {
        "text": (
            f"Auth ready ({active.get('mode')})."
            if active
            else "Credentials are present but require an explicit refresh."
            if credentials_present
            else "No credentials. Use Claude Code login or ANTHROPIC_API_KEY."
        ),
        "configured": credentials_present,
        "ready": ready,
        "credentials_present": credentials_present,
        "active_mode": (active or {}).get("mode"),
        "active_source": (active or {}).get("source"),
        "subscription": sub,
        "api_key_present": bool(key),
        "prefer_subscription": prefer_subscription(),
        "hint": "claude auth login --claudeai  |  CLAUDE_CODEX_AUTH_MODE=subscription|api_key",
    }


def logout_local_plugin_key() -> bool:
    """Remove plugin-local API key file only (does not revoke Claude Code login)."""
    path = api_key_path()
    if path.is_file():
        path.unlink()
        return True
    return False
