"""Task-aware model routing helpers."""

from __future__ import annotations

from typing import Any, Dict, List

from . import chat, image, model_prefs, response


ROUTES: Dict[str, Dict[str, Any]] = {
    "chat": {
        "model": chat.DEFAULT_MODEL,
        "candidates": [
            "gemini-3.5-flash-high",
            "gemini-3.5-flash",
            "gemini-3.1-pro-high",
            "gemini-3.1-pro-preview",
        ],
        "tool": "google_antigravity_chat",
        "reason": "Balanced default for general Codex chat.",
    },
    "code": {
        "model": "gemini-3.1-pro-high",
        "candidates": [
            "gemini-3.1-pro-high",
            "gemini-3.1-pro-preview",
            "claude-opus-4-6-thinking",
            "claude-sonnet-4-6-thinking",
            "gemini-3.5-flash-high",
        ],
        "tool": "google_antigravity_chat",
        "reason": "Higher reasoning budget for code, architecture, and debugging.",
    },
    "fast": {
        "model": "gemini-3.5-flash-low",
        "candidates": ["gemini-3.5-flash-low", "gemini-3.5-flash-medium", "gemini-3.5-flash-high"],
        "tool": "google_antigravity_chat",
        "reason": "Fast response path for lightweight tasks.",
    },
    "grounded-search": {
        "model": "gemini-3.5-flash-high",
        "candidates": ["gemini-3.5-flash-high", "gemini-3.1-pro-high"],
        "tool": "google_grounded_search",
        "reason": "Grounding-capable model for current facts and source checks.",
        "required_provider": "agy-oauth",
    },
    "writing": {
        "model": "gemini-3.1-pro-high",
        "candidates": ["gemini-3.1-pro-high", "gemini-3.5-flash-high", "claude-sonnet-4-6-thinking"],
        "tool": "google_antigravity_write",
        "reason": "Best default for polished prose and Korean/English tone work.",
    },
    "release": {
        "model": "gemini-3.1-pro-high",
        "candidates": ["gemini-3.1-pro-high", "gemini-3.5-flash-high"],
        "tool": "google_antigravity_release_draft",
        "reason": "Release prose benefits from stronger synthesis after deterministic snapshotting.",
    },
    "image": {
        "model": image.DEFAULT_MODEL,
        "candidates": [image.DEFAULT_MODEL, "gemini-3-pro-image", "gemini-2.5-flash-image"],
        "tool": "google_antigravity_generate_image",
        "reason": "Image-capable Antigravity Gemini models.",
        "required_provider": "agy-oauth",
    },
}


ALIASES = {
    "search": "grounded-search",
    "grounding": "grounded-search",
    "current": "grounded-search",
    "docs": "writing",
    "prose": "writing",
    "pr": "release",
    "release-notes": "release",
    "picture": "image",
    "generate-image": "image",
}


def normalize_task(value: Any) -> str:
    text = str(value or "chat").strip().lower().replace("_", "-")
    return ALIASES.get(text, text if text in ROUTES else "chat")


def route_model(arguments: Dict[str, Any]) -> Dict[str, Any]:
    task = normalize_task(arguments.get("task") or arguments.get("intent"))
    if arguments.get("image") is True:
        task = "image"
    if str(arguments.get("grounding") or "").lower() in {"always", "required"}:
        task = "grounded-search"
    if str(arguments.get("speed") or "").lower() in {"fast", "low-latency"} and task == "chat":
        task = "fast"
    route = ROUTES[task]
    candidates: List[str] = list(route["candidates"])
    # User-saved prefs and explicit preferred_model win over static route defaults.
    preferred = str(arguments.get("preferred_model") or arguments.get("model") or "").strip()
    saved = model_prefs.resolve_model(task=task, fallback="")
    ordered: List[str] = []
    for item in (preferred, saved, route.get("model"), *candidates):
        mid = model_prefs.normalize_model_id(str(item or ""))
        if mid and mid not in ordered:
            ordered.append(mid)
    candidates = ordered or candidates
    model = candidates[0]
    source = "route-default"
    if preferred:
        source = "call-preferred"
    elif saved:
        source = "user-pref"
    return {
        "text": f"Recommended {model} via {route['tool']} for task '{task}' ({source}).",
        "task": task,
        "recommended_model": model,
        "model": model,
        "candidates": candidates,
        "selection_source": source,
        "saved_pref": saved or None,
        "tool": route["tool"],
        "reason": route["reason"],
        **({"required_provider": route["required_provider"]} if route.get("required_provider") else {}),
        "arguments_template": {
            "model": model,
            **({"grounding": "always"} if task == "grounded-search" else {}),
        },
        **response.standard_fields(model=model, backend="local-router"),
    }
