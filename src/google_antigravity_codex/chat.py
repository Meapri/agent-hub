"""Chat request and response translation for Antigravity Code Assist."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

from . import model_prefs, provider, response as response_schema

DEFAULT_MODEL = "gemini-3.5-flash"
MIN_REASONING_MODEL_OUTPUT_TOKENS = 256
MIN_OUTPUT_TOKEN_MODEL_MARKERS = (
    "gemini-3.5-flash",
    "gemini-3.1-pro",
    "gemini-3-flash",
    "gemini-pro-agent",
    "gpt-oss",
)
GROUNDING_HINT = (
    "Google Search grounding is enabled for this request. Use grounded search results "
    "for current facts, separate verified facts from inference, and include source URLs "
    "when they materially help verification."
)
# Only accept inline data:image/*;base64,... payloads (no remote image fetch).
_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)
ProgressCallback = Optional[Callable[[str, Dict[str, Any]], None]]


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: List[str] = []
        for item in content:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    pieces.append(item["text"])
                elif isinstance(item.get("text"), str) and not item.get("type"):
                    pieces.append(item["text"])
        return "\n".join(piece for piece in pieces if piece)
    return str(content)


def _inline_from_data_url(url: str) -> Optional[Dict[str, Any]]:
    match = _DATA_URL_RE.match((url or "").strip())
    if not match:
        return None
    mime = match.group("mime").lower()
    raw = re.sub(r"\s+", "", match.group("data"))
    try:
        # Validate base64 without keeping the decoded bytes in the request twice.
        base64.b64decode(raw, validate=False)
    except Exception:
        return None
    return {"inlineData": {"mimeType": mime, "data": raw}}


def _parts_from_content(content: Any) -> List[Dict[str, Any]]:
    """Translate OpenAI-style or Gemini-style content into Gemini parts."""
    if content is None:
        return []
    if isinstance(content, str):
        text = content.strip()
        return [{"text": text}] if text else []
    if not isinstance(content, list):
        text = str(content).strip()
        return [{"text": text}] if text else []

    parts: List[Dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            if item.strip():
                parts.append({"text": item})
            continue
        if not isinstance(item, dict):
            continue
        # Already Gemini-shaped
        if isinstance(item.get("text"), str) and item.get("type") in {None, "text"}:
            if item.get("thought") is True:
                parts.append({"text": item["text"], "thought": True})
            elif item["text"]:
                parts.append({"text": item["text"]})
            continue
        if isinstance(item.get("inlineData"), dict) or isinstance(item.get("inline_data"), dict):
            data = item.get("inlineData") or item.get("inline_data")
            mime = str(data.get("mimeType") or data.get("mime_type") or "image/png")
            blob = str(data.get("data") or "")
            if blob:
                parts.append({"inlineData": {"mimeType": mime, "data": blob}})
            continue
        if isinstance(item.get("functionCall"), dict) or isinstance(item.get("function_call"), dict):
            fn = item.get("functionCall") or item.get("function_call") or {}
            parts.append(
                {
                    "functionCall": {
                        "name": str(fn.get("name") or ""),
                        "args": fn.get("args") if isinstance(fn.get("args"), dict) else {},
                    }
                }
            )
            continue
        if isinstance(item.get("functionResponse"), dict) or isinstance(item.get("function_response"), dict):
            fr = item.get("functionResponse") or item.get("function_response") or {}
            parts.append(
                {
                    "functionResponse": {
                        "name": str(fr.get("name") or ""),
                        "response": fr.get("response") if isinstance(fr.get("response"), dict) else {"result": fr.get("response")},
                    }
                }
            )
            continue
        # OpenAI multimodal shapes
        kind = str(item.get("type") or "").lower()
        if kind == "text" and isinstance(item.get("text"), str) and item["text"]:
            parts.append({"text": item["text"]})
        elif kind in {"image_url", "input_image", "image"}:
            image_url = item.get("image_url")
            url = ""
            if isinstance(image_url, dict):
                url = str(image_url.get("url") or "")
            elif isinstance(image_url, str):
                url = image_url
            elif isinstance(item.get("url"), str):
                url = item["url"]
            inline = _inline_from_data_url(url)
            if inline:
                parts.append(inline)
            elif url:
                # Remote URLs are not fetched (trust boundary); surface a text note.
                parts.append({"text": f"[image omitted: remote URL not fetched] {url[:120]}"})
        elif kind == "function_call":
            parts.append(
                {
                    "functionCall": {
                        "name": str(item.get("name") or ""),
                        "args": item.get("arguments")
                        if isinstance(item.get("arguments"), dict)
                        else _maybe_json_args(item.get("arguments")),
                    }
                }
            )
        elif kind == "function_response":
            parts.append(
                {
                    "functionResponse": {
                        "name": str(item.get("name") or ""),
                        "response": item.get("response")
                        if isinstance(item.get("response"), dict)
                        else {"result": item.get("content") or item.get("response")},
                    }
                }
            )
    return parts


def _maybe_json_args(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {"raw": value}
    return {}


def _tool_calls_to_parts(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = []
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return parts
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else call
        name = str(fn.get("name") or call.get("name") or "")
        args = fn.get("arguments") if "arguments" in fn else fn.get("args")
        if isinstance(args, str):
            args = _maybe_json_args(args)
        if not isinstance(args, dict):
            args = {}
        if name:
            parts.append({"functionCall": {"name": name, "args": args}})
    return parts


def _build_contents(messages: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    contents: List[Dict[str, Any]] = []
    system_parts: List[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").lower()
        if role == "system":
            text = _content_to_text(message.get("content")).strip()
            if text:
                system_parts.append(text)
            continue

        parts = _parts_from_content(message.get("content"))
        # OpenAI assistant tool_calls
        if role in {"assistant", "model"}:
            parts.extend(_tool_calls_to_parts(message))
            gemini_role = "model"
        elif role in {"tool", "function"}:
            # Tool results → user functionResponse parts
            name = str(message.get("name") or message.get("tool_name") or "tool")
            response_payload: Any
            if isinstance(message.get("content"), (dict, list)):
                response_payload = message.get("content")
            else:
                raw = _content_to_text(message.get("content"))
                try:
                    response_payload = json.loads(raw) if raw.strip().startswith(("{", "[")) else {"result": raw}
                except json.JSONDecodeError:
                    response_payload = {"result": raw}
            if not isinstance(response_payload, dict):
                response_payload = {"result": response_payload}
            parts = [{"functionResponse": {"name": name, "response": response_payload}}]
            gemini_role = "user"
        else:
            gemini_role = "user"

        if parts:
            contents.append({"role": gemini_role, "parts": parts})

    system_instruction = None
    system_text = "\n".join(system_parts).strip()
    if system_text:
        system_instruction = {"parts": [{"text": system_text}]}
    return contents, system_instruction


def _thinking_level_from_model(model: str) -> str:
    normalized = str(model or "").strip().removeprefix("models/").lower()
    if normalized.endswith("-high"):
        return "high"
    if normalized.endswith("-low"):
        return "low"
    return ""


def _effective_max_tokens(model: str, max_tokens: Optional[int]) -> Optional[int]:
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        return None
    normalized = str(model or "").strip().removeprefix("models/").lower()
    if any(marker in normalized for marker in MIN_OUTPUT_TOKEN_MODEL_MARKERS):
        return max(int(max_tokens), MIN_REASONING_MODEL_OUTPUT_TOKENS)
    return int(max_tokens)


def build_request(
    *,
    messages: List[Dict[str, Any]],
    model: str = DEFAULT_MODEL,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    top_p: Optional[float] = None,
    thinking_level: str = "",
    grounding: str = "off",
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    contents, system_instruction = _build_contents(messages)
    body: Dict[str, Any] = {"contents": contents}
    if system_instruction:
        body["systemInstruction"] = system_instruction
    generation: Dict[str, Any] = {}
    if isinstance(temperature, (int, float)):
        generation["temperature"] = float(temperature)
    effective_max_tokens = _effective_max_tokens(model, max_tokens)
    if effective_max_tokens is not None:
        generation["maxOutputTokens"] = effective_max_tokens
    if isinstance(top_p, (int, float)):
        generation["topP"] = float(top_p)
    requested_thinking = str(thinking_level or "").strip().lower()
    if requested_thinking not in {"minimal", "low", "medium", "high"}:
        requested_thinking = _thinking_level_from_model(model)
    if requested_thinking:
        generation["thinkingConfig"] = {"thinkingLevel": requested_thinking}
    if generation:
        body["generationConfig"] = generation

    tool_list: List[Dict[str, Any]] = []
    if grounding in {"always", "auto"}:
        tool_list.append({"google_search": {}})
        existing = body.get("systemInstruction")
        if isinstance(existing, dict) and isinstance(existing.get("parts"), list) and existing["parts"]:
            first = existing["parts"][0]
            if isinstance(first, dict):
                first["text"] = f"{first.get('text', '')}\n\n{GROUNDING_HINT}".strip()
        else:
            body["systemInstruction"] = {"parts": [{"text": GROUNDING_HINT}]}
    if isinstance(tools, list):
        # Accept Gemini-shaped tools or OpenAI function tools.
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if "functionDeclarations" in tool or "google_search" in tool:
                tool_list.append(tool)
            elif tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                fn = tool["function"]
                decl = {
                    "name": str(fn.get("name") or ""),
                    "description": str(fn.get("description") or ""),
                }
                if isinstance(fn.get("parameters"), dict):
                    decl["parameters"] = fn["parameters"]
                # Merge into a single functionDeclarations block when possible.
                existing_fd = next((t for t in tool_list if "functionDeclarations" in t), None)
                if existing_fd is None:
                    tool_list.append({"functionDeclarations": [decl]})
                else:
                    existing_fd["functionDeclarations"].append(decl)
            elif "name" in tool and "parameters" in tool:
                existing_fd = next((t for t in tool_list if "functionDeclarations" in t), None)
                if existing_fd is None:
                    tool_list.append({"functionDeclarations": [tool]})
                else:
                    existing_fd["functionDeclarations"].append(tool)
    if tool_list:
        body["tools"] = tool_list
    return body


def extract_response_text(payload: Dict[str, Any]) -> Dict[str, Any]:
    inner = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    candidates = inner.get("candidates") if isinstance(inner, dict) else []
    if not isinstance(candidates, list) or not candidates:
        return {"text": "", "reasoning": "", "finish_reason": "stop", "tool_calls": [], "raw": inner}
    candidate = candidates[0] if isinstance(candidates[0], dict) else {}
    content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("thought") is True and isinstance(part.get("text"), str):
            reasoning_parts.append(part["text"])
        elif isinstance(part.get("text"), str):
            text_parts.append(part["text"])
        elif isinstance(part.get("functionCall"), dict):
            fn = part["functionCall"]
            tool_calls.append(
                {
                    "id": f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": json.dumps(fn.get("args") or {}, ensure_ascii=False),
                    },
                    "name": fn.get("name", ""),
                    "args": fn.get("args") or {},
                }
            )
    usage = inner.get("usageMetadata") if isinstance(inner.get("usageMetadata"), dict) else {}
    return {
        "text": "".join(text_parts),
        "reasoning": "".join(reasoning_parts),
        "tool_calls": tool_calls,
        "finish_reason": str(candidate.get("finishReason") or "STOP").lower(),
        "usage": {
            "prompt_tokens": int(usage.get("promptTokenCount") or 0),
            "completion_tokens": int(usage.get("candidatesTokenCount") or 0),
            "total_tokens": int(usage.get("totalTokenCount") or 0),
        },
        "raw": inner,
    }


def merge_stream_chunks(chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Assemble a non-streaming-shaped payload from streamGenerateContent chunks."""
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    finish_reason = "STOP"
    usage: Dict[str, Any] = {}
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        extracted = extract_response_text(chunk)
        if extracted.get("text"):
            text_parts.append(str(extracted["text"]))
        if extracted.get("reasoning"):
            reasoning_parts.append(str(extracted["reasoning"]))
        for call in extracted.get("tool_calls") or []:
            tool_calls.append(call)
        if extracted.get("finish_reason") and extracted["finish_reason"] not in {"", "stop"}:
            finish_reason = str(extracted["finish_reason"]).upper()
        if extracted.get("usage") and any(extracted["usage"].values()):
            usage = extracted["usage"]
    return {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": (
                            ([{"text": "".join(reasoning_parts), "thought": True}] if reasoning_parts else [])
                            + ([{"text": "".join(text_parts)}] if text_parts else [])
                            + [
                                {
                                    "functionCall": {
                                        "name": c.get("name") or c.get("function", {}).get("name"),
                                        "args": c.get("args")
                                        or _maybe_json_args((c.get("function") or {}).get("arguments")),
                                    }
                                }
                                for c in tool_calls
                            ]
                        ),
                    },
                    "finishReason": finish_reason,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": int((usage or {}).get("prompt_tokens") or 0),
                "candidatesTokenCount": int((usage or {}).get("completion_tokens") or 0),
                "totalTokenCount": int((usage or {}).get("total_tokens") or 0),
            },
        }
    }


def run_chat(arguments: Dict[str, Any], *, progress: ProgressCallback = None) -> Dict[str, Any]:
    try:
        from . import profiles

        arguments = profiles.apply_profile_to_chat_args(arguments)
    except Exception:
        arguments = dict(arguments or {})
    task_hint = str(arguments.get("task") or "chat").strip() or "chat"
    model = model_prefs.resolve_model(
        explicit=str(arguments.get("model") or ""),
        task=task_hint if task_hint in model_prefs.TASK_KEYS else "chat",
        fallback=DEFAULT_MODEL,
    ) or DEFAULT_MODEL
    messages = arguments.get("messages")
    if not isinstance(messages, list):
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt or messages is required")
        messages = []
        system = str(arguments.get("system") or "").strip()
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
    grounding = str(arguments.get("grounding") or os.getenv("GOOGLE_ANTIGRAVITY_GROUNDING") or "off").strip().lower()
    if grounding not in {"off", "auto", "always"}:
        grounding = "off"
    tools = arguments.get("tools") if isinstance(arguments.get("tools"), list) else None
    request = build_request(
        messages=messages,
        model=model,
        temperature=arguments.get("temperature"),
        max_tokens=arguments.get("max_tokens"),
        top_p=arguments.get("top_p"),
        thinking_level=str(arguments.get("thinking_level") or ""),
        grounding=grounding,
        tools=tools,
    )
    timeout = float(arguments.get("timeout_sec") or 180.0)
    retries = int(arguments.get("retry_count") if arguments.get("retry_count") is not None else 1)
    retry_cap = float(arguments.get("retry_sleep_cap_sec") or 8.0)
    want_stream = bool(arguments.get("stream"))

    diagnostics: Dict[str, Any] = {}
    if want_stream and hasattr(provider, "generate_content_stream"):
        chunks: List[Dict[str, Any]] = []
        streamed_text = ""
        try:
            for event in provider.generate_content_stream(
                model=model,
                request=request,
                timeout=timeout,
                max_retries=retries,
                retry_sleep_cap_seconds=retry_cap,
            ):
                if not isinstance(event, dict):
                    continue
                if event.get("_antigravity_diagnostics"):
                    diagnostics = event["_antigravity_diagnostics"]
                    continue
                chunks.append(event)
                piece = extract_response_text(event).get("text") or ""
                if piece and progress:
                    streamed_text += piece
                    progress(
                        "notifications/message",
                        {
                            "level": "info",
                            "logger": "google-antigravity-codex",
                            "data": {
                                "type": "stream_delta",
                                "delta": piece,
                                "accumulated_chars": len(streamed_text),
                            },
                        },
                    )
            payload = merge_stream_chunks(chunks)
            if diagnostics:
                payload["_antigravity_diagnostics"] = {**diagnostics, "streamed": True}
            else:
                payload["_antigravity_diagnostics"] = {"streamed": True, "backend": "agy-session"}
        except Exception:
            # Fall back to non-streaming generate on stream failure.
            payload = provider.generate_content(
                model=model,
                request=request,
                timeout=timeout,
                max_retries=retries,
                retry_sleep_cap_seconds=retry_cap,
            )
            diag = payload.get("_antigravity_diagnostics")
            if isinstance(diag, dict):
                diag = dict(diag)
                diag["stream_fallback"] = True
                payload["_antigravity_diagnostics"] = diag
    else:
        payload = provider.generate_content(
            model=model,
            request=request,
            timeout=timeout,
            max_retries=retries,
            retry_sleep_cap_seconds=retry_cap,
        )

    diagnostics = (
        payload.get("_antigravity_diagnostics")
        if isinstance(payload.get("_antigravity_diagnostics"), dict)
        else {}
    )
    backend = str(diagnostics.get("backend") or "agy-session")
    extracted = extract_response_text(payload)
    warnings: List[str] = []
    if not (extracted["text"] or extracted.get("tool_calls")):
        warnings.append("empty_model_text")
    if diagnostics.get("capacity_fallback"):
        used = diagnostics.get("used_model") or model
        requested = diagnostics.get("requested_model") or model
        warnings.append(f"capacity_fallback:{requested}->{used}")
    text_out = extracted["text"]
    if diagnostics.get("capacity_fallback") and text_out:
        used = diagnostics.get("used_model") or model
        text_out = (
            f"{text_out}\n\n[note: requested model capacity exhausted; "
            f"answered with fallback `{used}`]"
        )
    return {
        "text": text_out,
        "model": model,
        "created": int(time.time()),
        "finish_reason": extracted["finish_reason"],
        "usage": extracted.get("usage", {}),
        "reasoning": extracted.get("reasoning", ""),
        "tool_calls": extracted.get("tool_calls", []),
        "streamed": bool(diagnostics.get("streamed")),
        "capacity_fallback": bool(diagnostics.get("capacity_fallback")),
        "used_model": diagnostics.get("used_model") or model,
        **response_schema.standard_fields(
            model=model,
            usage=extracted.get("usage", {}),
            warnings=warnings,
            diagnostics=diagnostics,
            backend=backend,
        ),
    }


def to_jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
