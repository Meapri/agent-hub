"""Safe public errors for the v2 daemon and MCP bridge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import failure_classes


@dataclass
class HubV2Error(Exception):
    code: str
    message: str
    scope: str = "runtime"
    retryable: bool = False
    safe_details: Mapping[str, Any] | None = None
    next_action: Mapping[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        # Callers act on this flag, so it has to agree with the taxonomy that
        # governs the runtime's own retries. Where the table has an opinion it
        # wins; codes outside it (setup, release, daemon) keep what they declared.
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "scope": self.scope,
            "retryable": (
                failure_classes.is_retryable(self.code)
                if failure_classes.is_known(self.code)
                else self.retryable
            ),
            "safe_details": dict(self.safe_details or {}),
        }
        if self.next_action:
            payload["next_action"] = dict(self.next_action)
        return payload


def public_failure(
    error: HubV2Error,
    *,
    operation: str,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "operation": operation,
        "error": error.public(),
        "data": dict(data or {}),
    }


def safe_unexpected_error(*, operation: str, scope: str = "runtime") -> dict[str, Any]:
    """Never expose an exception string across the local protocol boundary."""

    return public_failure(
        HubV2Error(
            code="internal_error",
            message="Agent Hub could not complete the operation.",
            scope=scope,
            retryable=False,
        ),
        operation=operation,
    )
