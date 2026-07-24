"""Bounded, redacted event journals committed atomically with run state."""

from __future__ import annotations

from hashlib import sha256
import math
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Mapping

from . import store

EVENT_SCHEMA = "agent_hub_run_event_v1"
EVENT_LIST_SCHEMA = "agent_hub_run_events_v1"
MAX_EVENTS = 200
MAX_EVENT_LIMIT = 100
MAX_EVENT_STRING_CHARS = 256
MAX_EVENT_LIST_ITEMS = 100

_STRING_FIELDS = {
    "run_kind",
    "workflow_id",
    "status",
    "stage_id",
    "action_id",
    "tool",
    "provider",
    "model",
    "error_type",
    "pause_reason",
}
_BOOL_FIELDS = {"success", "retryable"}
_INTEGER_FIELDS = {
    "base_revision",
    "resulting_revision",
    "elapsed_ms",
    "prompt_chars",
    "result_chars",
    "wave_index",
    "leaf_calls",
    "pending_steps",
}
_HASH_FIELDS = {"prompt_sha256", "result_sha256"}
_LIST_FIELDS = {"completed_step_ids"}
_USAGE_FIELD = "usage"
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")


def text_identity(text: Any, *, prefix: str) -> Dict[str, Any]:
    """Return length/hash metadata without retaining the supplied text."""

    value = str(text or "")
    return {
        f"{prefix}_chars": len(value),
        f"{prefix}_sha256": sha256(value.encode("utf-8")).hexdigest(),
    }


def _bounded_label(value: Any) -> str:
    label = str(value or "")[:MAX_EVENT_STRING_CHARS]
    if not label:
        return ""
    return label if _SAFE_LABEL_RE.fullmatch(label) else "redacted"


def _safe_integer(value: Any, *, field: str) -> int | None:
    if value is None and field == "base_revision":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _safe_usage(value: Any) -> Dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    usage: Dict[str, int | float] = {}
    for raw_key, raw_value in list(value.items())[:32]:
        if (
            isinstance(raw_value, (int, float))
            and not isinstance(raw_value, bool)
            and (not isinstance(raw_value, float) or math.isfinite(raw_value))
            and raw_value >= 0
        ):
            key = _bounded_label(raw_key)[:64]
            if key:
                usage[key] = raw_value
    return usage


def _normalize_event(raw: Mapping[str, Any]) -> Dict[str, Any]:
    seq = _safe_integer(raw.get("seq"), field="seq")
    event_type = _bounded_label(raw.get("type"))
    if not event_type:
        raise ValueError("event type is required")
    at = raw.get("at")
    if (
        isinstance(at, bool)
        or not isinstance(at, (int, float))
        or not math.isfinite(float(at))
        or float(at) < 0
    ):
        raise ValueError("event timestamp must be a non-negative finite number")
    event: Dict[str, Any] = {
        "schema": EVENT_SCHEMA,
        "seq": seq,
        "type": event_type,
        "at": float(at),
    }
    for field in _STRING_FIELDS:
        if field in raw and raw[field] is not None:
            event[field] = _bounded_label(raw[field])
    for field in _BOOL_FIELDS:
        if field in raw:
            if not isinstance(raw[field], bool):
                raise ValueError(f"{field} must be a boolean")
            event[field] = raw[field]
    for field in _INTEGER_FIELDS:
        if field in raw:
            event[field] = _safe_integer(raw[field], field=field)
    for field in _HASH_FIELDS:
        if field in raw:
            digest = str(raw[field] or "")
            if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest):
                event[field] = digest
    if _USAGE_FIELD in raw:
        usage = _safe_usage(raw[_USAGE_FIELD])
        if usage:
            event[_USAGE_FIELD] = usage
    for field in _LIST_FIELDS:
        value = raw.get(field)
        if isinstance(value, (list, tuple)):
            event[field] = [
                _bounded_label(item)[:128]
                for item in value[:MAX_EVENT_LIST_ITEMS]
                if str(item or "")
            ]
    return event


def append_event(
    state: Dict[str, Any],
    event_type: str,
    *,
    at: float,
    base_revision: int | None,
    resulting_revision: int,
    **fields: Any,
) -> Dict[str, Any]:
    """Append one safe event to state and prune the oldest entries."""

    raw_seq = state.get("event_seq", 0)
    if isinstance(raw_seq, bool) or not isinstance(raw_seq, int) or raw_seq < 0:
        raise ValueError("event_seq must be a non-negative integer")
    raw_dropped = state.get("events_dropped", 0)
    if (
        isinstance(raw_dropped, bool)
        or not isinstance(raw_dropped, int)
        or raw_dropped < 0
    ):
        raise ValueError("events_dropped must be a non-negative integer")
    existing = state.get("events", [])
    if not isinstance(existing, list):
        raise ValueError("events must be an array")
    event = _normalize_event(
        {
            "seq": raw_seq + 1,
            "type": event_type,
            "at": at,
            "base_revision": base_revision,
            "resulting_revision": resulting_revision,
            **fields,
        }
    )
    journal, corrupt_dropped = _normalized_events(existing)
    journal.append(event)
    newly_dropped = max(0, len(journal) - MAX_EVENTS)
    if newly_dropped:
        journal = journal[newly_dropped:]
    state["events"] = journal
    state["event_seq"] = event["seq"]
    state["events_dropped"] = raw_dropped + corrupt_dropped + newly_dropped
    return event


def event_summary(state: Mapping[str, Any]) -> Dict[str, Any]:
    events = state.get("events")
    count = len(events) if isinstance(events, list) else 0
    latest = state.get("event_seq", 0)
    dropped = state.get("events_dropped", 0)
    return {
        "schema": "agent_hub_run_event_summary_v1",
        "retained": max(0, int(count)),
        "latest_seq": latest
        if isinstance(latest, int) and not isinstance(latest, bool) and latest >= 0
        else 0,
        "dropped": dropped
        if isinstance(dropped, int) and not isinstance(dropped, bool) and dropped >= 0
        else 0,
    }


def _state_project_root(state: Mapping[str, Any]) -> str:
    raw = state.get("project_root")
    if not raw and isinstance(state.get("options"), Mapping):
        raw = state["options"].get("project_root")
    root = Path(str(raw or "")).expanduser()
    if not root.is_absolute():
        raise ValueError("persisted run does not have a canonical project_root")
    canonical = str(root.resolve())
    if str(root) != canonical:
        raise ValueError("persisted run project_root is not canonical")
    return canonical


def _normalized_events(values: Any) -> tuple[list[Dict[str, Any]], int]:
    if not isinstance(values, list):
        return [], 0
    safe = []
    skipped = 0
    for raw in values:
        try:
            if not isinstance(raw, Mapping):
                raise ValueError("event must be an object")
            safe.append(_normalize_event(raw))
        except (TypeError, ValueError):
            skipped += 1
    safe.sort(key=lambda item: item["seq"])
    return safe, skipped


def read_run_events(
    run_id: str,
    *,
    project_root: str,
    after_seq: int = 0,
    limit: int = 50,
) -> Dict[str, Any]:
    if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
        raise ValueError("after_seq must be a non-negative integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_EVENT_LIMIT
    ):
        raise ValueError(f"limit must be between 1 and {MAX_EVENT_LIMIT}")
    state = store.load_strict(store.validate_run_id(run_id))
    requested_root = str(Path(project_root).expanduser().resolve())
    authoritative_root = _state_project_root(state)
    if requested_root != authoritative_root:
        raise ValueError("run belongs to a different project")
    safe, skipped = _normalized_events(state.get("events"))
    available = [item for item in safe if item["seq"] > after_seq]
    page = available[:limit]
    latest = max(
        (
            int(state.get("event_seq"))
            if isinstance(state.get("event_seq"), int)
            and not isinstance(state.get("event_seq"), bool)
            else 0
        ),
        max((item["seq"] for item in safe), default=0),
    )
    oldest = safe[0]["seq"] if safe else None
    return {
        "schema": EVENT_LIST_SCHEMA,
        "run_id": state["run_id"],
        "project_root": authoritative_root,
        "store_revision": store._current_revision(state),
        "events": page,
        "oldest_available_seq": oldest,
        "latest_seq": latest,
        "events_dropped": (
            int(state.get("events_dropped"))
            if isinstance(state.get("events_dropped"), int)
            and not isinstance(state.get("events_dropped"), bool)
            and int(state.get("events_dropped")) >= 0
            else 0
        ),
        "corrupt_events_skipped": skipped,
        "gap": bool(oldest is not None and after_seq + 1 < oldest),
        "has_more": len(available) > len(page),
        "next_after_seq": page[-1]["seq"] if page else after_seq,
    }


def completed_step_ids(
    before: Iterable[str],
    after: Iterable[str],
) -> list[str]:
    previous = {str(item) for item in before}
    return [
        str(item)
        for item in after
        if str(item) and str(item) not in previous
    ][:MAX_EVENT_LIST_ITEMS]
