"""Agent Hub local-first runtime.

The package exposes one daemon-backed MCP surface. Provider-specific adapters
remain private implementation details behind the worker protocol.
"""

from .contracts import (
    ARTIFACT_SCHEMA,
    EGRESS_MANIFEST_SCHEMA,
    EVENT_SCHEMA,
    PLAN_SCHEMA,
    PROVIDER_MANIFEST_SCHEMA,
    ROUTING_DECISION_SCHEMA,
    RUN_SCHEMA,
    TASK_SCHEMA,
)

PROTOCOL_VERSION = "2.0"

__all__ = [
    "ARTIFACT_SCHEMA",
    "EGRESS_MANIFEST_SCHEMA",
    "EVENT_SCHEMA",
    "PLAN_SCHEMA",
    "PROTOCOL_VERSION",
    "PROVIDER_MANIFEST_SCHEMA",
    "ROUTING_DECISION_SCHEMA",
    "RUN_SCHEMA",
    "TASK_SCHEMA",
]
