"""Safe local operation latency summaries without request or response content."""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil((len(ordered) * percentile) - 1))
    return ordered[min(index, len(ordered) - 1)]


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
        }
    return {
        "schema": "agent_hub_operation_metrics_v1",
        "content_recorded": False,
        "operations": operations,
    }
