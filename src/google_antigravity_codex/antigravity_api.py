"""Minimal direct Code Assist transport using an official ``agy`` token export."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Generator, List, Optional

from . import __version__, agy_auth

ENDPOINT = "https://cloudcode-pa.googleapis.com"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

# Aliases informed by Meapri/hermes-google-antigravity-plugin Cloud Code PA client.
MODEL_ALIASES = {
    "gemini-3.5-flash": "gemini-3.5-flash-high",
    "gemini-3.5-flash-medium": "gemini-3.5-flash-low",
    "gemini-3.5-flash-low": "gemini-3.5-flash-extra-low",
    "gemini-3.1-pro-preview": "gemini-3.1-pro-high",
    "gemini-3.1-pro": "gemini-3.1-pro-low",
    "gemini-3.1-pro-high": "gemini-pro-agent",
    "gemini-2.5-pro": "gemini-3.1-pro-high",
    "gemini-2.5-flash": "gemini-3.5-flash-high",
    "gemini-3-flash-high": "gemini-3-flash-agent",
    "gemini-3.5-flash-high": "gemini-3-flash-agent",
    "claude-opus-4.6": "claude-opus-4-6-thinking",
    "claude-opus-4-6": "claude-opus-4-6-thinking",
    "claude-4.6-opus": "claude-opus-4-6-thinking",
    "claude-4-6-opus": "claude-opus-4-6-thinking",
    "claude-opus-4.6-thinking": "claude-opus-4-6-thinking",
}

# When a tier returns MODEL_CAPACITY_EXHAUSTED (503), try cheaper alternatives.
CAPACITY_FALLBACK_CHAIN: Dict[str, List[str]] = {
    "gemini-3-flash-agent": [
        "gemini-3.5-flash-low",
        "gemini-3.5-flash-extra-low",
        "gemini-pro-agent",
    ],
    "gemini-3.5-flash-low": ["gemini-3.5-flash-extra-low", "gemini-pro-agent"],
    "gemini-3.5-flash-extra-low": ["gemini-pro-agent"],
}


class AntigravityApiError(RuntimeError):
    def __init__(self, message: str, *, code: str = "antigravity_api_error", status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(newurl, code, "redirects are blocked", headers, fp)


def _headers(access_token: str) -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "User-Agent": f"google-antigravity-codex/{__version__}",
        "X-Goog-Api-Client": f"google-antigravity-codex/{__version__}",
    }


def _post(path: str, body: Dict[str, Any], access_token: str, *, timeout: float) -> Dict[str, Any]:
    request = urllib.request.Request(
        ENDPOINT + path,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=_headers(access_token),
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        code = "antigravity_unauthorized" if exc.code in {401, 403} else f"antigravity_http_{exc.code}"
        raise AntigravityApiError(
            f"Antigravity Code Assist returned HTTP {exc.code}; response body omitted.",
            code=code,
            status_code=exc.code,
        ) from exc
    except urllib.error.URLError as exc:
        raise AntigravityApiError("Antigravity Code Assist request failed.", code="antigravity_network_error") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AntigravityApiError("Antigravity response exceeded the size limit.", code="antigravity_response_too_large")
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AntigravityApiError("Antigravity returned invalid JSON.", code="antigravity_response_invalid") from exc
    return payload if isinstance(payload, dict) else {}


def resolve_project_id(credentials: agy_auth.AgyCredentials) -> str:
    """Resolve Cloud Code companion project id (env → credential → loadCodeAssist)."""
    configured = os.getenv("GOOGLE_ANTIGRAVITY_PROJECT_ID", "").strip()
    if configured:
        return configured
    if credentials.project_id:
        return credentials.project_id
    payload = _post(
        "/v1internal:loadCodeAssist",
        {"metadata": {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}},
        credentials.access_token,
        timeout=30.0,
    )
    return str(payload.get("cloudaicompanionProject") or "").strip()


# Back-compat alias for internal callers.
_project_id = resolve_project_id


def _credentials_and_project() -> tuple[agy_auth.AgyCredentials, str]:
    credentials = agy_auth.valid_credentials(refresh=True)
    try:
        return credentials, resolve_project_id(credentials)
    except AntigravityApiError as exc:
        if exc.code != "antigravity_unauthorized":
            raise
    credentials = agy_auth.force_refresh_credentials()
    return credentials, resolve_project_id(credentials)


def _resolve_model(model: str) -> str:
    requested = str(model or "").strip() or "gemini-3.5-flash-high"
    return MODEL_ALIASES.get(requested, requested)


def _stream_post(
    path: str,
    body: Dict[str, Any],
    access_token: str,
    *,
    timeout: float,
) -> Generator[Dict[str, Any], None, None]:
    """POST and yield JSON objects from an SSE / NDJSON-style stream."""
    request = urllib.request.Request(
        ENDPOINT + path,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            **_headers(access_token),
            "Accept": "text/event-stream, application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        code = "antigravity_unauthorized" if exc.code in {401, 403} else f"antigravity_http_{exc.code}"
        raise AntigravityApiError(
            f"Antigravity Code Assist stream returned HTTP {exc.code}; response body omitted.",
            code=code,
            status_code=exc.code,
        ) from exc
    except urllib.error.URLError as exc:
        raise AntigravityApiError(
            "Antigravity Code Assist stream request failed.",
            code="antigravity_network_error",
        ) from exc

    total = 0
    try:
        while True:
            raw_line = response.readline()
            if not raw_line:
                break
            total += len(raw_line)
            if total > MAX_RESPONSE_BYTES:
                raise AntigravityApiError(
                    "Antigravity stream exceeded the size limit.",
                    code="antigravity_response_too_large",
                )
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line in {"[", "]", ","}:
                continue
            line = line.lstrip(",")
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload
            elif isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        yield item
    finally:
        try:
            response.close()
        except Exception:
            pass


def generate_content(
    *,
    model: str,
    request: Dict[str, Any],
    timeout: float = 180.0,
    max_retries: int = 1,
    retry_sleep_cap_seconds: float = 8.0,
) -> Dict[str, Any]:
    credentials, project = _credentials_and_project()
    if not project:
        raise AntigravityApiError("Could not resolve the Antigravity project id.", code="antigravity_project_missing")
    resolved = _resolve_model(model)
    fallback_models = [resolved] + CAPACITY_FALLBACK_CHAIN.get(resolved, [])
    attempts = max(0, min(int(max_retries), 5)) + 1
    request_count = 0
    auth_refreshed = False
    last_error: Optional[AntigravityApiError] = None

    for model_index, attempt_model in enumerate(fallback_models):
        body = {
            "project": project,
            "model": attempt_model,
            "request": request,
            "requestType": "agent",
            "userAgent": "antigravity",
            "requestId": "agent-" + str(uuid.uuid4()),
        }
        attempt = 0
        while attempt < attempts:
            try:
                request_count += 1
                payload = _post(
                    "/v1internal:generateContent",
                    body,
                    credentials.access_token,
                    timeout=timeout,
                )
                payload["_antigravity_diagnostics"] = {
                    "backend": "agy-oauth-code-assist",
                    "auth_source": "agy-token-export",
                    "attempt_count": request_count,
                    "auth_refreshed": auth_refreshed,
                    "requested_model": resolved,
                    "used_model": attempt_model,
                    "capacity_fallback": model_index > 0,
                }
                return payload
            except AntigravityApiError as exc:
                last_error = exc
                if exc.code == "antigravity_unauthorized" and not auth_refreshed:
                    credentials = agy_auth.force_refresh_credentials()
                    project = resolve_project_id(credentials)
                    if not project:
                        raise AntigravityApiError(
                            "Could not resolve the Antigravity project id after OAuth refresh.",
                            code="antigravity_project_missing",
                        )
                    body["project"] = project
                    auth_refreshed = True
                    continue
                # Capacity exhausted: skip retries on the same model and try the next tier.
                if exc.status_code == 503 and model_index + 1 < len(fallback_models):
                    break
                if exc.status_code not in {429, 500, 502, 503, 504} or attempt + 1 >= attempts:
                    raise
                time.sleep(min(max(0.0, float(retry_sleep_cap_seconds)), 0.5 * (2**attempt)))
                attempt += 1
    raise last_error or AntigravityApiError("Antigravity request failed.")


def generate_content_stream(
    *,
    model: str,
    request: Dict[str, Any],
    timeout: float = 180.0,
    max_retries: int = 1,
    retry_sleep_cap_seconds: float = 8.0,
) -> Generator[Dict[str, Any], None, None]:
    """Stream Code Assist chunks; yields dicts and a final diagnostics dict.

    On non-2xx or empty stream, raises so callers can fall back to generate_content.
    """
    del max_retries, retry_sleep_cap_seconds  # stream path uses a single attempt + capacity fallback
    credentials, project = _credentials_and_project()
    if not project:
        raise AntigravityApiError("Could not resolve the Antigravity project id.", code="antigravity_project_missing")
    resolved = _resolve_model(model)
    fallback_models = [resolved] + CAPACITY_FALLBACK_CHAIN.get(resolved, [])
    last_error: Optional[AntigravityApiError] = None
    auth_refreshed = False

    for model_index, attempt_model in enumerate(fallback_models):
        body = {
            "project": project,
            "model": attempt_model,
            "request": request,
            "requestType": "agent",
            "userAgent": "antigravity",
            "requestId": "agent-stream-" + str(uuid.uuid4()),
        }
        try:
            yielded = 0
            for chunk in _stream_post(
                "/v1internal:streamGenerateContent",
                body,
                credentials.access_token,
                timeout=timeout,
            ):
                yielded += 1
                yield chunk
            if yielded == 0:
                raise AntigravityApiError(
                    "Antigravity stream returned no chunks.",
                    code="antigravity_stream_empty",
                )
            yield {
                "_antigravity_diagnostics": {
                    "backend": "agy-oauth-code-assist",
                    "auth_source": "agy-token-export",
                    "streamed": True,
                    "requested_model": resolved,
                    "used_model": attempt_model,
                    "capacity_fallback": model_index > 0,
                    "auth_refreshed": auth_refreshed,
                    "chunk_count": yielded,
                }
            }
            return
        except AntigravityApiError as exc:
            last_error = exc
            if exc.code == "antigravity_unauthorized" and not auth_refreshed:
                credentials = agy_auth.force_refresh_credentials()
                project = resolve_project_id(credentials)
                auth_refreshed = True
                continue
            if exc.status_code == 503 and model_index + 1 < len(fallback_models):
                continue
            raise
    raise last_error or AntigravityApiError("Antigravity stream request failed.")


def list_models() -> List[Dict[str, Any]]:
    credentials, project = _credentials_and_project()
    body = {"project": project, "requestId": "agent-" + str(uuid.uuid4())}
    try:
        payload = _post(
            "/v1internal:fetchAvailableModels",
            body,
            credentials.access_token,
            timeout=60.0,
        )
    except AntigravityApiError as exc:
        if exc.code != "antigravity_unauthorized":
            raise
        credentials = agy_auth.force_refresh_credentials()
        body["project"] = resolve_project_id(credentials)
        payload = _post(
            "/v1internal:fetchAvailableModels",
            body,
            credentials.access_token,
            timeout=60.0,
        )
    raw = payload.get("models") or payload.get("modelConfigs") or payload.get("availableModels") or {}
    image_ids = {
        str(item).removeprefix("models/")
        for item in (payload.get("imageGenerationModelIds") or [])
        if str(item).strip()
    }
    items = raw.items() if isinstance(raw, dict) else enumerate(raw if isinstance(raw, list) else [])
    models: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for key, value in items:
        data = value if isinstance(value, dict) else {}
        # Prefer the catalog key (public id, e.g. gemini-3.1-flash-image) over
        # internal enums like MODEL_PLACEHOLDER_M21.
        public_id = str(key if not isinstance(key, int) else "").removeprefix("models/").strip()
        internal_id = str(data.get("model") or data.get("id") or data.get("name") or "").removeprefix("models/").strip()
        model_id = public_id or internal_id
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        methods = ["generateContent"]
        display = str(data.get("displayName") or model_id)
        is_image = (
            model_id in image_ids
            or internal_id in image_ids
            or "image" in model_id.lower()
            or "image" in display.lower()
        )
        if is_image:
            methods.append("generateImages")
        entry: Dict[str, Any] = {
            "id": model_id,
            "display": display,
            "methods": methods,
        }
        if internal_id and internal_id != model_id:
            entry["internal_id"] = internal_id
        models.append(entry)
    # Ensure imageGenerationModelIds appear even if missing from models map.
    for image_id in sorted(image_ids):
        if image_id in seen:
            continue
        models.append(
            {
                "id": image_id,
                "display": image_id,
                "methods": ["generateContent", "generateImages"],
            }
        )
    return models


def status(*, probe: bool = False) -> Dict[str, Any]:
    auth_state = agy_auth.status(probe=False)
    state: Dict[str, Any] = {
        "configured": auth_state.get("enabled") is True and auth_state.get("token_file_present") is True,
        "provider": "agy-oauth",
        "backend": "agy-oauth-code-assist",
        "auth_method": "agy_token_export",
        "auth": auth_state,
        "healthy": None,
    }
    if not state["configured"] or not probe:
        return state
    try:
        visible = list_models()
    except (agy_auth.AgyAuthError, AntigravityApiError) as exc:
        state.update({"healthy": False, "error_type": getattr(exc, "code", type(exc).__name__), "error": str(exc)})
    else:
        state.update({"healthy": True, "model_count": len(visible)})
    return state
