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

from agent_hub.core import media
from agent_hub.core import limits

from . import api, auth, models, response, security

DEFAULT_MODEL = models.DEFAULT_MODEL
DEFAULT_MAX_TOKENS = limits.MAX_OUTPUT_TOKENS
DEFAULT_TIMEOUT_SECONDS = limits.MAX_PROVIDER_TIMEOUT_SECONDS
REASONING_EFFORTS = {"low", "medium", "high"}


def supports_reasoning_effort(model: str) -> bool:
    return "grok-4.5" in str(model or "").strip().lower()


def _normalize_messages(arguments: Dict[str, Any]) -> List[Dict[str, Any]]:
    prompt = str(arguments.get("prompt") or "").strip()
    system = str(arguments.get("system") or "").strip()
    messages = arguments.get("messages")
    out: List[Dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    supplied_messages = isinstance(messages, list) and bool(messages)
    if supplied_messages:
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "user")
            content = msg.get("content")
            if content:
                out.append({"role": role, "content": content})
    elif prompt:
        out.append({"role": "user", "content": prompt})
    images = media.normalize_images(
        arguments.get("images"), workspace_root=arguments.get("workspace_root")
    )
    if images:
        if not supplied_messages:
            out = [message for message in out if message.get("role") == "system"]
        out.append({"role": "user", "content": media.user_content(prompt, images)})
    if not out or all(m["role"] == "system" for m in out):
        raise ValueError("prompt or messages is required")
    return out


def _has_images(messages: List[Dict[str, Any]]) -> bool:
    return any(
        isinstance(block, dict)
        and str(block.get("type") or "").lower() in {"image", "image_url", "input_image"}
        for message in messages
        for block in (message.get("content") if isinstance(message.get("content"), list) else [])
    )


def _responses_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            out.append(dict(message))
            continue
        blocks: List[Dict[str, Any]] = []
        for block in content:
            if isinstance(block, str):
                blocks.append({"type": "input_text", "text": block})
            elif isinstance(block, dict):
                kind = str(block.get("type") or "").lower()
                if kind in {"text", "input_text"}:
                    blocks.append({"type": "input_text", "text": str(block.get("text") or "")})
                elif kind in {"image", "image_url", "input_image"}:
                    raw = block.get("image_url") or block.get("url")
                    if isinstance(raw, dict):
                        raw = raw.get("url")
                    blocks.append(
                        {
                            "type": "input_image",
                            "image_url": str(raw or ""),
                            "detail": str(block.get("detail") or "auto"),
                        }
                    )
        out.append({"role": message.get("role") or "user", "content": blocks})
    return out


def _chat_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            out.append(dict(message))
            continue
        blocks: List[Dict[str, Any]] = []
        for block in content:
            if isinstance(block, str):
                blocks.append({"type": "text", "text": block})
            elif isinstance(block, dict):
                kind = str(block.get("type") or "").lower()
                if kind in {"text", "input_text"}:
                    blocks.append({"type": "text", "text": str(block.get("text") or "")})
                elif kind in {"image", "image_url", "input_image"}:
                    raw = block.get("image_url") or block.get("url")
                    if isinstance(raw, dict):
                        raw = raw.get("url")
                    blocks.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": str(raw or ""),
                                "detail": str(block.get("detail") or "auto"),
                            },
                        }
                    )
        out.append({"role": message.get("role") or "user", "content": blocks})
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
    # Tool-backed Responses may contain user-visible progress messages before
    # the final assistant message. Prefer the last structured assistant message
    # instead of concatenating those intermediate messages through output_text.
    message_texts: List[str] = []
    fallback_parts: List[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        item_parts: List[str] = []
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                item_parts.append(str(block.get("text") or ""))
        if not item_parts:
            continue
        fallback_parts.extend(item_parts)
        if item.get("type") == "message" and item.get("role") in {None, "assistant"}:
            message_texts.append("".join(item_parts))
    if message_texts:
        return message_texts[-1].strip()
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"].strip()
    if fallback_parts:
        return "".join(fallback_parts).strip()
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
    max_tokens = int(arguments.get("max_tokens") or DEFAULT_MAX_TOKENS)
    temperature = arguments.get("temperature")
    reasoning_effort = str(arguments.get("reasoning_effort") or "").strip().lower()
    if reasoning_effort and reasoning_effort not in REASONING_EFFORTS:
        raise ValueError("reasoning_effort must be low, medium, or high")
    if reasoning_effort and not supports_reasoning_effort(model):
        raise ValueError(f"reasoning_effort is not supported by Grok model: {model}")
    timeout = float(arguments.get("timeout_sec") or DEFAULT_TIMEOUT_SECONDS)
    session_id = str(arguments.get("session_id") or "").strip() or str(uuid4())
    api_mode = str(arguments.get("api_mode") or os.getenv("GROK_CODEX_API_MODE") or "chat").strip().lower()
    messages = _normalize_messages(arguments)
    if _has_images(messages):
        api_mode = "responses"
    if reasoning_effort:
        api_mode = "responses"

    if api_mode in {"responses", "response"}:
        # Minimal Responses body (Hermes uses richer conversion; keep leaf simple).
        # Concatenate as input string if needed.
        body: Dict[str, Any] = {
            "model": model,
            "input": _responses_messages(messages),
            "max_output_tokens": max(1, max_tokens),
        }
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        payload = api.responses_create(body, timeout=timeout, session_id=session_id)
        text = _extract_responses_text(payload)
        auth_ctx = auth.resolve_auth()
        backend = "xai-responses"
    else:
        body = {
            "model": model,
            "messages": _chat_messages(messages),
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
    if api_mode in {"responses", "response"}:
        finish_reason = str(payload.get("status") or "completed").lower()
        incomplete = finish_reason == "incomplete"
    else:
        choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        finish_reason = str(first.get("finish_reason") or "stop").lower()
        incomplete = finish_reason == "length"
    warnings = [f"incomplete_finish_reason:{finish_reason}"] if incomplete else []
    return {
        "text": text,
        "finish_reason": finish_reason,
        "session_id": session_id,
        "auth_mode": auth_ctx.get("mode"),
        "citations": payload.get("citations") if isinstance(payload.get("citations"), list) else [],
        **response.standard_fields(
            success=not incomplete,
            provider="xai",
            backend=backend + ("-oauth" if auth_ctx.get("mode") == "subscription_oauth" else ""),
            model=str(payload.get("model") or model),
            usage=_usage(payload),
            warnings=warnings,
            diagnostics={
                "api_mode": api_mode,
                "session_id": session_id,
                "auth_mode": auth_ctx.get("mode"),
                "reasoning_effort": reasoning_effort or None,
            },
        ),
    }
