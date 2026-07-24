"""xAI SuperGrok / Premium+ device-code OAuth (Hermes xai-oauth pattern)."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import fcntl
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Dict, Optional

from agent_hub.core.auth_state import auth_state_lock, file_revision

from . import paths, security

XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_DEVICE_CODE_URL = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
DEFAULT_BASE_URL = "https://api.x.ai/v1"
ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 3600
PENDING_VERSION = 2


class LoginInProgressError(RuntimeError):
    pass


def _trusted_xai_url(url: str, *, host: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == host
        and port in {None, 443}
        and not parsed.username
        and not parsed.password
    )


def token_path() -> Path:
    return paths.config_dir() / "oauth-token.json"


def pending_path() -> Path:
    return paths.config_dir() / "oauth-pending.json"


@contextmanager
def _pending_lock():
    path = pending_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path.parent / ".oauth-pending.lock",
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _existing_pending_is_active(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(
            "Existing xAI login state is invalid; clear it before starting again."
        ) from exc
    if not isinstance(pending, dict):
        raise RuntimeError(
            "Existing xAI login state is invalid; clear it before starting again."
        )
    created_at = float(pending.get("created_at") or 0)
    expires_in = int(pending.get("expires_in") or 900)
    return bool(created_at and time.time() < created_at + max(1, expires_in))


def _load_pending(*, expected_flow_id: str | None = None) -> Dict[str, Any]:
    path = pending_path()
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError("No pending login. Call login_start first.") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(
            "Existing xAI login state is invalid; clear it and start again."
        ) from exc
    flow_id = str(pending.get("flow_id") or "") if isinstance(pending, dict) else ""
    if (
        not isinstance(pending, dict)
        or pending.get("version") != PENDING_VERSION
        or not flow_id
        or not pending.get("consent_revision")
    ):
        raise RuntimeError(
            "Existing xAI login state predates flow ownership; clear it and start again."
        )
    if expected_flow_id and not secrets.compare_digest(flow_id, expected_flow_id):
        raise RuntimeError("The xAI login flow was replaced; start again.")
    created_at = float(pending.get("created_at") or 0)
    expires_in = int(pending.get("expires_in") or 900)
    if not created_at or time.time() >= created_at + max(1, expires_in):
        raise RuntimeError("The xAI login flow expired; start again.")
    return pending


def _write_private_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return path
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return path


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
        oauth_error = "oauth_http_error"
        try:
            payload = json.loads(
                exc.read(4096).decode("utf-8", errors="replace")
            )
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and payload.get("error") in {
            "access_denied",
            "authorization_pending",
            "expired_token",
            "slow_down",
        }:
            oauth_error = str(payload["error"])
        raise RuntimeError(f"HTTP {exc.code}: {oauth_error}") from exc


def discovery(timeout: float = 15.0) -> Dict[str, str]:
    payload = _http_json("GET", XAI_OAUTH_DISCOVERY_URL, timeout=timeout)
    authorization_endpoint = str(payload.get("authorization_endpoint") or "").strip()
    token_endpoint = str(payload.get("token_endpoint") or "").strip()
    if not authorization_endpoint or not token_endpoint:
        raise RuntimeError("xAI OIDC discovery incomplete")
    if not _trusted_xai_url(token_endpoint, host="auth.x.ai"):
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
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    if not _trusted_xai_url(token_endpoint, host="auth.x.ai"):
        raise RuntimeError(f"refusing non-xAI token_endpoint: {token_endpoint}")
    deadline = time.monotonic() + max(1, int(expires_in))
    interval = max(1, int(poll_interval))
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("xAI device authorization cancelled")
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
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("xAI device authorization cancelled")
                return payload
        except RuntimeError as exc:
            msg = str(exc)
            if "authorization_pending" in msg:
                if cancel_event is not None:
                    cancel_event.wait(interval)
                else:
                    time.sleep(interval)
                continue
            if "slow_down" in msg:
                interval = min(interval + 1, 30)
                if cancel_event is not None:
                    cancel_event.wait(interval)
                else:
                    time.sleep(interval)
                continue
            raise
        if cancel_event is not None:
            cancel_event.wait(interval)
        else:
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
    return _write_private_json(path, payload)


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


def clear_pending_login(*, expected_flow_id: str | None = None) -> bool:
    path = pending_path()
    with _pending_lock():
        if expected_flow_id:
            try:
                pending = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return False
            current_flow_id = (
                str(pending.get("flow_id") or "")
                if isinstance(pending, dict)
                else ""
            )
            if not current_flow_id or not secrets.compare_digest(
                current_flow_id,
                expected_flow_id,
            ):
                return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True


def clear_unusable_pending_login() -> bool:
    """Remove only stale or unowned pending state so the GUI can safely recover."""

    path = pending_path()
    with auth_state_lock(paths.config_dir()):
        with _pending_lock():
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return False
            except OSError:
                return False
            try:
                pending = json.loads(raw)
            except (ValueError, TypeError):
                pending = None
            unusable = not isinstance(pending, dict)
            if isinstance(pending, dict):
                try:
                    created_at = float(pending.get("created_at") or 0)
                    expires_in = int(pending.get("expires_in") or 900)
                except (TypeError, ValueError):
                    created_at = 0
                    expires_in = 900
                consent_revision = str(pending.get("consent_revision") or "")
                unusable = bool(
                    pending.get("version") != PENDING_VERSION
                    or not str(pending.get("flow_id") or "")
                    or not consent_revision
                    or not created_at
                    or time.time() >= created_at + max(1, expires_in)
                    or not secrets.compare_digest(
                        consent_revision,
                        security.consent_revision(),
                    )
                )
            if not unusable:
                return False
            path.unlink()
            return True


def clear_tokens() -> bool:
    removed = False
    with auth_state_lock(paths.config_dir()):
        try:
            token_path().unlink()
        except FileNotFoundError:
            pass
        else:
            removed = True
        with _pending_lock():
            try:
                pending_path().unlink()
            except FileNotFoundError:
                pass
            else:
                removed = True
    return removed


def refresh_tokens(refresh_token: str, *, token_endpoint: str = "") -> Dict[str, Any]:
    endpoint = token_endpoint.strip() or discovery()["token_endpoint"]
    if not _trusted_xai_url(endpoint, host="auth.x.ai"):
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
    with auth_state_lock(paths.config_dir()):
        if not security.user_consent_enabled():
            return None
        consent_revision = security.consent_revision()
        credential_revision = file_revision(token_path())
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
                    refreshed = refresh_tokens(
                        refresh,
                        token_endpoint=str(data.get("token_endpoint") or ""),
                    )
                    with auth_state_lock(paths.config_dir()):
                        if (
                            not security.user_consent_enabled()
                            or not secrets.compare_digest(
                                security.consent_revision(),
                                consent_revision,
                            )
                            or not secrets.compare_digest(
                                file_revision(token_path()),
                                credential_revision,
                            )
                        ):
                            return None
                        save_tokens(
                            refreshed,
                            discovery_info={
                                "token_endpoint": (
                                    refreshed.get("token_endpoint")
                                    or data.get("token_endpoint")
                                )
                            },
                        )
                        return refreshed["access_token"]
        except Exception:
            pass
    with auth_state_lock(paths.config_dir()):
        if (
            not security.user_consent_enabled()
            or not secrets.compare_digest(
                security.consent_revision(),
                consent_revision,
            )
            or not secrets.compare_digest(
                file_revision(token_path()),
                credential_revision,
            )
        ):
            return None
    return str(data["access_token"])


def start_login(*, open_browser: bool = True) -> Dict[str, Any]:
    with auth_state_lock(paths.config_dir()):
        security.require_consent()
        starting_consent_revision = security.consent_revision()
        with _pending_lock():
            if _existing_pending_is_active(pending_path()):
                raise LoginInProgressError(
                    "An xAI login is already in progress; complete or cancel it first."
                )

    disc = discovery()
    device = request_device_code()
    path = pending_path()
    flow_id = secrets.token_urlsafe(18)
    with auth_state_lock(paths.config_dir()):
        security.require_consent()
        current_revision = security.consent_revision()
        if not secrets.compare_digest(current_revision, starting_consent_revision):
            raise RuntimeError("xAI consent changed while login was starting; start again.")
        with _pending_lock():
            if _existing_pending_is_active(path):
                raise LoginInProgressError(
                    "An xAI login is already in progress; complete or cancel it first."
                )
            path.unlink(missing_ok=True)
            pending = {
                "version": PENDING_VERSION,
                "flow_id": flow_id,
                "consent_revision": current_revision,
                "device_code": device["device_code"],
                "user_code": device["user_code"],
                "verification_uri": device.get("verification_uri"),
                "verification_uri_complete": device.get("verification_uri_complete"),
                "expires_in": int(device.get("expires_in") or 900),
                "interval": int(device.get("interval") or 5),
                "token_endpoint": disc["token_endpoint"],
                "created_at": time.time(),
            }
            _write_private_json(path, pending)
    url = str(pending.get("verification_uri_complete") or pending.get("verification_uri") or "")
    if open_browser and url:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return {
        "success": True,
        "flow_id": flow_id,
        "verification_uri": pending.get("verification_uri"),
        "verification_uri_complete": pending.get("verification_uri_complete"),
        "user_code": pending.get("user_code"),
        "expires_in": pending.get("expires_in"),
        "interval": pending.get("interval"),
        "text": (
            f"Open {url} and approve access"
            + (f" (code {pending.get('user_code')})" if pending.get("user_code") else "")
            + ". Then return to Agent Hub 연결 관리 or run login.py complete."
        ),
    }


def complete_login(
    *,
    open_browser: bool = False,
    cancel_event: Optional[threading.Event] = None,
    expected_flow_id: str | None = None,
    commit_guard: Callable[[], ContextManager[Any]] | None = None,
) -> Dict[str, Any]:
    with _pending_lock():
        pending = _load_pending(expected_flow_id=expected_flow_id)
    flow_id = str(pending["flow_id"])
    tokens = poll_device_token(
        token_endpoint=str(pending["token_endpoint"]),
        device_code=str(pending["device_code"]),
        expires_in=int(pending.get("expires_in") or 900),
        poll_interval=int(pending.get("interval") or 5),
        cancel_event=cancel_event,
    )
    warnings: list[str] = []
    with auth_state_lock(paths.config_dir()):
        with _pending_lock():
            current = _load_pending(expected_flow_id=flow_id)
            if not security.user_consent_enabled():
                raise RuntimeError("xAI consent was revoked before login completed.")
            current_revision = security.consent_revision()
            if not secrets.compare_digest(
                current_revision,
                str(current["consent_revision"]),
            ):
                raise RuntimeError("xAI consent changed before login completed.")
            guard = commit_guard() if commit_guard is not None else nullcontext()
            with guard:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("xAI device authorization cancelled")
                save_tokens(
                    tokens,
                    discovery_info={"token_endpoint": pending["token_endpoint"]},
                )
                try:
                    pending_path().unlink()
                except OSError:
                    warnings.append("pending_cleanup_failed")
    return {
        "success": True,
        "text": "xAI SuperGrok OAuth login complete.",
        "token_file_present": token_path().is_file(),
        "warnings": warnings,
    }


def interactive_login(*, open_browser: bool = True) -> Dict[str, Any]:
    start = start_login(open_browser=open_browser)
    print(start.get("text"))
    result = complete_login(expected_flow_id=str(start["flow_id"]))
    return result


def status() -> Dict[str, Any]:
    token_file_present = token_path().is_file()
    pending_login_present = pending_path().is_file()
    data = load_tokens()
    if not data or not data.get("access_token"):
        return {
            "logged_in": False,
            "mode": "none",
            "token_file_present": token_file_present,
            "pending_login_present": pending_login_present,
            "hint": "python3 scripts/grok_codex_login.py interactive",
        }
    token_valid, refresh_recommended = _token_timing(data)
    return {
        "logged_in": True,
        "mode": "subscription_oauth",
        "token_file_present": token_file_present,
        "pending_login_present": pending_login_present,
        "last_refresh": data.get("last_refresh"),
        "has_refresh_token": bool(data.get("refresh_token")),
        "token_valid": token_valid,
        "refresh_recommended": refresh_recommended,
        "source": data.get("source"),
    }
