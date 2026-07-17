"""HTTP helpers for xAI (subscription OAuth or API key)."""

from __future__ import annotations

from agent_hub.core.http import http_json  # noqa: F401

import os
from typing import Any, Dict, List
from uuid import uuid4

from . import auth

DEFAULT_BASE = "https://api.x.ai/v1"


def base_url() -> str:
    return os.getenv("XAI_BASE_URL", DEFAULT_BASE).rstrip("/")




def auth_headers(*, session_id: str = "") -> Dict[str, str]:
    ctx = auth.resolve_auth()
    if ctx.get("mode") == "subscription_oauth":
        token = ctx["access_token"]
    else:
        token = ctx.get("api_key") or ""
    if not token:
        raise RuntimeError("xAI credentials missing")
    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
    }
    sid = session_id or os.getenv("XAI_SESSION_ID", "").strip() or str(uuid4())
    headers["x-grok-conv-id"] = sid
    return headers


def list_models_live(timeout: float = 30.0) -> List[Dict[str, Any]]:
    url = f"{base_url()}/models"
    payload = http_json("GET", url, auth_headers(), None, timeout)
    items = payload.get("data") or []
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if mid:
            out.append({"id": mid, "display": mid, "source": "live"})
    return out


def chat_completions(body: Dict[str, Any], *, timeout: float = 120.0, session_id: str = "") -> Dict[str, Any]:
    url = f"{base_url()}/chat/completions"
    body = dict(body)
    body.pop("reasoningEffort", None)
    body.pop("reasoning_effort", None)
    return http_json("POST", url, auth_headers(session_id=session_id), body, timeout)


def responses_create(body: Dict[str, Any], *, timeout: float = 120.0, session_id: str = "") -> Dict[str, Any]:
    url = f"{base_url()}/responses"
    body = dict(body)
    body.pop("reasoningEffort", None)
    body.pop("reasoning_effort", None)
    return http_json("POST", url, auth_headers(session_id=session_id), body, timeout)


def images_generate(body: Dict[str, Any], *, timeout: float = 180.0) -> Dict[str, Any]:
    return http_json("POST", f"{base_url()}/images/generations", auth_headers(), body, timeout)
