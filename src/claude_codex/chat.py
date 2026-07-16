"""Claude chat via Anthropic Messages API.

Protocol ideas adapted from NousResearch/hermes-agent anthropic transport:
system/messages split, tool input_schema shape, text block extraction.
Implementation is independent stdlib code for Codex MCP.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from . import api, auth, models, response, security

DEFAULT_MODEL = models.DEFAULT_MODEL


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content or "")


def to_anthropic_messages(
    messages: List[Dict[str, Any]],
    *,
    system: str = "",
) -> Tuple[str, List[Dict[str, Any]]]:
    """Convert OpenAI-style messages to Anthropic (system, messages).

    Hermes insight: system is a top-level field, not a message role.
    """
    system_parts: List[str] = []
    if system.strip():
        system_parts.append(system.strip())
    out: List[Dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role") or "").strip()
        text = _content_to_text(msg.get("content"))
        if role == "system":
            if text.strip():
                system_parts.append(text.strip())
            continue
        if role not in {"user", "assistant"}:
            # map tool/function-ish to user text for leaf simplicity
            role = "user"
        if not text:
            continue
        # merge consecutive same-role messages (Anthropic requirement)
        if out and out[-1]["role"] == role and isinstance(out[-1]["content"], str):
            out[-1]["content"] = str(out[-1]["content"]) + "\n\n" + text
        else:
            out.append({"role": role, "content": text})
    if not out:
        raise ValueError("at least one user/assistant message is required")
    if out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(continue)"})
    return "\n\n".join(system_parts), out


def convert_tools_to_anthropic(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """OpenAI tools → Anthropic input_schema tools (Hermes pattern, rewritten)."""
    if not tools:
        return None
    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            fn = tool["function"]
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            converted.append(
                {
                    "name": name,
                    "description": str(fn.get("description") or ""),
                    "input_schema": fn.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
        elif tool.get("name"):
            converted.append(
                {
                    "name": str(tool["name"]),
                    "description": str(tool.get("description") or ""),
                    "input_schema": tool.get("input_schema")
                    or tool.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
    return converted or None


def extract_text(payload: Dict[str, Any]) -> str:
    parts: List[str] = []
    for block in payload.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts).strip()


def extract_usage(payload: Dict[str, Any]) -> Dict[str, Any]:
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0),
    }


def run_chat(arguments: Dict[str, Any]) -> Dict[str, Any]:
    security.require_consent()
    prompt = str(arguments.get("prompt") or "").strip()
    system = str(arguments.get("system") or "").strip()
    model = str(arguments.get("model") or os.getenv("CLAUDE_CODEX_MODEL") or DEFAULT_MODEL).strip()
    max_tokens = int(arguments.get("max_tokens") or 4096)
    temperature = arguments.get("temperature")
    timeout = float(arguments.get("timeout_sec") or 120)
    messages = arguments.get("messages")
    if isinstance(messages, list) and messages:
        oai = [m for m in messages if isinstance(m, dict)]
    else:
        if not prompt:
            raise ValueError("prompt or messages is required")
        oai = [{"role": "user", "content": prompt}]
    system_text, anth_messages = to_anthropic_messages(oai, system=system)
    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": max(1, max_tokens),
        "messages": anth_messages,
    }
    if system_text:
        body["system"] = system_text
    if temperature is not None:
        body["temperature"] = float(temperature)
    tools = convert_tools_to_anthropic(arguments.get("tools") if isinstance(arguments.get("tools"), list) else None)
    if tools:
        body["tools"] = tools

    auth_ctx = auth.resolve_auth()
    payload = api.messages_create(body, timeout=timeout)
    text = extract_text(payload)
    usage = extract_usage(payload)
    backend = (
        "anthropic-messages-oauth"
        if auth_ctx.get("mode") == "subscription_oauth"
        else "anthropic-messages"
    )
    return {
        "text": text,
        "stop_reason": payload.get("stop_reason"),
        "raw_id": payload.get("id"),
        "auth_mode": auth_ctx.get("mode"),
        **response.standard_fields(
            provider="anthropic",
            backend=backend,
            model=str(payload.get("model") or model),
            usage=usage,
            diagnostics={
                "api_mode": "anthropic_messages",
                "auth_mode": auth_ctx.get("mode"),
                "auth_source": auth_ctx.get("source"),
                "subscription_fingerprint": auth_ctx.get("mode") == "subscription_oauth",
            },
        ),
    }
