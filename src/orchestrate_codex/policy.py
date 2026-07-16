"""Document class and context policy (provider-neutral)."""

from __future__ import annotations

from typing import Any, Dict

DOC_CLASSES = {
    "durable": {
        "git": "off",
        "session_diary": "off",
        "facts": "required",
        "description": "Stable product docs (README, technical-doc). No recent-work tone.",
    },
    "change": {
        "git": "on",
        "session_diary": "allowed",
        "facts": "snapshot",
        "description": "PR descriptions and release notes grounded in git changes.",
    },
    "transform": {
        "git": "off",
        "session_diary": "off",
        "facts": "source_only",
        "description": "Polish/translate/summarize existing source text only.",
    },
    "direct": {
        "git": "n/a",
        "session_diary": "n/a",
        "facts": "prompt_only",
        "description": "Single-shot leaf call; no multi-stage orchestration.",
    },
}


def get_policy(doc_class: str) -> Dict[str, Any]:
    key = (doc_class or "").strip().lower()
    if key not in DOC_CLASSES:
        raise ValueError(f"unknown doc_class: {doc_class}. Choose from {sorted(DOC_CLASSES)}")
    return {"doc_class": key, **DOC_CLASSES[key]}
