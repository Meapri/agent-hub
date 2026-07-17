"""Bounded execution helpers for independent provider calls."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import time
from typing import Callable, Generic, List, Sequence, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class CallOutcome(Generic[T]):
    """One call result, kept in the caller's original order."""

    value: T | None
    error: Exception | None
    elapsed_ms: int


def run_ordered(
    calls: Sequence[Callable[[], T]],
    *,
    execution: str = "parallel",
    max_workers: int = 3,
) -> List[CallOutcome[T]]:
    """Run independent calls sequentially or in a bounded thread pool.

    Network adapters are synchronous, so threads let independent provider requests overlap.
    Exceptions stay isolated in their own outcome and results always retain input order.
    """

    mode = str(execution or "parallel").strip().lower()
    if mode not in {"parallel", "sequential"}:
        raise ValueError("execution must be parallel or sequential")
    if not calls:
        return []

    workers = max(1, min(int(max_workers), len(calls)))

    def invoke(call: Callable[[], T]) -> CallOutcome[T]:
        started = time.monotonic()
        try:
            return CallOutcome(
                value=call(),
                error=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - provider failures are data here
            return CallOutcome(
                value=None,
                error=exc,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    if mode == "sequential" or workers == 1:
        return [invoke(call) for call in calls]

    ordered: List[CallOutcome[T] | None] = [None] * len(calls)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent-hub-provider") as pool:
        futures = {pool.submit(invoke, call): index for index, call in enumerate(calls)}
        for future in as_completed(futures):
            ordered[futures[future]] = future.result()
    return [item for item in ordered if item is not None]
