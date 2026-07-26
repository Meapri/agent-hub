"""Source-backed web search through Anthropic's server-side search tool."""

from __future__ import annotations

from typing import Any, Dict, List

from agent_hub.core import limits

from . import api, auth, chat, models, response, security


def _sources(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    seen = set()
    sources: List[Dict[str, Any]] = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        for citation in block.get("citations") or []:
            if not isinstance(citation, dict):
                continue
            url = str(citation.get("url") or citation.get("source") or "")
            key = url or str(citation)
            if key in seen:
                continue
            seen.add(key)
            sources.append(
                {
                    "url": url,
                    "title": str(citation.get("title") or citation.get("document_title") or ""),
                    "citation": citation,
                }
            )
    return sources


def run_search(arguments: Dict[str, Any]) -> Dict[str, Any]:
    security.require_consent()
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    model = str(arguments.get("model") or models.DEFAULT_MODEL)
    max_sources = max(
        1,
        min(
            int(arguments.get("max_sources") or limits.MAX_SEARCH_SOURCES),
            limits.MAX_SEARCH_SOURCES,
        ),
    )
    language = str(arguments.get("language") or "ko")
    tool: Dict[str, Any] = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max_sources,
    }
    allowed_domains = arguments.get("allowed_domains")
    blocked_domains = arguments.get("blocked_domains")
    if isinstance(allowed_domains, list) and allowed_domains:
        tool["allowed_domains"] = [str(item) for item in allowed_domains[:20]]
    elif isinstance(blocked_domains, list) and blocked_domains:
        tool["blocked_domains"] = [str(item) for item in blocked_domains[:20]]
    body = {
        "model": model,
        "max_tokens": max(1, int(arguments.get("max_tokens") or chat.DEFAULT_MAX_TOKENS)),
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Answer in {language}. Separate verified facts from inference and cite the "
                    f"sources returned by web search.\n\nQuestion: {query}"
                ),
            }
        ],
        "tools": [tool],
    }
    payload = api.messages_create(
        body,
        timeout=float(arguments.get("timeout_sec") or limits.MAX_PROVIDER_TIMEOUT_SECONDS),
    )
    text = chat.extract_text(payload)
    sources = _sources(payload)[:max_sources]
    stop_reason = str(payload.get("stop_reason") or "end_turn")
    warnings = [] if sources else ["no_search_citations_returned"]
    auth_ctx = auth.resolve_auth()
    return {
        "text": text,
        "answer": text,
        "sources": sources,
        "citations": [item["citation"] for item in sources],
        "model": str(payload.get("model") or model),
        "finish_reason": stop_reason,
        "search_provider": "anthropic_web_search",
        **response.standard_fields(
            success=bool(text) and stop_reason != "max_tokens",
            provider="anthropic",
            backend="anthropic-web-search",
            model=str(payload.get("model") or model),
            usage=chat.extract_usage(payload),
            warnings=warnings,
            diagnostics={"auth_mode": auth_ctx.get("mode")},
        ),
    }
