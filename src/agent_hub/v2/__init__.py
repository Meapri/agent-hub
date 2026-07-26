"""Agent Hub v2 local-first runtime.

The v2 package is intentionally isolated from the v1 public operation module.
It can be exercised and migrated incrementally without changing the installed
v1 MCP surface until the daemon and bridge pass their cutover gates.
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
