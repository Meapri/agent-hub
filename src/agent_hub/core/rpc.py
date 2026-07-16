"""Shared JSON-RPC error type used by the server and provider adapters.

Carries an optional ``data`` payload so richer providers (e.g. antigravity's
modern-protocol errors) surface identical error envelopes through the unified
server. ``data`` defaults to None, so existing simple errors are unchanged.
"""

from __future__ import annotations

from typing import Any, Optional


class RpcError(ValueError):
    """JSON-RPC error carrying a numeric code and optional data."""

    def __init__(self, code: int, message: str, data: Optional[Any] = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data
