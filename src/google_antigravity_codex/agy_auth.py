"""Plugin-owned Google OAuth credentials for Code Assist.

Tokens are created only by this plugin's PKCE login
(``oauth_login`` / ``scripts/google_antigravity_login.py``). The official
``agy`` CLI session and Keychain are **not** used or scraped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import math
import os
from pathlib import Path
import stat
import time
from typing import Any, Dict, Optional

from . import security

# Direct plugin OAuth login (PKCE) writes here by default.
PLUGIN_TOKEN_FILE = "~/.config/google-antigravity-codex/oauth-token.json"
MAX_TOKEN_FILE_BYTES = 1024 * 1024


class AgyAuthError(RuntimeError):
    def __init__(self, message: str, *, code: str = "agy_auth_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AgyCredentials:
    access_token: str = field(repr=False)
    refresh_token: str = field(default="", repr=False)
    expires_at_ms: int = 0
    project_id: str = ""

    @property
    def expired(self) -> bool:
        return self.expires_at_ms > 0 and self.expires_at_ms <= int(time.time() * 1000) + 60_000


def plugin_token_path() -> Path:
    """Canonical plugin-owned token path (where direct login writes)."""
    try:
        from . import paths as _paths

        return _paths.config_dir() / "oauth-token.json"
    except Exception:
        return Path(PLUGIN_TOKEN_FILE).expanduser()


def candidate_token_paths() -> list[Path]:
    """Ordered candidate token files for read/logout (plugin-owned only)."""
    paths: list[Path] = []
    # Optional override for tests / advanced installs (must be a plugin-written file).
    explicit = os.getenv("GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE", "").strip()
    if explicit:
        paths.append(Path(explicit).expanduser())
    paths.append(plugin_token_path())
    seen: set[str] = set()
    ordered: list[Path] = []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            ordered.append(path)
    return ordered


def token_file_path() -> Path:
    """Resolve the plugin OAuth token path.

    Priority:
    1. Explicit ``GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE`` if set and present
    2. Plugin ``oauth-token.json`` (read if present, else write target)
    """
    for path in candidate_token_paths():
        if path.is_file():
            return path
    return plugin_token_path()


def _expiry_to_ms(value: Any) -> int:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return int(parsed.timestamp() * 1000)
        except ValueError:
            value = text
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number) or number <= 0:
        return 0
    return int(number if number >= 10_000_000_000 else number * 1000)


def _read_token_file(path: Path) -> str:
    try:
        path_info = path.lstat()
    except FileNotFoundError as exc:
        raise AgyAuthError(
            f"Official agy token export was not found at {path}.",
            code="agy_token_file_missing",
        ) from exc
    if not stat.S_ISREG(path_info.st_mode) or stat.S_ISLNK(path_info.st_mode):
        raise AgyAuthError("agy token export must be a regular, non-symlink file.", code="agy_token_file_unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AgyAuthError("agy token export could not be opened safely.", code="agy_token_file_unsafe") from exc
    try:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino):
            raise AgyAuthError("agy token export changed while it was being opened.", code="agy_token_file_unsafe")
        if not stat.S_ISREG(info.st_mode):
            raise AgyAuthError("agy token export must be a regular file.", code="agy_token_file_unsafe")
        if info.st_size <= 0 or info.st_size > MAX_TOKEN_FILE_BYTES:
            raise AgyAuthError("agy token export has an invalid size.", code="agy_token_file_size_invalid")
        if os.name == "posix":
            if info.st_uid != os.getuid():
                raise AgyAuthError("agy token export is not owned by the current user.", code="agy_token_file_owner_invalid")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise AgyAuthError("agy token export permissions must be 0600 or stricter.", code="agy_token_file_permissions_invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_TOKEN_FILE_BYTES + 1)
        if len(raw) > MAX_TOKEN_FILE_BYTES:
            raise AgyAuthError("agy token export has an invalid size.", code="agy_token_file_size_invalid")
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise AgyAuthError("agy token export is not valid UTF-8.", code="agy_token_file_invalid") from exc
    finally:
        os.close(descriptor)


def load_credentials() -> AgyCredentials:
    if not security.agy_session_enabled():
        raise AgyAuthError(
            "agy session reuse requires explicit user consent.",
            code="agy_session_disabled",
        )
    path = token_file_path()
    try:
        data = json.loads(_read_token_file(path))
    except json.JSONDecodeError as exc:
        raise AgyAuthError("agy token export is not valid JSON.", code="agy_token_file_invalid") from exc
    if not isinstance(data, dict):
        raise AgyAuthError("agy token export must contain a JSON object.", code="agy_token_file_invalid")
    token = data.get("token") if isinstance(data.get("token"), dict) else data
    access = str(token.get("access_token") or token.get("access") or "").strip()
    refresh = str(token.get("refresh_token") or token.get("refresh") or "").strip()
    expires = _expiry_to_ms(
        token.get("expiry")
        or token.get("expires_at")
        or token.get("expires")
        or token.get("expires_at_ms")
        or 0
    )
    project = str(
        token.get("project_id")
        or token.get("cloudaicompanion_project")
        or data.get("project_id")
        or data.get("cloudaicompanion_project")
        or ""
    ).strip()
    if not access:
        raise AgyAuthError("agy token export does not contain an access token.", code="agy_access_token_missing")
    return AgyCredentials(access_token=access, refresh_token=refresh, expires_at_ms=expires, project_id=project)


def _refresh_via_oauth_client(credentials: AgyCredentials) -> Optional[AgyCredentials]:
    """Refresh using this plugin's OAuth client credentials (never agy CLI)."""
    if not credentials.refresh_token:
        return None
    try:
        from . import oauth_login
    except Exception:
        return None
    try:
        oauth_login.refresh_access_token(refresh_token=credentials.refresh_token)
        return load_credentials()
    except Exception:
        return None


def force_refresh_credentials() -> AgyCredentials:
    """Force a Google token refresh (plugin OAuth only)."""
    credentials = load_credentials()
    if not credentials.refresh_token:
        raise AgyAuthError(
            "No refresh token stored. Run: python3 scripts/google_antigravity_login.py interactive",
            code="agy_refresh_missing",
        )
    refreshed = _refresh_via_oauth_client(credentials)
    if refreshed is None or not refreshed.access_token:
        raise AgyAuthError(
            "OAuth refresh failed. Run: python3 scripts/google_antigravity_login.py interactive",
            code="agy_refresh_failed",
        )
    return refreshed


def valid_credentials(*, refresh: bool = True) -> AgyCredentials:
    try:
        credentials = load_credentials()
    except AgyAuthError:
        raise
    if credentials.expired:
        if refresh:
            return force_refresh_credentials()
        raise AgyAuthError("OAuth access token is expired.", code="agy_token_expired")
    return credentials


def status(*, probe: bool = False) -> Dict[str, Any]:
    path = token_file_path()
    result: Dict[str, Any] = {
        "enabled": security.agy_session_enabled(),
        "token_file": str(path),
        "token_file_present": path.is_file(),
        "credentials_readable": False,
        "access_token_present": False,
        "refresh_token_present": False,
        "expired": None,
        "project_id_present": False,
        "storage_boundary": "file-export-only; macOS Keychain is not inspected",
    }
    if not result["enabled"] or not path.is_file():
        return result
    try:
        credentials = valid_credentials(refresh=probe)
    except AgyAuthError as exc:
        result.update({"error_type": exc.code, "error": str(exc)})
        return result
    result.update(
        {
            "credentials_readable": True,
            "access_token_present": bool(credentials.access_token),
            "refresh_token_present": bool(credentials.refresh_token),
            "expired": credentials.expired,
            "project_id_present": bool(credentials.project_id),
        }
    )
    return result


def status_tool(_: Dict[str, Any] | None = None) -> Dict[str, Any]:
    state = status(probe=False)
    if state.get("credentials_readable"):
        text = "Plugin OAuth token is available."
    elif state.get("token_file_present"):
        text = "Plugin OAuth token exists but is not ready; inspect the reported validation error."
    else:
        text = (
            "No plugin OAuth token. Sign in with google_antigravity_login_start/"
            "login_complete or scripts/google_antigravity_login.py."
        )
    return {
        "text": text,
        **state,
    }


def refresh_tool(_: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Refresh the access token using the stored refresh_token (OAuth only)."""
    credentials = valid_credentials(refresh=True)
    # Force a refresh attempt when still unexpired so users can renew early.
    if credentials.refresh_token:
        refreshed = _refresh_via_oauth_client(credentials)
        if refreshed is not None:
            credentials = refreshed
    return {
        "text": "Plugin OAuth token refreshed via Google token endpoint.",
        "success": True,
        "backend": "agy-oauth-code-assist",
        "access_token_present": bool(credentials.access_token),
        "refresh_token_present": bool(credentials.refresh_token),
        "project_id_present": bool(credentials.project_id),
        "expires_at_ms": credentials.expires_at_ms or None,
    }
