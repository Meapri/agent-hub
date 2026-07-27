"""Safe local operation latency summaries without request or response content."""

from __future__ import annotations

from collections import Counter
import math
from typing import Any, Iterable, Mapping

MAX_REPORTED_FAILURE_CODES = 5


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil((len(ordered) * percentile) - 1))
    return ordered[min(index, len(ordered) - 1)]


def _field(row: Mapping[str, Any], key: str) -> Any:
    # Accepts sqlite3.Row (no .get) and plain mappings from older callers.
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _failure_summary(samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    codes: Counter[str] = Counter()
    unrecorded = 0
    total = 0
    for item in samples:
        if item["success"]:
            continue
        total += 1
        code = _field(item, "error_code")
        if isinstance(code, str) and code:
            codes[code] += 1
        else:
            # Rows written before schema 10 carry no code; reporting them apart
            # keeps the top-code list from looking artificially complete.
            unrecorded += 1
    ranked = sorted(codes.items(), key=lambda pair: (-pair[1], pair[0]))
    return {
        "count": total,
        "unrecorded": unrecorded,
        "top_codes": [
            {"code": code, "count": count} for code, count in ranked[:MAX_REPORTED_FAILURE_CODES]
        ],
        "distinct_codes": len(codes),
    }


def summarize_operation_metrics(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["operation"]), []).append(row)
    operations = {}
    for operation, samples in sorted(grouped.items()):
        durations = [int(item["duration_ms"]) for item in samples]
        successes = sum(bool(item["success"]) for item in samples)
        operations[operation] = {
            "count": len(samples),
            "success_rate": round(successes / len(samples), 4),
            "latency_ms": {
                "p50": _percentile(durations, 0.50),
                "p95": _percentile(durations, 0.95),
                "max": max(durations),
            },
            "failures": _failure_summary(samples),
        }
    return {
        "schema": "agent_hub_operation_metrics_v2",
        "content_recorded": False,
        "operations": operations,
    }
