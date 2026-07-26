"""Web and X search through xAI's Responses API tools."""

from __future__ import annotations

from typing import Any, Dict, List

from agent_hub.core import limits

from . import api, auth, chat, models, response, security

SEARCH_COMPLETE_MARKER = "<agent_hub_search_complete/>"


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


def _search_prompt(query: str, language: str, *, retry: bool) -> str:
    retry_instruction = (
        "A previous attempt ended without a complete final answer. Replace it with a complete "
        "answer now. "
        if retry
        else ""
    )
    return (
        f"Answer in {language}. {retry_instruction}"
        "Return only one self-contained user-visible final answer; do not narrate searches, "
        "tool calls, or drafting progress. Separate verified facts from inference, address every "
        "part of the question, and include source citations. Before finishing, check that the "
        "answer is complete. End the response with this exact marker on its own line: "
        f"{SEARCH_COMPLETE_MARKER}\n\nQuestion: {query}"
    )


def _has_completion_marker(text: str) -> bool:
    return text.rstrip().endswith(SEARCH_COMPLETE_MARKER)


def _strip_completion_marker(text: str) -> str:
    stripped = text.rstrip()
    if stripped.endswith(SEARCH_COMPLETE_MARKER):
        stripped = stripped[: -len(SEARCH_COMPLETE_MARKER)]
    return stripped.rstrip()


def _aggregate_usage(payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    present = {key: False for key in totals}
    for payload in payloads:
        for key, value in chat._usage(payload).items():
            if key in totals and isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] += int(value)
                present[key] = True
    return {key: value for key, value in totals.items() if present[key]}


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
            x_tool["allowed_x_handles"] = [
                str(item) for item in arguments["allowed_x_handles"][:20]
            ]
        if arguments.get("from_date"):
            x_tool["from_date"] = str(arguments["from_date"])
        if arguments.get("to_date"):
            x_tool["to_date"] = str(arguments["to_date"])
        tools.append(x_tool)
    language = str(arguments.get("language") or "ko")
    max_sources = max(
        1,
        min(
            int(arguments.get("max_sources") or limits.MAX_SEARCH_SOURCES),
            limits.MAX_SEARCH_SOURCES,
        ),
    )
    max_output_tokens = max(1, int(arguments.get("max_tokens") or chat.DEFAULT_MAX_TOKENS))
    retry_count = max(
        0,
        min(
            int(arguments.get("retry_count", limits.MAX_PROVIDER_RETRIES)),
            limits.MAX_PROVIDER_RETRIES,
        ),
    )
    timeout = float(arguments.get("timeout_sec") or limits.MAX_PROVIDER_TIMEOUT_SECONDS)
    payloads: List[Dict[str, Any]] = []
    raw_text = ""
    status = "incomplete"
    complete = False
    for attempt in range(retry_count + 1):
        body = {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": _search_prompt(query, language, retry=attempt > 0),
                }
            ],
            "tools": tools,
            "max_output_tokens": max_output_tokens,
        }
        payload = api.responses_create(body, timeout=timeout)
        payloads.append(payload)
        raw_text = chat._extract_responses_text(payload)
        status = str(payload.get("status") or "incomplete").strip().lower()
        complete = status == "completed" and _has_completion_marker(raw_text)
        if complete:
            break

    text = _strip_completion_marker(raw_text)
    sources = _annotation_sources(payload)[:max_sources]
    warnings = [] if sources else ["no_search_citations_returned"]
    if complete and len(payloads) > 1:
        warnings.append(f"search_completion_recovered:{len(payloads) - 1}")
    elif not complete:
        warnings.append(
            "incomplete_search_answer:"
            + ("missing_completion_marker" if status == "completed" else status)
        )
    auth_ctx = auth.resolve_auth()
    finish_reason = status if complete else "incomplete"
    return {
        "text": text,
        "answer": text,
        "sources": sources,
        "citations": [item["citation"] for item in sources],
        "model": str(payload.get("model") or model),
        "finish_reason": finish_reason,
        "search_provider": f"xai_{source}_search",
        **response.standard_fields(
            success=bool(text) and complete,
            provider="xai",
            backend="xai-responses-search",
            model=str(payload.get("model") or model),
            usage=_aggregate_usage(payloads),
            warnings=warnings,
            diagnostics={
                "auth_mode": auth_ctx.get("mode"),
                "source": source,
                "provider_status": status,
                "completion_marker_seen": _has_completion_marker(raw_text),
                "completion_attempts": len(payloads),
            },
        ),
    }
