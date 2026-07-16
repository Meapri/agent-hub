"""HTTP helpers for Anthropic Messages API (API key or subscription OAuth)."""

from __future__ import annotations

from agent_hub.core.http import http_json  # noqa: F401

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
