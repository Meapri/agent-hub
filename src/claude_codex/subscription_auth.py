"""Claude Code / Claude.ai subscription OAuth credentials.

Reads refreshable tokens from macOS Keychain or ``~/.claude/.credentials.json``
(same sources Hermes and Claude Code use). Does not implement a separate
browser PKCE client — users log in with Claude Code CLI.
"""

from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import os
import platform
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, ContextManager, Dict, Optional

from agent_hub.core.auth_state import file_revision, refresh_operation_lock

from . import paths, security

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


class ClaudeRefreshError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def credentials_file_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


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
    path = credentials_file_path()
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
        return (
            kc
            if int(kc.get("expiresAt") or 0) >= int(file_creds.get("expiresAt") or 0)
            else file_creds
        )
    return kc or file_creds


def write_credentials(
    access_token: str,
    refresh_token: str,
    expires_at_ms: int,
    *,
    expected_revision: str | None = None,
) -> Path:
    path = credentials_file_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise OSError("Claude credentials path must not be a symlink")
    if expected_revision is not None and not secrets.compare_digest(
        file_revision(path),
        expected_revision,
    ):
        raise ClaudeRefreshError(
            "Claude credentials changed before commit.",
            code="credentials_changed",
        )
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
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor_open = False
        with handle:
            json.dump(existing, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if expected_revision is not None and not secrets.compare_digest(
            file_revision(path),
            expected_revision,
        ):
            raise ClaudeRefreshError(
                "Claude credentials changed before commit.",
                code="credentials_changed",
            )
        os.replace(temporary, path)
    finally:
        if descriptor_open:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
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


def _credential_identity(credentials: Dict[str, Any] | None) -> str:
    if not credentials:
        return "missing"
    material = json.dumps(
        {
            "accessToken": credentials.get("accessToken"),
            "refreshToken": credentials.get("refreshToken"),
            "expiresAt": credentials.get("expiresAt"),
            "source": credentials.get("source"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(material).hexdigest()


def refresh_access_token(
    *,
    cancel_event: threading.Event | None = None,
    commit_guard: Callable[[], ContextManager[Any]] | None = None,
) -> Dict[str, Any]:
    """Strictly refresh Claude OAuth with consent, revision, and commit fences."""

    with refresh_operation_lock(paths.config_dir()):
        with security.auth_state_lock():
            security.require_consent()
            consent_revision = security.consent_revision()
            credential_path = credentials_file_path()
            credential_revision = file_revision(credential_path)
            credentials = read_credentials()
            if not credentials:
                raise ClaudeRefreshError(
                    "Claude login credentials are unavailable.",
                    code="credentials_missing",
                )
            if is_token_valid(credentials):
                return {
                    "access_token": credentials["accessToken"],
                    "source": credentials.get("source"),
                }
            refresh_token = str(credentials.get("refreshToken") or "")
            if not refresh_token:
                raise ClaudeRefreshError(
                    "Claude login must be completed again.",
                    code="refresh_token_missing",
                )
            credential_identity = _credential_identity(credentials)
        if cancel_event is not None and cancel_event.is_set():
            raise ClaudeRefreshError(
                "Claude login refresh was cancelled.",
                code="refresh_cancelled",
            )
        try:
            refreshed = refresh_token_pure(refresh_token)
        except Exception as exc:  # noqa: BLE001
            raise ClaudeRefreshError(
                "Claude login refresh failed.",
                code="refresh_failed",
            ) from exc
        with security.auth_state_lock():
            if not security.user_consent_enabled() or not secrets.compare_digest(
                security.consent_revision(),
                consent_revision,
            ):
                raise ClaudeRefreshError(
                    "Claude consent changed during refresh.",
                    code="consent_changed",
                )
            current = read_credentials()
            current_revision = file_revision(credential_path)
            credentials_changed = bool(
                not secrets.compare_digest(current_revision, credential_revision)
                or not secrets.compare_digest(
                    _credential_identity(current),
                    credential_identity,
                )
            )
            if credentials_changed:
                if current and is_token_valid(current):
                    return {
                        "access_token": current["accessToken"],
                        "source": current.get("source"),
                    }
                raise ClaudeRefreshError(
                    "Claude credentials changed during refresh.",
                    code="credentials_changed",
                )
            guard = commit_guard() if commit_guard is not None else nullcontext()
            with guard:
                if cancel_event is not None and cancel_event.is_set():
                    raise ClaudeRefreshError(
                        "Claude login refresh was cancelled.",
                        code="refresh_cancelled",
                    )
                write_credentials(
                    refreshed["access_token"],
                    refreshed["refresh_token"],
                    refreshed["expires_at_ms"],
                    expected_revision=credential_revision,
                )
        return {
            "access_token": refreshed["access_token"],
            "source": "refreshed",
        }


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
    try:
        refreshed = refresh_access_token()
        return {**refreshed, "mode": "subscription_oauth"}
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
