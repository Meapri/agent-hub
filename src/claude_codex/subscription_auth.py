"""Claude Code / Claude.ai subscription OAuth credentials.

Reads refreshable tokens from macOS Keychain or ``~/.claude/.credentials.json``
(same sources Hermes and Claude Code use). Does not implement a separate
browser PKCE client — users log in with Claude Code CLI.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

# Public Claude Code OAuth client (same as Hermes / Claude Code).
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_ENDPOINTS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)
OAUTH_TOKEN_USER_AGENT = "claude-codex/0.2.0"
OAUTH_BETAS = (
    "claude-code-20250219",
    "oauth-2025-04-20",
    "prompt-caching-scope-2026-01-05",
    "advisor-tool-2026-03-01",
)


def _parse_oauth_blob(data: Any, *, source: str) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    access = str(oauth.get("accessToken") or "").strip()
    if not access:
        return None
    return {
        "accessToken": access,
        "refreshToken": str(oauth.get("refreshToken") or "").strip(),
        "expiresAt": int(oauth.get("expiresAt") or 0),
        "source": source,
    }


def read_from_file() -> Optional[Dict[str, Any]]:
    path = Path.home() / ".claude" / ".credentials.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return _parse_oauth_blob(data, source="claude_code_credentials_file")


def read_from_keychain() -> Optional[Dict[str, Any]]:
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout.strip())
    except ValueError:
        return None
    return _parse_oauth_blob(data, source="macos_keychain")


def is_token_valid(creds: Dict[str, Any]) -> bool:
    expires_at = int(creds.get("expiresAt") or 0)
    if not expires_at:
        return bool(creds.get("accessToken"))
    now_ms = int(time.time() * 1000)
    return now_ms < (expires_at - 60_000)


def read_credentials() -> Optional[Dict[str, Any]]:
    kc = read_from_keychain()
    file_creds = read_from_file()
    if kc and file_creds:
        kc_ok = is_token_valid(kc)
        file_ok = is_token_valid(file_creds)
        if kc_ok and not file_ok:
            return kc
        if file_ok and not kc_ok:
            return file_creds
        return kc if int(kc.get("expiresAt") or 0) >= int(file_creds.get("expiresAt") or 0) else file_creds
    return kc or file_creds


def write_credentials(access_token: str, refresh_token: str, expires_at_ms: int) -> Path:
    path = Path.home() / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: Dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError, TypeError):
            existing = {}
    existing["claudeAiOauth"] = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at_ms,
    }
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def mirror_keychain_to_file() -> Dict[str, Any]:
    """Copy macOS Keychain Claude Code credentials into ~/.claude/.credentials.json."""
    kc = read_from_keychain()
    if not kc:
        return {"success": False, "error": "No Claude Code-credentials entry in Keychain"}
    path = write_credentials(
        kc["accessToken"],
        kc.get("refreshToken") or "",
        int(kc.get("expiresAt") or 0),
    )
    return {
        "success": True,
        "path": str(path),
        "source": "macos_keychain",
        "expires_at": kc.get("expiresAt"),
    }


def refresh_token_pure(refresh_token: str) -> Dict[str, Any]:
    if not refresh_token:
        raise ValueError("refresh_token is required")
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }
    ).encode()
    last_error: Exception | None = None
    for endpoint in TOKEN_ENDPOINTS:
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": OAUTH_TOKEN_USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
        access = str(result.get("access_token") or "").strip()
        if not access:
            raise ValueError("Anthropic refresh response missing access_token")
        expires_in = int(result.get("expires_in") or 3600)
        return {
            "access_token": access,
            "refresh_token": str(result.get("refresh_token") or refresh_token).strip(),
            "expires_at_ms": int(time.time() * 1000) + expires_in * 1000,
        }
    if last_error:
        raise last_error
    raise ValueError("Anthropic token refresh failed")


def resolve_access_token() -> Optional[Dict[str, Any]]:
    """Return ``{access_token, mode, source}`` for subscription OAuth, or None."""
    creds = read_credentials()
    if not creds:
        return None
    if is_token_valid(creds):
        return {
            "access_token": creds["accessToken"],
            "mode": "subscription_oauth",
            "source": creds.get("source"),
        }
    # Try adopt newer file after Claude Code refresh
    again = read_credentials()
    if again and is_token_valid(again) and again.get("accessToken") != creds.get("accessToken"):
        return {
            "access_token": again["accessToken"],
            "mode": "subscription_oauth",
            "source": again.get("source"),
        }
    refresh = (again or creds).get("refreshToken") or ""
    if not refresh:
        return None
    try:
        refreshed = refresh_token_pure(refresh)
        write_credentials(
            refreshed["access_token"],
            refreshed["refresh_token"],
            refreshed["expires_at_ms"],
        )
        return {
            "access_token": refreshed["access_token"],
            "mode": "subscription_oauth",
            "source": "refreshed",
        }
    except Exception:
        return None


def status() -> Dict[str, Any]:
    creds = read_credentials()
    if not creds:
        return {
            "logged_in": False,
            "mode": "none",
            "hint": "Run: claude auth login --claudeai  (then optional mirror-keychain on macOS)",
        }
    valid = is_token_valid(creds)
    return {
        "logged_in": True,
        "token_valid": valid,
        "source": creds.get("source"),
        "expires_at": creds.get("expiresAt"),
        "mode": "subscription_oauth",
        "has_refresh_token": bool(creds.get("refreshToken")),
        "token_prefix": (creds["accessToken"][:12] + "…") if creds.get("accessToken") else "",
    }
