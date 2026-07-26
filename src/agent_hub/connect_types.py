"""Shared public types for the local provider connection GUI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
from typing import Any, Dict


class ConnectionError(RuntimeError):
    """A provider connection action could not be completed safely."""

    def __init__(self, message: str, *, code: str = "connection_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ConnectionJob:
    id: str
    provider: str
    kind: str
    state: str = "pending"
    message: str = ""
    action_url: str | None = None
    user_code: str | None = None
    requires_code: bool = False
    fallback_command: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def public(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("created_at", None)
        data.pop("updated_at", None)
        return data
