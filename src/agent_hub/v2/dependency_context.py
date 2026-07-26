"""Deterministic compaction and budgeting for provider dependency context."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping

from .context import FACT_PACK_SCHEMA
from .contracts import MAX_INLINE_INPUT_CHARS, canonical_json
from .errors import HubV2Error


def assemble_dependency_context(parts: list[str]) -> tuple[str, dict[str, int]]:
    """Merge fact-pack artifacts without losing the original provenance IDs."""

    segments: list[tuple[str, str]] = []
    fact_pack_segment: int | None = None
    fact_pack_count = 0
    duplicate_plain_count = 0
    duplicate_item_count = 0
    seen_plain: set[str] = set()
    fact_items: dict[str, dict[str, Any]] = {}
    project_identities: list[str] = []
    requested_paths: list[str] = []
    covered_paths: list[str] = []
    missing_paths: list[str] = []
    coverage_complete = True

    for text in parts:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        is_fact_pack = (
            isinstance(parsed, Mapping)
            and parsed.get("schema") == FACT_PACK_SCHEMA
            and isinstance(parsed.get("items"), list)
        )
        if not is_fact_pack:
            digest = sha256(text.encode("utf-8")).hexdigest()
            if digest in seen_plain:
                duplicate_plain_count += 1
                continue
            seen_plain.add(digest)
            segments.append(("text", text))
            continue

        fact_pack_count += 1
        if fact_pack_segment is None:
            fact_pack_segment = len(segments)
            segments.append(("fact_pack", ""))
        project_identity = str(parsed.get("project_identity") or "")
        if project_identity and project_identity not in project_identities:
            project_identities.append(project_identity)
        coverage = parsed.get("coverage")
        if isinstance(coverage, Mapping):
            coverage_complete = coverage_complete and coverage.get("complete") is True
            for target, key in (
                (requested_paths, "requested_paths"),
                (covered_paths, "covered_paths"),
                (missing_paths, "missing_paths"),
            ):
                for value in coverage.get(key) or []:
                    path = str(value)
                    if path not in target:
                        target.append(path)
        else:
            coverage_complete = False
        for raw_item in parsed["items"]:
            if not isinstance(raw_item, Mapping):
                coverage_complete = False
                continue
            item = dict(raw_item)
            item.pop("collected_at", None)
            identity = sha256(canonical_json(item).encode("utf-8")).hexdigest()
            if identity in fact_items:
                duplicate_item_count += 1
                continue
            fact_items[identity] = item

    if fact_pack_segment is not None:
        merged_fact_pack: dict[str, Any] = {
            "schema": FACT_PACK_SCHEMA,
            "items": list(fact_items.values()),
            "coverage": {
                "requested_paths": requested_paths,
                "covered_paths": covered_paths,
                "missing_paths": missing_paths,
                "complete": coverage_complete and not missing_paths,
            },
            "total_chars": sum(len(str(item.get("content") or "")) for item in fact_items.values()),
            "retrieval": "merged_dependency_artifacts",
            "source_fact_pack_count": fact_pack_count,
        }
        if len(project_identities) == 1:
            merged_fact_pack["project_identity"] = project_identities[0]
        elif project_identities:
            merged_fact_pack["project_identities"] = project_identities
        segments[fact_pack_segment] = ("fact_pack", canonical_json(merged_fact_pack))

    rendered = [text for _, text in segments]
    return "\n\n".join(rendered), {
        "source_artifact_count": len(parts),
        "context_segment_count": len(rendered),
        "duplicate_artifact_count": duplicate_plain_count,
        "fact_pack_count": fact_pack_count,
        "fact_pack_item_count": len(fact_items),
        "fact_pack_duplicate_item_count": duplicate_item_count,
    }


def enforce_provider_context_budget(
    text: str,
    *,
    max_tokens: int,
    context_stats: Mapping[str, int],
    max_input_tokens: int | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, int]:
    """Reject context that already exceeds the task budget before invoking a worker."""

    encoded_bytes = len(text.encode("utf-8"))
    estimated_input_tokens = (encoded_bytes + 3) // 4
    effective_input_limit = min(
        max_tokens,
        max_input_tokens if max_input_tokens is not None else max_tokens,
    )
    safe_details = {
        "context_chars": len(text),
        "context_bytes": encoded_bytes,
        "estimated_input_tokens": estimated_input_tokens,
        "token_budget": max_tokens,
        "model_max_input_tokens": max_input_tokens or max_tokens,
        "effective_input_limit": effective_input_limit,
        "max_context_chars": MAX_INLINE_INPUT_CHARS,
        **{
            str(key): int(value)
            for key, value in context_stats.items()
            if isinstance(value, int) and not isinstance(value, bool)
        },
    }
    if len(text) > MAX_INLINE_INPUT_CHARS or estimated_input_tokens > effective_input_limit:
        public_details: dict[str, Any] = dict(safe_details)
        if provider:
            public_details["provider"] = provider
        if model:
            public_details["model"] = model
        raise HubV2Error(
            "provider_context_limit",
            "The assembled provider context exceeds the effective input limit.",
            scope="context",
            retryable=False,
            safe_details=public_details,
        )
    return safe_details
