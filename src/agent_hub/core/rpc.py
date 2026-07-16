"""Shared JSON-RPC error type used by the server and provider adapters."""

from __future__ import annotations


class RpcError(ValueError):
    """JSON-RPC error carrying a numeric code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
