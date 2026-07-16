"""Grok chat via xAI OpenAI-compatible Chat Completions (default) or Responses API.

Ideas adapted from NousResearch/hermes-agent xAI integration:
- base URL https://api.x.ai/v1
- x-grok-conv-id session affinity header
- avoid unsupported reasoningEffort on models that reject it
- optional /v1/responses for reasoning-oriented calls
"""

from __future__ import annotations

import os
from typing import Any, Dict, List
from uuid import uuid4

from . import api, auth, models, response, security

DEFAULT_MODEL = models.DEFAULT_MODEL


def _normalize_messages(arguments: Dict[str, Any]) -> List[Dict[str, str]]:
    prompt = str(arguments.get("prompt") or "").strip()
    system = str(arguments.get("system") or "").strip()
    messages = arguments.get("messages")
    out: List[Dict[str, str]] = []
    if system:
        out.append({"role": "system", "content": system})
    if isinstance(messages, list) and messages:
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "user")
            content = msg.get("content")
            if isinstance(content, list):
                # flatten text parts
                text = "".join(
                    str(b.get("text") or "") if isinstance(b, dict) else str(b) for b in content
                )
            else:
                text = str(content or "")
            if text:
                out.append({"role": role, "content": text})
    elif prompt:
        out.append({"role": "user", "content": prompt})
    if not out or all(m["role"] == "system" for m in out):
        raise ValueError("prompt or messages is required")
    return out


def _extract_chat_text(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            str(p.get("text") or "") if isinstance(p, dict) else str(p) for p in content
        ).strip()
    return str(content or "").strip()


def _extract_responses_text(payload: Dict[str, Any]) -> str:
    # Responses API variants: output_text or output[].content[].text
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"].strip()
    parts: List[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                parts.append(str(block.get("text") or ""))
    if parts:
        return "".join(parts).strip()
    return _extract_chat_text(payload)


def _usage(payload: Dict[str, Any]) -> Dict[str, Any]:
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens")
    completion = usage.get("completion_tokens") or usage.get("output_tokens")
    total = usage.get("total_tokens")
    if total is None and prompt is not None and completion is not None:
        total = int(prompt) + int(completion)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def run_chat(arguments: Dict[str, Any]) -> Dict[str, Any]:
    security.require_consent()
    model = str(arguments.get("model") or os.getenv("GROK_CODEX_MODEL") or DEFAULT_MODEL).strip()
    max_tokens = int(arguments.get("max_tokens") or 4096)
    temperature = arguments.get("temperature")
    timeout = float(arguments.get("timeout_sec") or 120)
    session_id = str(arguments.get("session_id") or "").strip() or str(uuid4())
    api_mode = str(arguments.get("api_mode") or os.getenv("GROK_CODEX_API_MODE") or "chat").strip().lower()
    messages = _normalize_messages(arguments)

    if api_mode in {"responses", "response"}:
        # Minimal Responses body (Hermes uses richer conversion; keep leaf simple).
        # Concatenate as input string if needed.
        input_text = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        body: Dict[str, Any] = {
            "model": model,
            "input": input_text,
            "max_output_tokens": max(1, max_tokens),
        }
        payload = api.responses_create(body, timeout=timeout, session_id=session_id)
        text = _extract_responses_text(payload)
        auth_ctx = auth.resolve_auth()
        backend = "xai-responses"
    else:
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max(1, max_tokens),
        }
        if temperature is not None:
            body["temperature"] = float(temperature)
        payload = api.chat_completions(body, timeout=timeout, session_id=session_id)
        text = _extract_chat_text(payload)
        backend = "xai-chat-completions"

    try:
        auth_ctx = auth.resolve_auth()
    except Exception:
        auth_ctx = {}
    return {
        "text": text,
        "session_id": session_id,
        "auth_mode": auth_ctx.get("mode"),
        **response.standard_fields(
            provider="xai",
            backend=backend + ("-oauth" if auth_ctx.get("mode") == "subscription_oauth" else ""),
            model=str(payload.get("model") or model),
            usage=_usage(payload),
            diagnostics={
                "api_mode": api_mode,
                "session_id": session_id,
                "auth_mode": auth_ctx.get("mode"),
            },
        ),
    }
