"""xAI SuperGrok / Premium+ device-code OAuth (Hermes xai-oauth pattern)."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from . import paths

XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_DEVICE_CODE_URL = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
DEFAULT_BASE_URL = "https://api.x.ai/v1"
ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 3600


def token_path() -> Path:
    return paths.config_dir() / "oauth-token.json"


def pending_path() -> Path:
    return paths.config_dir() / "oauth-pending.json"


def _http_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    form: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    data = None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1500]
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc


def discovery(timeout: float = 15.0) -> Dict[str, str]:
    payload = _http_json("GET", XAI_OAUTH_DISCOVERY_URL, timeout=timeout)
    authorization_endpoint = str(payload.get("authorization_endpoint") or "").strip()
    token_endpoint = str(payload.get("token_endpoint") or "").strip()
    if not authorization_endpoint or not token_endpoint:
        raise RuntimeError("xAI OIDC discovery incomplete")
    if "x.ai" not in token_endpoint:
        raise RuntimeError(f"unexpected token_endpoint: {token_endpoint}")
    return {
        "authorization_endpoint": authorization_endpoint,
        "token_endpoint": token_endpoint,
    }


def request_device_code() -> Dict[str, Any]:
    return _http_json(
        "POST",
        XAI_OAUTH_DEVICE_CODE_URL,
        form={"client_id": XAI_OAUTH_CLIENT_ID, "scope": XAI_OAUTH_SCOPE},
    )


def poll_device_token(
    *,
    token_endpoint: str,
    device_code: str,
    expires_in: int,
    poll_interval: int,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(1, int(expires_in))
    interval = max(1, int(poll_interval))
    while time.monotonic() < deadline:
        try:
            payload = _http_json(
                "POST",
                token_endpoint,
                form={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": XAI_OAUTH_CLIENT_ID,
                    "device_code": device_code,
                },
            )
            if payload.get("access_token") and payload.get("refresh_token"):
                return payload
        except RuntimeError as exc:
            msg = str(exc)
            if "authorization_pending" in msg:
                time.sleep(interval)
                continue
            if "slow_down" in msg:
                interval = min(interval + 1, 30)
                time.sleep(interval)
                continue
            raise
        time.sleep(interval)
    raise RuntimeError("Timed out waiting for xAI device authorization")


def save_tokens(tokens: Dict[str, Any], *, discovery_info: Optional[Dict[str, str]] = None) -> Path:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "id_token": tokens.get("id_token"),
        "expires_in": tokens.get("expires_in"),
        "token_type": tokens.get("token_type") or "Bearer",
        "token_endpoint": (discovery_info or {}).get("token_endpoint"),
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "oauth-device-code",
        "base_url": os.getenv("XAI_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def load_tokens() -> Optional[Dict[str, Any]]:
    path = token_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _token_timing(data: Dict[str, Any]) -> tuple[Optional[bool], Optional[bool]]:
    """Return ``(valid, refresh_recommended)`` without refreshing the token."""

    if not data.get("access_token"):
        return False, None
    last = data.get("last_refresh")
    try:
        expires_in = int(data.get("expires_in") or 0)
        refreshed_at = datetime.fromisoformat(
            str(last).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return None, None
    if expires_in <= 0:
        return None, None
    age = time.time() - refreshed_at
    return (
        age < expires_in,
        age > max(60, expires_in - ACCESS_TOKEN_REFRESH_SKEW_SECONDS),
    )


def clear_tokens() -> bool:
    path = token_path()
    if path.is_file():
        path.unlink()
        return True
    return False


def refresh_tokens(refresh_token: str, *, token_endpoint: str = "") -> Dict[str, Any]:
    endpoint = token_endpoint.strip() or discovery()["token_endpoint"]
    if "x.ai" not in endpoint:
        raise RuntimeError(f"refusing non-xAI token_endpoint: {endpoint}")
    payload = _http_json(
        "POST",
        endpoint,
        form={
            "grant_type": "refresh_token",
            "client_id": XAI_OAUTH_CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
    access = str(payload.get("access_token") or "").strip()
    if not access:
        raise RuntimeError("xAI refresh missing access_token")
    return {
        "access_token": access,
        "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
        "id_token": str(payload.get("id_token") or "").strip(),
        "expires_in": payload.get("expires_in"),
        "token_type": str(payload.get("token_type") or "Bearer"),
        "token_endpoint": endpoint,
    }


def resolve_access_token() -> Optional[str]:
    data = load_tokens()
    if not data or not data.get("access_token"):
        return None
    # Proactive refresh when we have refresh_token (skew window).
    refresh = str(data.get("refresh_token") or "").strip()
    if refresh:
        # Always try refresh if last_refresh older than skew and expires_in known
        try:
            last = data.get("last_refresh")
            expires_in = int(data.get("expires_in") or 0)
            if last and expires_in:
                # naive: refresh if more than expires_in - skew elapsed
                from datetime import datetime

                ts = datetime.fromisoformat(str(last).replace("Z", "+00:00")).timestamp()
                age = time.time() - ts
                if age > max(60, expires_in - ACCESS_TOKEN_REFRESH_SKEW_SECONDS):
                    refreshed = refresh_tokens(refresh, token_endpoint=str(data.get("token_endpoint") or ""))
                    save_tokens(refreshed, discovery_info={"token_endpoint": refreshed.get("token_endpoint") or data.get("token_endpoint")})
                    return refreshed["access_token"]
        except Exception:
            pass
    return str(data["access_token"])


def start_login(*, open_browser: bool = True) -> Dict[str, Any]:
    disc = discovery()
    device = request_device_code()
    pending = {
        "device_code": device["device_code"],
        "user_code": device["user_code"],
        "verification_uri": device.get("verification_uri"),
        "verification_uri_complete": device.get("verification_uri_complete"),
        "expires_in": int(device.get("expires_in") or 900),
        "interval": int(device.get("interval") or 5),
        "token_endpoint": disc["token_endpoint"],
        "created_at": time.time(),
    }
    path = pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pending, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    url = str(pending.get("verification_uri_complete") or pending.get("verification_uri") or "")
    if open_browser and url:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return {
        "success": True,
        "verification_uri": pending.get("verification_uri"),
        "verification_uri_complete": pending.get("verification_uri_complete"),
        "user_code": pending.get("user_code"),
        "expires_in": pending.get("expires_in"),
        "interval": pending.get("interval"),
        "text": (
            f"Open {url} and approve access"
            + (f" (code {pending.get('user_code')})" if pending.get("user_code") else "")
            + ". Then call grok_codex_login_complete or run login.py complete."
        ),
    }


def complete_login(*, open_browser: bool = False) -> Dict[str, Any]:
    path = pending_path()
    if not path.is_file():
        raise RuntimeError("No pending login. Call login_start first.")
    pending = json.loads(path.read_text(encoding="utf-8"))
    tokens = poll_device_token(
        token_endpoint=str(pending["token_endpoint"]),
        device_code=str(pending["device_code"]),
        expires_in=int(pending.get("expires_in") or 900),
        poll_interval=int(pending.get("interval") or 5),
    )
    save_tokens(tokens, discovery_info={"token_endpoint": pending["token_endpoint"]})
    try:
        path.unlink()
    except OSError:
        pass
    return {
        "success": True,
        "text": "xAI SuperGrok OAuth login complete.",
        "token_file": str(token_path()),
    }


def interactive_login(*, open_browser: bool = True) -> Dict[str, Any]:
    start = start_login(open_browser=open_browser)
    print(start.get("text"))
    result = complete_login()
    return result


def status() -> Dict[str, Any]:
    data = load_tokens()
    if not data or not data.get("access_token"):
        return {
            "logged_in": False,
            "mode": "none",
            "hint": "python3 scripts/grok_codex_login.py interactive",
        }
    token_valid, refresh_recommended = _token_timing(data)
    return {
        "logged_in": True,
        "mode": "subscription_oauth",
        "token_file": str(token_path()),
        "last_refresh": data.get("last_refresh"),
        "has_refresh_token": bool(data.get("refresh_token")),
        "token_valid": token_valid,
        "refresh_recommended": refresh_recommended,
        "token_prefix": str(data.get("access_token") or "")[:8] + "…",
        "source": data.get("source"),
    }
