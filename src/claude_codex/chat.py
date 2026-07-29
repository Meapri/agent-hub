"""Claude chat via Anthropic Messages API.

Protocol ideas adapted from NousResearch/hermes-agent anthropic transport:
system/messages split, tool input_schema shape, text block extraction.
Implementation is independent stdlib code for Codex MCP.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from agent_hub.core import media
from agent_hub.core import limits

from . import api, auth, models, response, security
from agent_hub.core import response as shared_response

DEFAULT_MODEL = models.DEFAULT_MODEL
DEFAULT_MAX_TOKENS = limits.CLAUDE_MAX_OUTPUT_TOKENS
DEFAULT_TIMEOUT_SECONDS = limits.MAX_PROVIDER_TIMEOUT_SECONDS
_MAX_64K_MODEL_MARKERS = ("haiku",)
REASONING_EFFORTS = {"low", "medium", "high"}
_EFFORT_MODEL_MARKERS = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-mythos-preview",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
)
_ADAPTIVE_THINKING_MODEL_MARKERS = (
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
)


def supports_temperature(model: str) -> bool:
    lowered = model.lower()
    return not any(
        marker in lowered
        for marker in (
            "claude-sonnet-5",
            "claude-fable-5",
            "claude-mythos-5",
            "claude-opus-5",
            "claude-opus-4-8",
        )
    )


def supports_reasoning_effort(model: str) -> bool:
    lowered = str(model or "").strip().lower()
    return any(marker in lowered for marker in _EFFORT_MODEL_MARKERS)


def uses_explicit_adaptive_thinking(model: str) -> bool:
    lowered = str(model or "").strip().lower()
    return any(marker in lowered for marker in _ADAPTIVE_THINKING_MODEL_MARKERS)


def max_output_tokens_for_model(model: str) -> int:
    """Return the conservative documented output cap for a Claude model."""
    lowered = str(model or "").strip().lower()
    if any(marker in lowered for marker in _MAX_64K_MODEL_MARKERS):
        return 65_536
    return DEFAULT_MAX_TOKENS


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


def _content_to_anthropic(content: Any) -> str | List[Dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts: List[Dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            parts.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "").lower()
        if kind in {"text", "input_text"}:
            text = str(block.get("text") or "")
            if text:
                parts.append({"type": "text", "text": text})
            continue
        if kind not in {"image", "image_url", "input_image"}:
            continue
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") in {"base64", "url", "file"}:
            parts.append({"type": "image", "source": source})
            continue
        raw_url = block.get("image_url") or block.get("url")
        if isinstance(raw_url, dict):
            raw_url = raw_url.get("url")
        url = str(raw_url or "")
        if url.startswith("data:image/"):
            header, _, data = url.partition(",")
            parts.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": header[5:].split(";", 1)[0],
                        "data": data,
                    },
                }
            )
        elif url.startswith(("https://", "http://")):
            parts.append({"type": "image", "source": {"type": "url", "url": url}})
    return parts


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
        content = _content_to_anthropic(msg.get("content"))
        if not content:
            continue
        # Merge consecutive same-role messages (Anthropic requirement), including
        # multimodal blocks appended by the unified Agent Hub adapter.
        if out and out[-1]["role"] == role:
            previous = out[-1]["content"]
            if isinstance(previous, str) and isinstance(content, str):
                out[-1]["content"] = previous + "\n\n" + content
            else:
                previous_parts = (
                    previous if isinstance(previous, list) else [{"type": "text", "text": previous}]
                )
                new_parts = (
                    content if isinstance(content, list) else [{"type": "text", "text": content}]
                )
                out[-1]["content"] = [*previous_parts, *new_parts]
        else:
            out.append({"role": role, "content": content})
    if not out:
        raise ValueError("at least one user/assistant message is required")
    if out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(continue)"})
    return "\n\n".join(system_parts), out


def convert_tools_to_anthropic(
    tools: Optional[List[Dict[str, Any]]],
) -> Optional[List[Dict[str, Any]]]:
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
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
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


def extract_citations(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    citations: List[Dict[str, Any]] = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        for citation in block.get("citations") or []:
            if isinstance(citation, dict):
                citations.append(dict(citation))
    return citations


def run_chat(arguments: Dict[str, Any]) -> Dict[str, Any]:
    security.require_consent()
    prompt = str(arguments.get("prompt") or "").strip()
    system = str(arguments.get("system") or "").strip()
    model = str(arguments.get("model") or os.getenv("CLAUDE_CODEX_MODEL") or DEFAULT_MODEL).strip()
    requested_max_tokens = int(arguments.get("max_tokens") or DEFAULT_MAX_TOKENS)
    model_max_tokens = max_output_tokens_for_model(model)
    max_tokens = min(max(1, requested_max_tokens), model_max_tokens)
    temperature = arguments.get("temperature")
    reasoning_effort = str(arguments.get("reasoning_effort") or "").strip().lower()
    if reasoning_effort and reasoning_effort not in REASONING_EFFORTS:
        raise ValueError("reasoning_effort must be low, medium, or high")
    if reasoning_effort and not supports_reasoning_effort(model):
        raise ValueError(f"reasoning_effort is not supported by Claude model: {model}")
    timeout = float(arguments.get("timeout_sec") or DEFAULT_TIMEOUT_SECONDS)
    messages = arguments.get("messages")
    if isinstance(messages, list) and messages:
        oai = [m for m in messages if isinstance(m, dict)]
    else:
        if not prompt:
            raise ValueError("prompt or messages is required")
        oai = [{"role": "user", "content": prompt}]
    images = media.normalize_images(
        arguments.get("images"), workspace_root=arguments.get("workspace_root")
    )
    if images:
        image_message = {"role": "user", "content": media.user_content(prompt, images)}
        if isinstance(messages, list) and messages:
            oai.append(image_message)
        else:
            oai = [image_message]
    system_text, anth_messages = to_anthropic_messages(oai, system=system)
    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": max(1, max_tokens),
        "messages": anth_messages,
    }
    if system_text:
        body["system"] = system_text
    temperature_ignored = temperature is not None and not supports_temperature(model)
    if temperature is not None and not temperature_ignored:
        body["temperature"] = float(temperature)
    if reasoning_effort:
        body["output_config"] = {"effort": reasoning_effort}
        if uses_explicit_adaptive_thinking(model):
            body["thinking"] = {"type": "adaptive"}
    tools = convert_tools_to_anthropic(
        arguments.get("tools") if isinstance(arguments.get("tools"), list) else None
    )
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
    stop_reason = str(payload.get("stop_reason") or "end_turn").lower()
    # tool_use is not truncation: the model stopped to ask for a tool this
    # runtime does not offer, so there is no answer to keep.
    outcome = shared_response.chat_outcome(
        text=text,
        finish_reason=stop_reason,
        unusable_finish_reasons={"tool_use"},
    )
    warnings = list(outcome["warnings"])
    if requested_max_tokens > model_max_tokens:
        warnings.append(f"max_tokens_clamped_for_model:{requested_max_tokens}->{model_max_tokens}")
    if temperature_ignored:
        warnings.append("temperature_ignored_by_model")
    return {
        "text": text,
        "stop_reason": stop_reason,
        "finish_reason": stop_reason,
        "raw_id": payload.get("id"),
        "auth_mode": auth_ctx.get("mode"),
        "citations": extract_citations(payload),
        # A truncated answer is an answer. The model produced text and ran out of
        # room, which the finish_reason and the warning both already say. Reporting
        # it as a failure discarded that text and, because no error_type came with
        # it, reached the caller as provider_unclassified_failure -- a dead end that
        # names neither the cause nor the fix. Vision hit this constantly: an image
        # costs ~1000 prompt tokens and its answers are long.
        **response.standard_fields(
            success=outcome["success"],
            provider="anthropic",
            backend=backend,
            model=str(payload.get("model") or model),
            usage=usage,
            warnings=warnings,
            diagnostics={
                "api_mode": "anthropic_messages",
                "auth_mode": auth_ctx.get("mode"),
                "auth_source": auth_ctx.get("source"),
                "subscription_fingerprint": auth_ctx.get("mode") == "subscription_oauth",
                "reasoning_effort": reasoning_effort or None,
            },
        ),
    }
