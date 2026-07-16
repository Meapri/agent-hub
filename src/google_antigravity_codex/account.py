"""Account identity (whoami) and logout / forget tokens."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from . import agy_auth, oauth_login, paths, response, security

USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
TOKENINFO_URL = "https://www.googleapis.com/oauth2/v3/tokeninfo"


class AccountError(RuntimeError):
    def __init__(self, message: str, *, code: str = "account_error") -> None:
        super().__init__(message)
        self.code = code


def _safe_get_json(url: str, *, access_token: str = "", timeout: float = 15.0) -> Dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "google-antigravity-codex"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as resp:
            raw = resp.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        raise AccountError(f"HTTP {exc.code} from identity endpoint.", code="identity_http_error") from exc
    except urllib.error.URLError as exc:
        raise AccountError("Network error calling identity endpoint.", code="identity_network_error") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AccountError("Invalid identity JSON.", code="identity_invalid") from exc
    return data if isinstance(data, dict) else {}


def whoami(_: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Return non-secret account / project facts for the active token."""
    if not security.agy_session_enabled():
        raise AccountError("Consent required for account inspection.", code="consent_required")
    token_path = agy_auth.token_file_path()
    present = token_path.is_file()
    result: Dict[str, Any] = {
        "success": False,
        "token_file": str(token_path),
        "token_file_present": present,
        "email": None,
        "email_verified": None,
        "name": None,
        "project_id_present": False,
        "project_id": None,
        "token_scope_hint": None,
        "expires": None,
        "provider_hint": "agy-oauth" if present else None,
    }
    if not present:
        result["text"] = "No local OAuth token file; run google_antigravity_login_start/complete first."
        result.update(
            response.standard_fields(success=False, backend="account", warnings=["token_missing"])
        )
        return result

    try:
        creds = agy_auth.valid_credentials(refresh=True)
    except Exception as exc:
        result["text"] = f"Token present but not usable ({getattr(exc, 'code', type(exc).__name__)})."
        result["error_type"] = getattr(exc, "code", type(exc).__name__)
        result.update(
            response.standard_fields(success=False, backend="account", warnings=["token_unusable"])
        )
        return result

    access = creds.access_token
    # Never include access in output.
    email = ""
    name = ""
    verified = None
    try:
        info = _safe_get_json(USERINFO_URL, access_token=access)
        email = str(info.get("email") or "")
        name = str(info.get("name") or info.get("given_name") or "")
        verified = info.get("email_verified")
    except AccountError:
        try:
            # tokeninfo accepts access_token as query param; still omit from response
            url = TOKENINFO_URL + "?" + urllib.parse.urlencode({"access_token": access})
            info = _safe_get_json(url)
            email = str(info.get("email") or "")
            result["token_scope_hint"] = str(info.get("scope") or "")[:200] or None
        except AccountError:
            pass

    project_id = str(creds.project_id or "")
    if not project_id:
        try:
            from . import antigravity_api

            project_id = antigravity_api.resolve_project_id(creds)
        except Exception:
            project_id = ""

    # Persist non-secret email onto token file if missing
    if email:
        try:
            data = json.loads(token_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and not data.get("email"):
                data["email"] = email
                token_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                os.chmod(token_path, 0o600)
        except Exception:
            pass

    result.update(
        {
            "success": True,
            "email": email or None,
            "email_verified": verified,
            "name": name or None,
            "project_id": project_id or None,
            "project_id_present": bool(project_id),
            "expires": creds.expires_at_ms or None,
            "expired": creds.expired,
            "text": (
                f"Signed in as {email or '(email unavailable)'}"
                + (f" · project={project_id}" if project_id else "")
            ),
        }
    )
    # Ensure no secrets leaked
    blob = json.dumps(result)
    if access and access in blob:
        raise AccountError("Refusing to return payload that embeds access token.", code="secret_leak_blocked")
    result.update(response.standard_fields(backend="account"))
    return result


def _unlink(path: Path) -> bool:
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        return False
    return False


def logout(arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Forget local plugin tokens (does not revoke at Google or touch Keychain)."""
    args = arguments or {}
    forget_client = bool(args.get("forget_client") or args.get("forget_oauth_client"))
    candidates = list(agy_auth.candidate_token_paths())
    candidates.extend(
        [
            oauth_login.pending_file_path(),
            paths.config_dir() / "oauth-login-pending.json",
        ]
    )
    if forget_client:
        candidates.extend(
            [
                oauth_login.client_file_path(),
                paths.config_dir() / "oauth-client.json",
            ]
        )
    seen: set[str] = set()
    unique: List[str] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if _unlink(path):
            unique.append(key)

    return {
        "text": (
            f"Logged out locally; removed {len(unique)} file(s). "
            "Google-side revoke and agy Keychain were not modified."
        ),
        "success": True,
        "removed": unique,
        "forget_client": forget_client,
        **response.standard_fields(backend="account"),
    }
