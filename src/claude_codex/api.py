"""HTTP helpers for Anthropic Messages API (API key or subscription OAuth)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from . import auth, subscription_auth, subscription_fingerprint

DEFAULT_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


def base_url() -> str:
    return os.getenv("ANTHROPIC_BASE_URL", DEFAULT_BASE).rstrip("/")


def http_json(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[Dict[str, Any]],
    timeout: float,
) -> Dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"HTTP {exc.code}: {err_body}") from exc


def build_headers(*, auth_ctx: Optional[Dict[str, Any]] = None, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    auth_ctx = auth_ctx or auth.resolve_auth()
    headers: Dict[str, str] = {
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    if auth_ctx.get("mode") == "subscription_oauth":
        headers["authorization"] = f"Bearer {auth_ctx['access_token']}"
        headers["anthropic-beta"] = ",".join(subscription_auth.OAUTH_BETAS)
    else:
        headers["x-api-key"] = str(auth_ctx.get("api_key") or "")
    if extra:
        headers.update(extra)
    return headers


def list_models_live(timeout: float = 30.0) -> List[Dict[str, Any]]:
    url = f"{base_url()}/v1/models"
    payload = http_json("GET", url, build_headers(), None, timeout)
    items = payload.get("data") or []
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if mid:
            out.append({"id": mid, "display": mid, "source": "live"})
    return out


def messages_create(body: Dict[str, Any], *, timeout: float = 120.0) -> Dict[str, Any]:
    auth_ctx = auth.resolve_auth()
    request_body = dict(body)
    extra_headers: Dict[str, str] = {}
    if auth_ctx.get("mode") == "subscription_oauth":
        request_body, extra_headers = subscription_fingerprint.apply_subscription_fingerprint(request_body)
    url = f"{base_url()}/v1/messages"
    if auth_ctx.get("mode") == "subscription_oauth":
        # Claude Code sends ?beta=true on OAuth traffic.
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode({'beta': 'true'})}"
    headers = build_headers(auth_ctx=auth_ctx, extra=extra_headers)
    return http_json("POST", url, headers, request_body, timeout)
