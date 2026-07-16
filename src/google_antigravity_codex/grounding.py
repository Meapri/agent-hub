"""Google Search grounding helper for Antigravity Gemini requests."""

from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from . import chat, network, provider, response as response_schema

URL_RE = re.compile(r"https?://[^\s)>\]}\"']+")
TRAILING_URL_CHARS = ".,;:*!?_~`"
CLAIM_RE = re.compile(
    r"(?<![\w])(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*"
    r"(?:GB|TB|MB|GHz|MHz|nm|W|Wh|fps|FPS|core|cores|tokens?|%|년|월|일|Q[1-4])",
    re.IGNORECASE,
)
DEFAULT_OFFICIAL_DOMAINS = (
    "google.com",
    "blog.google",
    "deepmind.google",
    "ai.google.dev",
    "developers.googleblog.com",
    "nvidia.com",
    "microsoft.com",
    "github.com",
    "openai.com",
)


def _clean_url(url: str) -> str:
    return (url or "").rstrip(TRAILING_URL_CHARS)


def _official_domains() -> tuple[str, ...]:
    extras = [
        item.strip().lower().removeprefix("www.")
        for item in os.getenv("GOOGLE_ANTIGRAVITY_OFFICIAL_DOMAINS", "").split(",")
        if item.strip()
    ]
    return tuple(dict.fromkeys([*DEFAULT_OFFICIAL_DOMAINS, *extras]))


def extract_urls(text: str) -> List[str]:
    seen = set()
    urls: List[str] = []
    for match in URL_RE.finditer(text or ""):
        url = _clean_url(match.group(0))
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def classify_source(url: str) -> str:
    domain = urllib.parse.urlparse(url).netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    if domain == "vertexaisearch.cloud.google.com":
        return "grounding_redirect"
    academic = ("arxiv.org", "nature.com", "science.org", "acm.org", "ieee.org")
    community = ("reddit.com", "x.com", "twitter.com", "news.ycombinator.com")
    if any(domain == item or domain.endswith("." + item) for item in _official_domains()):
        return "official"
    if any(domain == item or domain.endswith("." + item) for item in academic):
        return "academic"
    if any(domain == item or domain.endswith("." + item) for item in community):
        return "community"
    return "media_or_web" if domain else "unknown"


def resolve_url(url: str, *, timeout_sec: int = 8) -> Dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    source = {
        "url": url,
        "resolved_url": url,
        "domain": parsed.netloc.lower(),
        "source_type": classify_source(url),
        "redirect_resolved": False,
        "resolution_error": "",
    }
    if "vertexaisearch.cloud.google.com" not in parsed.netloc.lower():
        return source
    try:
        network.validate_public_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": "google-antigravity-codex/0.1.0"})
        with network.public_url_opener().open(request, timeout=max(1, timeout_sec)) as response:
            final_url = response.geturl()
        network.validate_public_url(final_url)
    except Exception as exc:
        source["resolution_error"] = str(exc)
        return source
    final = urllib.parse.urlparse(final_url)
    source.update(
        {
            "resolved_url": final_url,
            "domain": final.netloc.lower(),
            "source_type": classify_source(final_url),
            "redirect_resolved": final_url != url,
            "resolution_error": "",
        }
    )
    return source


def resolve_url_with_curl(url: str, *, timeout_sec: int = 8) -> str:
    """Retained for API compatibility; unsafe curl redirect following is disabled."""
    return ""


def extract_numeric_claims(text: str) -> List[str]:
    seen = set()
    claims: List[str] = []
    for match in CLAIM_RE.finditer(text or ""):
        claim = match.group(0).strip()
        if claim not in seen:
            seen.add(claim)
            claims.append(claim)
    return claims[:40]


def build_evidence(answer: str, sources: List[Dict[str, Any]], claims: List[str]) -> List[Dict[str, Any]]:
    official_urls = [str(item.get("resolved_url") or item.get("url")) for item in sources if item.get("source_type") == "official"]
    fallback_urls = [str(item.get("resolved_url") or item.get("url")) for item in sources]
    urls = official_urls or fallback_urls
    evidence: List[Dict[str, Any]] = []
    for claim in claims[:12]:
        evidence.append({"claim": claim, "source_urls": urls[:3], "confidence": "needs_review"})
    if not evidence and answer and urls:
        evidence.append({"claim": answer[:240], "source_urls": urls[:3], "confidence": "grounded"})
    return evidence


def source_summary(sources: List[Dict[str, Any]]) -> str:
    lines = []
    for idx, item in enumerate(sources[:10], start=1):
        url = item.get("resolved_url") or item.get("url") or ""
        kind = item.get("source_type") or "unknown"
        lines.append(f"{idx}. [{kind}] {url}")
    return "\n".join(lines)


def _source_key(item: Dict[str, Any]) -> str:
    return str(item.get("resolved_url") or item.get("url") or "").rstrip("/")


def _is_unresolved_grounding_redirect(item: Dict[str, Any]) -> bool:
    return item.get("source_type") == "grounding_redirect" and not item.get("redirect_resolved")


def _direct_source_retry_prompt(query: str, answer: str, max_sources: int, language: str) -> str:
    return (
        "Use native Google Search grounding to recover direct public source URLs for this answer. "
        "Return only canonical https:// URLs from official or publisher pages. Do not return "
        "vertexaisearch.cloud.google.com, redirect URLs, tracking URLs, or explanations. "
        f"Return at most {max_sources} URLs, one per line. Language preference: {language}.\n\n"
        f"Original question:\n{query}\n\nAnswer to support:\n{answer[:4000]}"
    )


def recover_direct_sources(arguments: Dict[str, Any], answer: str, existing: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not any(_is_unresolved_grounding_redirect(item) for item in existing):
        return existing
    if arguments.get("direct_source_retry") is False:
        return existing
    max_sources = max(1, min(int(arguments.get("max_sources") or 5), 10))
    retry_response = chat.run_chat(
        {
            "prompt": _direct_source_retry_prompt(
                str(arguments.get("query") or ""),
                answer,
                max_sources,
                str(arguments.get("language") or "ko"),
            ),
            "model": str(arguments.get("model") or chat.DEFAULT_MODEL),
            "grounding": "always",
            "timeout_sec": arguments.get("timeout_sec") or 180,
            "retry_count": arguments.get("retry_count", 1),
            "retry_sleep_cap_sec": arguments.get("retry_sleep_cap_sec", 8),
        }
    )
    direct_sources: List[Dict[str, Any]] = []
    for url in extract_urls(str(retry_response.get("text") or "")):
        resolved = resolve_url(url)
        if resolved.get("source_type") != "grounding_redirect":
            resolved["recovered_by"] = "direct_source_retry"
            direct_sources.append(resolved)
    if not direct_sources:
        return existing
    kept = [item for item in existing if not _is_unresolved_grounding_redirect(item)]
    seen = {_source_key(item) for item in kept}
    for item in direct_sources:
        key = _source_key(item)
        if key and key not in seen:
            kept.append(item)
            seen.add(key)
    return kept[:max_sources]


def build_prompt(arguments: Dict[str, Any]) -> str:
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    max_sources = max(1, min(int(arguments.get("max_sources") or 5), 10))
    freshness = str(arguments.get("freshness") or "auto").strip() or "auto"
    language = str(arguments.get("language") or "ko").strip() or "ko"
    return (
        "Use native Google Search grounding. Answer in "
        f"{language}. Freshness preference: {freshness}. Include up to {max_sources} "
        "direct source URLs when available. Separate verified facts from inference. "
        "If grounding does not find enough evidence, say that clearly. End with a "
        "Sources section whose lines include full https:// URLs.\n\n"
        f"Question: {query}"
    )


def run_grounded_search(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from . import model_prefs

    provider.require_capability("native_grounding")
    model = model_prefs.resolve_model(
        explicit=str(arguments.get("model") or ""),
        task="grounded-search",
        fallback=chat.DEFAULT_MODEL,
    ) or chat.DEFAULT_MODEL
    prompt = build_prompt(arguments)
    chat_response = chat.run_chat(
        {
            "prompt": prompt,
            "model": model,
            "task": "grounded-search",
            "grounding": "always",
            "timeout_sec": arguments.get("timeout_sec") or 180,
            "retry_count": arguments.get("retry_count", 1),
            "retry_sleep_cap_sec": arguments.get("retry_sleep_cap_sec", 8),
        }
    )
    answer = chat_response.get("text", "")
    resolve_sources = bool(arguments.get("resolve_sources", True))
    sources = [
        resolve_url(url) if resolve_sources else {
            "url": url,
            "resolved_url": url,
            "domain": urllib.parse.urlparse(url).netloc.lower(),
            "source_type": classify_source(url),
            "redirect_resolved": False,
            "resolution_error": "",
        }
        for url in extract_urls(answer)
    ]
    if resolve_sources:
        sources = recover_direct_sources(arguments, answer, sources)
    official_count = sum(1 for item in sources if item.get("source_type") == "official")
    unresolved_redirect_count = sum(1 for item in sources if item.get("source_type") == "grounding_redirect")
    claims = extract_numeric_claims(answer)
    quality = {
        "source_count": len(sources),
        "official_source_count": official_count,
        "unresolved_redirect_count": unresolved_redirect_count,
        "needs_manual_source_check": unresolved_redirect_count > 0 or official_count == 0,
    }
    warnings = []
    if not answer:
        warnings.append("empty_grounded_answer")
    if quality["needs_manual_source_check"]:
        warnings.append("needs_manual_source_check")
    return {
        "text": answer,
        "answer": answer,
        "sources": sources,
        "source_summary": source_summary(sources),
        "numeric_claims": claims,
        "evidence": build_evidence(answer, sources, claims),
        "quality_signals": quality,
        "model": model,
        "grounding": "native_google_search",
        "usage": chat_response.get("usage", {}),
        **response_schema.standard_fields(
            model=model,
            usage=chat_response.get("usage", {}),
            warnings=warnings,
            diagnostics=chat_response.get("diagnostics", {}),
            backend=str(chat_response.get("backend") or "agy-oauth-code-assist"),
        ),
    }
