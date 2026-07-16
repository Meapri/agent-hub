"""Direct Google Antigravity OAuth login (PKCE), adapted from Hermes plugin.

Flow matches ``Meapri/hermes-google-antigravity-plugin``:
authorization-code + PKCE against accounts.google.com, redirect to either a
local loopback server or ``https://antigravity.google/oauth-callback``, then
token exchange. Tokens are stored for the ``agy-oauth`` provider path.

OAuth client credentials resolve in order:
1. ``GOOGLE_ANTIGRAVITY_CLIENT_ID`` / ``GOOGLE_ANTIGRAVITY_CLIENT_SECRET``
2. Config file (``oauth-client.json``)
3. Hermes client file (``~/.hermes/auth/google_antigravity_client.json``)
4. Built-in installed-app clients that ship inside the official ``agy`` binary
   (same "ok to embed for installed apps" pattern as Gemini CLI)

MCP cannot drive an interactive browser alone, so this module exposes:
- ``start_login()`` → auth URL + pending state (for Codex GUI / MCP)
- ``complete_login(code_or_url)`` → token exchange
- ``run_interactive_login()`` → full CLI flow with local callback + paste
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import io_util, paths, security

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
EXTERNAL_REDIRECT = "https://antigravity.google/oauth-callback"
LOCAL_PORT = 51121
LOCAL_REDIRECT = f"http://localhost:{LOCAL_PORT}/auth/callback"
SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile "
    "https://www.googleapis.com/auth/cclog "
    "https://www.googleapis.com/auth/experimentsandconfigs openid"
)

# Installed-app OAuth clients from Antigravity IDE
# (out-build/vs/platform/cloudCode/common/oauthClient.js).
# Pairing is client_id ↔ secret as in that module (NOT order from binary strings).
# User env/file overrides always win.
_BUILTIN_CLIENTS: Tuple[Dict[str, str], ...] = (
    {
        # B1e / k1e — Cloud Code primary
        "client_id": "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com",
        "client_secret": "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",
        "label": "antigravity-cloudcode-primary",
    },
    {
        # u7e / c7e — secondary
        "client_id": "884354919052-36trc1jjb3tguiac32ov6cod268c5blh.apps.googleusercontent.com",
        "client_secret": "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",
        "label": "antigravity-cloudcode-secondary",
    },
)

PENDING_TTL_SEC = 900


class OAuthLoginError(RuntimeError):
    def __init__(self, message: str, *, code: str = "oauth_login_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str
    label: str = "configured"


def client_file_path() -> Path:
    override = os.getenv("GOOGLE_ANTIGRAVITY_CLIENT_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return paths.config_dir() / "oauth-client.json"


def hermes_client_file_path() -> Path:
    return Path("~/.hermes/auth/google_antigravity_client.json").expanduser()


def token_file_path() -> Path:
    """Primary token path written by this plugin's direct OAuth login."""
    # Keep write target aligned with agy_auth.plugin_token_path (not "first existing").
    override = os.getenv("GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    try:
        from . import agy_auth

        return agy_auth.plugin_token_path()
    except Exception:
        return paths.config_dir() / "oauth-token.json"


def pending_file_path() -> Path:
    return paths.config_dir() / "oauth-login-pending.json"


def _pkce() -> Tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _read_json_object(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _client_from_mapping(data: Dict[str, Any], *, label: str) -> Optional[OAuthClient]:
    cid = str(data.get("client_id") or data.get("clientId") or "").strip()
    csec = str(data.get("client_secret") or data.get("clientSecret") or "").strip()
    if cid and csec:
        return OAuthClient(client_id=cid, client_secret=csec, label=label)
    return None


def resolve_oauth_clients() -> List[OAuthClient]:
    """Return candidate OAuth clients in preference order (deduped by client_id)."""
    found: List[OAuthClient] = []
    seen: set[str] = set()

    def add(client: Optional[OAuthClient]) -> None:
        if client is None or client.client_id in seen:
            return
        seen.add(client.client_id)
        found.append(client)

    env_id = os.getenv("GOOGLE_ANTIGRAVITY_CLIENT_ID", "").strip()
    env_secret = os.getenv("GOOGLE_ANTIGRAVITY_CLIENT_SECRET", "").strip()
    if env_id and env_secret:
        add(OAuthClient(client_id=env_id, client_secret=env_secret, label="environment"))

    for path, label in (
        (client_file_path(), "config-file"),
        (hermes_client_file_path(), "hermes-client-file"),
    ):
        data = _read_json_object(path)
        if data:
            add(_client_from_mapping(data, label=label))

    for raw in _BUILTIN_CLIENTS:
        add(
            OAuthClient(
                client_id=raw["client_id"],
                client_secret=raw["client_secret"],
                label=raw.get("label", "builtin"),
            )
        )

    if not found:
        raise OAuthLoginError(
            "No OAuth client credentials available. Set GOOGLE_ANTIGRAVITY_CLIENT_ID/"
            "GOOGLE_ANTIGRAVITY_CLIENT_SECRET or write oauth-client.json under the config dir.",
            code="oauth_client_missing",
        )
    return found


def save_oauth_client(client: OAuthClient) -> Path:
    path = client_file_path()
    return io_util.write_json_secure(
        path,
        {
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "label": client.label,
        },
    )


def save_tokens(
    *,
    access_token: str,
    refresh_token: str = "",
    expires_in: int = 3600,
    project_id: str = "",
    email: str = "",
) -> Path:
    path = token_file_path()
    existing = _read_json_object(path) or {}
    if not email:
        email = str(existing.get("email") or "")
    if not project_id:
        project_id = str(existing.get("project_id") or existing.get("cloudaicompanion_project") or "")
    if not refresh_token:
        refresh_token = str(existing.get("refresh") or existing.get("refresh_token") or "")
    payload = {
        "access": access_token,
        "access_token": access_token,
        "refresh": refresh_token,
        "refresh_token": refresh_token,
        "expires": int((time.time() + max(60, int(expires_in or 3600))) * 1000),
        "expires_at_ms": int((time.time() + max(60, int(expires_in or 3600))) * 1000),
        "email": email,
    }
    if project_id:
        payload["project_id"] = project_id
    return io_util.write_json_secure(path, payload)


def _build_auth_url(client: OAuthClient, *, redirect_uri: str, challenge: str, state: str) -> str:
    params = {
        "access_type": "offline",
        "client_id": client.client_id,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def _parse_code(raw: str) -> Tuple[str, str]:
    text = (raw or "").strip()
    if not text:
        return "", ""
    if "code=" in text:
        query = urllib.parse.urlparse(text).query or text.split("?", 1)[-1]
        parsed = urllib.parse.parse_qs(query)
        return (parsed.get("code") or [""])[0], (parsed.get("state") or [""])[0]
    return text, ""


def _exchange(
    *,
    client: OAuthClient,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> Dict[str, Any]:
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode()
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = str(exc)
        raise OAuthLoginError(
            f"Token exchange failed (HTTP {exc.code}). {detail}",
            code="oauth_token_exchange_failed",
        ) from exc
    except urllib.error.URLError as exc:
        raise OAuthLoginError("Token exchange network error.", code="oauth_network_error") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise OAuthLoginError("Token exchange returned no access token.", code="oauth_token_missing")
    return payload


def refresh_access_token(*, refresh_token: str, client: Optional[OAuthClient] = None) -> Dict[str, Any]:
    clients = [client] if client else resolve_oauth_clients()
    last_error: Optional[Exception] = None
    for candidate in clients:
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": candidate.client_id,
                "client_secret": candidate.client_secret,
            }
        ).encode()
        request = urllib.request.Request(
            TOKEN_ENDPOINT,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — try next client
            last_error = exc
            continue
        if isinstance(payload, dict) and payload.get("access_token"):
            save_oauth_client(candidate)
            path = save_tokens(
                access_token=str(payload["access_token"]),
                refresh_token=str(payload.get("refresh_token") or refresh_token),
                expires_in=int(payload.get("expires_in") or 3600),
            )
            return {
                "success": True,
                "token_file": str(path),
                "client_label": candidate.label,
                "expires_in": int(payload.get("expires_in") or 3600),
            }
    raise OAuthLoginError(
        f"Refresh token exchange failed with all candidate clients ({last_error}).",
        code="oauth_refresh_failed",
    )


def start_login(*, use_local_redirect: bool = True) -> Dict[str, Any]:
    """Begin OAuth; returns a browser URL. Does not open a browser itself."""
    if not security.agy_session_enabled():
        raise OAuthLoginError(
            "Direct Antigravity login requires consent. Run "
            "`python3 scripts/google_antigravity_consent.py grant --i-understand-and-consent` first.",
            code="consent_required",
        )
    clients = resolve_oauth_clients()
    client = clients[0]
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    redirect = LOCAL_REDIRECT if use_local_redirect else EXTERNAL_REDIRECT
    auth_url = _build_auth_url(client, redirect_uri=redirect, challenge=challenge, state=state)
    # Do not persist client_secret on disk in the pending file; re-resolve at complete.
    pending = {
        "created_at": time.time(),
        "state": state,
        "verifier": verifier,
        "redirect_uri": redirect,
        "client_id": client.client_id,
        "client_label": client.label,
        "candidate_client_ids": [c.client_id for c in clients],
    }
    path = pending_file_path()
    io_util.write_json_secure(path, pending)
    return {
        "text": (
            "Open the authorization URL in a browser, sign in with Google, then call "
            "google_antigravity_login_complete with the full redirect URL or the code= value."
        ),
        "success": True,
        "auth_url": auth_url,
        "redirect_uri": redirect,
        "state": state,
        "client_label": client.label,
        "pending_file": str(path),
        "expires_in_sec": PENDING_TTL_SEC,
        "instructions": [
            "1. Open auth_url in a browser and sign in with your Google account.",
            "2. After redirect, copy the full URL (or the code= value).",
            "3. Call google_antigravity_login_complete with that value.",
            "Local callback port 51121 also works if you tunnel: ssh -L 51121:localhost:51121 <host>",
        ],
    }


def _load_pending() -> Dict[str, Any]:
    data = _read_json_object(pending_file_path())
    if not data:
        raise OAuthLoginError("No pending login. Call google_antigravity_login_start first.", code="oauth_pending_missing")
    created = float(data.get("created_at") or 0)
    if created and time.time() - created > PENDING_TTL_SEC:
        raise OAuthLoginError("Pending login expired. Start again.", code="oauth_pending_expired")
    return data


def _probe_login(*, timeout: float = 45.0) -> Dict[str, Any]:
    """Verify saved tokens can list models (or fall back to a tiny chat)."""
    try:
        from . import antigravity_api
    except Exception as exc:  # pragma: no cover
        return {"success": False, "error_type": type(exc).__name__, "error": str(exc)}
    try:
        models = antigravity_api.list_models()
        return {
            "success": True,
            "method": "list_models",
            "model_count": len(models),
            "sample_models": [m.get("id") for m in models[:5] if isinstance(m, dict)],
        }
    except Exception as list_exc:
        try:
            from . import chat as chat_mod

            result = chat_mod.run_chat(
                {
                    "prompt": "Reply with exactly OK.",
                    "model": "gemini-3.5-flash",
                    "max_tokens": 32,
                    "temperature": 0,
                    "timeout_sec": int(timeout),
                    "retry_count": 0,
                }
            )
            return {
                "success": bool(result.get("success") or result.get("text")),
                "method": "chat_probe",
                "backend": result.get("backend"),
                "list_models_error": str(list_exc),
            }
        except Exception as chat_exc:
            return {
                "success": False,
                "method": "list_models+chat_probe",
                "error_type": getattr(chat_exc, "code", type(chat_exc).__name__),
                "error": str(chat_exc),
                "list_models_error": str(list_exc),
            }


def complete_login(code_or_url: str, *, probe: bool = True) -> Dict[str, Any]:
    """Finish a started login with a pasted redirect URL or authorization code."""
    if not security.agy_session_enabled():
        raise OAuthLoginError("Direct Antigravity login requires consent.", code="consent_required")
    code, rstate = _parse_code(code_or_url)
    if not code:
        raise OAuthLoginError("No authorization code found in the pasted value.", code="oauth_code_missing")
    pending = _load_pending()
    expected_state = str(pending.get("state") or "")
    if rstate and expected_state and rstate != expected_state:
        raise OAuthLoginError("OAuth state mismatch.", code="oauth_state_mismatch")

    pending_client_id = str(pending.get("client_id") or "").strip()
    if not pending_client_id:
        raise OAuthLoginError("Pending login is missing client id.", code="oauth_client_missing")

    # Rebuild candidates: preferred pending client first, secrets from live resolver.
    by_id = {c.client_id: c for c in resolve_oauth_clients()}
    if pending_client_id not in by_id:
        # Allow completing with only pending client_id if env/file has matching secret later.
        raise OAuthLoginError(
            "Pending login client is no longer available. Start login again.",
            code="oauth_client_missing",
        )
    preferred = by_id[pending_client_id]
    preferred = OAuthClient(
        client_id=preferred.client_id,
        client_secret=preferred.client_secret,
        label=str(pending.get("client_label") or preferred.label),
    )
    candidates = [preferred]
    for other in by_id.values():
        if other.client_id != preferred.client_id:
            candidates.append(other)

    redirect_uri = str(pending.get("redirect_uri") or EXTERNAL_REDIRECT)
    verifier = str(pending.get("verifier") or "")
    last_error: Optional[Exception] = None
    token_payload: Optional[Dict[str, Any]] = None
    used: Optional[OAuthClient] = None
    for candidate in candidates:
        try:
            token_payload = _exchange(
                client=candidate,
                code=code,
                verifier=verifier,
                redirect_uri=redirect_uri,
            )
            used = candidate
            break
        except OAuthLoginError as exc:
            last_error = exc
            continue
    if not token_payload or not used:
        raise OAuthLoginError(
            f"Token exchange failed for all candidate clients ({last_error}).",
            code="oauth_token_exchange_failed",
        )

    path = save_tokens(
        access_token=str(token_payload["access_token"]),
        refresh_token=str(token_payload.get("refresh_token") or ""),
        expires_in=int(token_payload.get("expires_in") or 3600),
    )
    save_oauth_client(used)
    try:
        pending_file_path().unlink(missing_ok=True)
    except TypeError:
        if pending_file_path().exists():
            pending_file_path().unlink()
    except OSError:
        pass

    result: Dict[str, Any] = {
        "text": (
            f"Google Antigravity login succeeded. Tokens saved for agy-oauth "
            f"(client={used.label}). Provider auto-selects agy-oauth for grounding/image."
        ),
        "success": True,
        "token_file": str(path),
        "client_label": used.label,
        "access_token_present": True,
        "refresh_token_present": bool(token_payload.get("refresh_token")),
        "provider_hint": "agy-oauth",
    }
    if probe:
        probe_result = _probe_login()
        result["probe"] = probe_result
        if probe_result.get("success"):
            count = probe_result.get("model_count")
            extra = f" Probe OK ({probe_result.get('method')}"
            if count is not None:
                extra += f", {count} models"
            extra += ")."
            result["text"] += extra
        else:
            result["text"] += (
                " Tokens saved, but live probe failed — re-check project/network "
                f"({probe_result.get('error_type') or 'probe_failed'})."
            )
            result["warnings"] = ["login_probe_failed"]
    return result


def run_interactive_login(
    *,
    use_local_server: bool = True,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    open_browser: bool = True,
) -> Dict[str, Any]:
    """Full interactive login (local callback server + concurrent paste)."""
    if not security.agy_session_enabled():
        raise OAuthLoginError("Direct Antigravity login requires consent.", code="consent_required")

    clients = resolve_oauth_clients()
    client = clients[0]
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    box: Dict[str, str] = {}
    server: Optional[http.server.HTTPServer] = None
    redirect = LOCAL_REDIRECT if use_local_server else EXTERNAL_REDIRECT

    if use_local_server:

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                box["code"] = (parsed.get("code") or [""])[0]
                box["state"] = (parsed.get("state") or [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<h2>Antigravity login complete. You can close this tab.</h2>"
                )

        try:
            server = http.server.HTTPServer(("127.0.0.1", LOCAL_PORT), Handler)
            threading.Thread(target=server.handle_request, daemon=True).start()
        except OSError:
            server = None
            redirect = EXTERNAL_REDIRECT

    auth_url = _build_auth_url(client, redirect_uri=redirect, challenge=challenge, state=state)
    # Persist pending so complete_login path stays consistent if user pastes later via MCP.
    # Never write client_secret into the pending file.
    io_util.write_json_secure(
        pending_file_path(),
        {
            "created_at": time.time(),
            "state": state,
            "verifier": verifier,
            "redirect_uri": redirect,
            "client_id": client.client_id,
            "client_label": client.label,
        },
    )

    print_fn("\n[1] Open this URL in a browser and sign in with Google:\n")
    print_fn(auth_url + "\n")
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(auth_url)
        except Exception:
            pass

    if server is not None:
        print_fn("[2] Waiting for localhost callback (or paste the redirected URL/code).")
        print_fn("    Remote/headless: ssh -L 51121:localhost:51121 <host>  OR paste the URL.\n")

        def _stdin_reader() -> None:
            try:
                raw = input_fn("Paste redirected URL or code (or wait for auto-capture): ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not raw or box.get("code"):
                return
            code, rstate = _parse_code(raw)
            box["code"] = code
            if rstate:
                box["state"] = rstate

        threading.Thread(target=_stdin_reader, daemon=True).start()
        deadline = time.time() + 300
        while not box.get("code") and time.time() < deadline:
            time.sleep(0.5)
        try:
            server.server_close()
        except Exception:
            pass
        code = box.get("code", "")
        rstate = box.get("state", "")
    else:
        print_fn("[2] After login, paste the redirect URL or the code= value.\n")
        raw = input_fn("Paste callback URL or code: ").strip()
        code, rstate = _parse_code(raw)

    if not code:
        raise OAuthLoginError("No authorization code received.", code="oauth_code_missing")
    if rstate and rstate != state:
        raise OAuthLoginError("OAuth state mismatch.", code="oauth_state_mismatch")

    # Prefer the client used to start; fall back across candidates on exchange failure.
    last_error: Optional[Exception] = None
    for candidate in clients:
        try:
            payload = _exchange(
                client=candidate,
                code=code,
                verifier=verifier,
                redirect_uri=redirect,
            )
            path = save_tokens(
                access_token=str(payload["access_token"]),
                refresh_token=str(payload.get("refresh_token") or ""),
                expires_in=int(payload.get("expires_in") or 3600),
            )
            save_oauth_client(candidate)
            try:
                pending_file_path().unlink()
            except OSError:
                pass
            print_fn(f"\n[OK] Logged in → {path}  (client={candidate.label})")
            return {
                "success": True,
                "token_file": str(path),
                "client_label": candidate.label,
                "access_token": payload["access_token"],
                "refresh_token": payload.get("refresh_token") or "",
            }
        except OAuthLoginError as exc:
            last_error = exc
            continue
    raise OAuthLoginError(str(last_error or "login failed"), code="oauth_token_exchange_failed")


def login_status() -> Dict[str, Any]:
    token_path = token_file_path()
    client_path = client_file_path()
    pending_path = pending_file_path()
    token_present = token_path.is_file()
    readable = False
    expired: Optional[bool] = None
    refresh_present = False
    if token_present:
        data = _read_json_object(token_path) or {}
        token = data.get("token") if isinstance(data.get("token"), dict) else data
        if isinstance(token, dict):
            access = str(token.get("access") or token.get("access_token") or "")
            refresh_present = bool(token.get("refresh") or token.get("refresh_token"))
            readable = bool(access)
            exp = token.get("expires") or token.get("expires_at") or token.get("expires_at_ms") or 0
            try:
                exp_ms = int(float(exp))
                if exp_ms and exp_ms < 10_000_000_000:
                    exp_ms *= 1000
                expired = exp_ms > 0 and exp_ms <= int(time.time() * 1000) + 60_000
            except (TypeError, ValueError):
                expired = None
    return {
        "text": (
            "Direct OAuth token is ready."
            if readable and expired is not True
            else "Direct OAuth token is not ready; run login."
        ),
        "consent": security.agy_session_enabled(),
        "token_file": str(token_path),
        "token_file_present": token_present,
        "credentials_readable": readable,
        "refresh_token_present": refresh_present,
        "expired": expired,
        "client_file": str(client_path),
        "client_file_present": client_path.is_file(),
        "pending_login": pending_path.is_file(),
        "success": readable and expired is not True,
    }


def start_tool(_: Dict[str, Any] | None = None) -> Dict[str, Any]:
    args = _ if isinstance(_, dict) else {}
    use_local = bool(args.get("use_local_redirect", True))
    return start_login(use_local_redirect=use_local)


def complete_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    code = str(arguments.get("code_or_url") or arguments.get("code") or arguments.get("url") or "")
    probe = arguments.get("probe")
    if probe is None:
        probe = True
    return complete_login(code, probe=bool(probe))


def status_tool(_: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return login_status()
