from __future__ import annotations

import math
import time

from agent_hub.v2.store import HubStore


def test_local_metric_summary_p95_is_below_250ms(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    for index in range(1_000):
        store.record_operation_metric(
            operation="agent_hub_events",
            success=True,
            duration_ms=index % 20,
        )

    samples = []
    for _ in range(40):
        started = time.perf_counter()
        result = store.operation_metrics()
        samples.append((time.perf_counter() - started) * 1000)

    p95 = sorted(samples)[math.ceil(len(samples) * 0.95) - 1]
    assert result["operations"]["agent_hub_events"]["count"] == 1_000
    assert p95 < 250
