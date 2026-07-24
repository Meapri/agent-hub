"""Auth: SuperGrok OAuth first, XAI_API_KEY fallback."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from . import oauth_login, paths

API_KEY_ENV = "XAI_API_KEY"
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
    raw = os.getenv("GROK_CODEX_AUTH_MODE", "subscription").strip().lower()
    if raw in {"api_key", "key", "apikey"}:
        return False
    return True


def resolve_auth() -> Dict[str, Any]:
    if prefer_subscription():
        token = oauth_login.resolve_access_token()
        if token:
            return {"mode": "subscription_oauth", "access_token": token, "source": "oauth-token.json"}
    key = get_api_key()
    if key:
        return {
            "mode": "api_key",
            "api_key": key,
            "source": API_KEY_ENV if os.getenv(API_KEY_ENV) else str(api_key_path()),
        }
    token = oauth_login.resolve_access_token()
    if token:
        return {"mode": "subscription_oauth", "access_token": token, "source": "oauth-token.json"}
    raise RuntimeError(
        "No xAI credentials. For SuperGrok: python3 scripts/grok_codex_login.py interactive "
        f"or set {API_KEY_ENV}."
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
    return {
        "mode": "api_key",
        "source": API_KEY_ENV if os.getenv(API_KEY_ENV) else str(api_key_path()),
    }


def status() -> Dict[str, Any]:
    """Report credential readiness without refreshing or writing credentials."""

    sub = oauth_login.status()
    key = get_api_key()
    subscription_ready = bool(
        sub.get("logged_in") and sub.get("token_valid") is True
    )
    subscription_context = (
        {"mode": "subscription_oauth", "source": sub.get("source")}
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
            else "No credentials. SuperGrok OAuth or XAI_API_KEY."
        ),
        "configured": credentials_present,
        "ready": ready,
        "credentials_present": credentials_present,
        "active_mode": (active or {}).get("mode"),
        "active_source": (active or {}).get("source"),
        "subscription": sub,
        "api_key_present": bool(key),
        "prefer_subscription": prefer_subscription(),
        "hint": "scripts/grok_codex_login.py interactive  |  GROK_CODEX_AUTH_MODE=subscription|api_key",
    }
