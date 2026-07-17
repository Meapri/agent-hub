"""Web and X search through xAI's Responses API tools."""

from __future__ import annotations

from typing import Any, Dict, List

from . import api, auth, chat, models, response, security


def _annotation_sources(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    seen = set()
    for url in payload.get("citations") or []:
        text = str(url or "")
        if text and text not in seen:
            seen.add(text)
            found.append({"url": text, "title": "", "citation": {"url": text}})
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if not isinstance(block, dict):
                continue
            for annotation in block.get("annotations") or []:
                if not isinstance(annotation, dict):
                    continue
                url = str(annotation.get("url") or annotation.get("source_url") or "")
                if url and url not in seen:
                    seen.add(url)
                    found.append(
                        {
                            "url": url,
                            "title": str(annotation.get("title") or annotation.get("label") or ""),
                            "citation": annotation,
                        }
                    )
    return found


def run_search(arguments: Dict[str, Any]) -> Dict[str, Any]:
    security.require_consent()
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    model = str(arguments.get("model") or models.DEFAULT_MODEL)
    source = str(arguments.get("source") or "web").strip().lower()
    if source not in {"web", "x", "both"}:
        raise ValueError("source must be web, x, or both")
    tools: List[Dict[str, Any]] = []
    if source in {"web", "both"}:
        web_tool: Dict[str, Any] = {"type": "web_search"}
        if isinstance(arguments.get("allowed_domains"), list):
            web_tool["allowed_domains"] = [str(item) for item in arguments["allowed_domains"][:20]]
        if isinstance(arguments.get("blocked_domains"), list):
            web_tool["excluded_domains"] = [str(item) for item in arguments["blocked_domains"][:20]]
        tools.append(web_tool)
    if source in {"x", "both"}:
        x_tool: Dict[str, Any] = {"type": "x_search"}
        if isinstance(arguments.get("allowed_x_handles"), list):
            x_tool["allowed_x_handles"] = [str(item) for item in arguments["allowed_x_handles"][:20]]
        if arguments.get("from_date"):
            x_tool["from_date"] = str(arguments["from_date"])
        if arguments.get("to_date"):
            x_tool["to_date"] = str(arguments["to_date"])
        tools.append(x_tool)
    language = str(arguments.get("language") or "ko")
    max_sources = max(1, min(int(arguments.get("max_sources") or 5), 10))
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": (
                    f"Answer in {language}. Separate verified facts from inference and include "
                    f"source citations.\n\nQuestion: {query}"
                ),
            }
        ],
        "tools": tools,
        "max_output_tokens": max(1, int(arguments.get("max_tokens") or chat.DEFAULT_MAX_TOKENS)),
    }
    payload = api.responses_create(body, timeout=float(arguments.get("timeout_sec") or 180))
    text = chat._extract_responses_text(payload)
    sources = _annotation_sources(payload)[:max_sources]
    status = str(payload.get("status") or "completed")
    warnings = [] if sources else ["no_search_citations_returned"]
    auth_ctx = auth.resolve_auth()
    return {
        "text": text,
        "answer": text,
        "sources": sources,
        "citations": [item["citation"] for item in sources],
        "model": str(payload.get("model") or model),
        "finish_reason": status,
        "search_provider": f"xai_{source}_search",
        **response.standard_fields(
            success=bool(text) and status != "incomplete",
            provider="xai",
            backend="xai-responses-search",
            model=str(payload.get("model") or model),
            usage=chat._usage(payload),
            warnings=warnings,
            diagnostics={"auth_mode": auth_ctx.get("mode"), "source": source},
        ),
    }
