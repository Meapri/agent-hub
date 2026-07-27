"""The compact 14-tool Agent Hub v2 MCP surface."""

from __future__ import annotations

from typing import Any

TOOL_NAMES = (
    "agent_hub_status",
    "agent_hub_catalog",
    "agent_hub_execute",
    "agent_hub_plan",
    "agent_hub_start",
    "agent_hub_continue",
    "agent_hub_get",
    "agent_hub_events",
    "agent_hub_cancel",
    "agent_hub_artifact",
    "agent_hub_feedback",
    "agent_hub_policy",
    "agent_hub_handoff",
    "agent_hub_doctor",
)


def _object(
    properties: dict[str, Any] | None = None,
    *,
    required: list[str] | None = None,
    additional: bool = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": additional,
    }
    if required:
        schema["required"] = required
    return schema


def tool_definitions() -> list[dict[str, Any]]:
    text = {"type": "string"}
    integer = {"type": "integer", "minimum": 0}
    boolean = {"type": "boolean"}
    definitions = [
        (
            "agent_hub_status",
            "Show the v2 daemon, store, provider manifest, and readiness state.",
            _object({"probe": boolean}),
        ),
        (
            "agent_hub_catalog",
            "List v2 provider, model, capability, catalog, and generation states.",
            _object(
                {
                    "provider": text,
                    "model": text,
                    "capability": text,
                    "refresh": boolean,
                }
            ),
        ),
        (
            "agent_hub_execute",
            "Execute one short task without creating a durable run by default.",
            _object(
                {
                    "task": {"type": "object"},
                    "provider": text,
                    "model": text,
                    "record": boolean,
                    "project_root": text,
                },
                required=["task", "project_root"],
            ),
        ),
        (
            "agent_hub_plan",
            "Prepare local egress or apply an approved proposal through a planner.",
            _object(
                {
                    "mode": {"enum": ["prepare", "apply"]},
                    "task": {"type": "object"},
                    "project_root": text,
                    "provider": text,
                    "model": text,
                    "source_paths": {"type": "array", "items": text},
                    "proposal": {"type": "object"},
                    "proposal_sha256": text,
                    "expected_policy_revision": integer,
                    "approval_request_id": text,
                },
                required=["mode", "task", "project_root"],
            ),
        ),
        (
            "agent_hub_start",
            "Create a revision-fenced durable run from an approved v2 plan.",
            _object(
                {
                    "plan": {"type": "object"},
                    "project_root": text,
                    "idempotency_key": text,
                },
                required=["plan", "project_root", "idempotency_key"],
            ),
        ),
        (
            "agent_hub_continue",
            "Claim a run revision, optionally grant more token budget and requeue explicitly retry-safe failed steps, then execute the next dependency-ready wave. Never retry outcome_unknown steps.",
            _object(
                {
                    "run_id": text,
                    "expected_revision": integer,
                    "max_waves": {"type": "integer", "minimum": 1, "maximum": 8},
                    "retry_failed_steps": {
                        "type": "array",
                        "items": text,
                        "maxItems": 64,
                        "description": "Paused failed step IDs listed by agent_hub_get.retryable_failed_steps. The store rejects ambiguous or unsafe retries.",
                    },
                    "token_budget_grant": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Extra tokens for a run paused by run_token_budget_exhausted. The sealed plan limit is unchanged; this is added on top. agent_hub_get.next_action suggests an amount.",
                    },
                },
                required=["run_id", "expected_revision"],
            ),
        ),
        (
            "agent_hub_get",
            "Read a durable run and its step checkpoints.",
            _object({"run_id": text, "project_root": text}, required=["run_id"]),
        ),
        (
            "agent_hub_events",
            "Read a bounded cursor page of redacted run events.",
            _object(
                {
                    "run_id": text,
                    "after_cursor": integer,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "project_root": text,
                },
                required=["run_id"],
            ),
        ),
        (
            "agent_hub_cancel",
            "Cancel a run, or adjudicate an outcome_unknown run through a digest-fenced human reconciliation. Only the not_delivered verdict can re-send an external request, and it requires an explicit confirmation phrase.",
            _object(
                {
                    "run_id": text,
                    "expected_revision": integer,
                    "action": {
                        "enum": ["cancel", "prepare_reconcile", "apply_reconcile"],
                        "description": "Defaults to cancel.",
                    },
                    "resolutions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "items": _object(
                            {
                                "step_id": text,
                                "verdict": {
                                    "enum": [
                                        "not_delivered",
                                        "delivered_discarded",
                                        "delivered_recovered",
                                    ],
                                    "description": "not_delivered re-sends the external request; delivered_discarded closes the step as failed; delivered_recovered accepts a result the human supplies.",
                                },
                                "result_text": text,
                            },
                            required=["step_id", "verdict"],
                        ),
                    },
                    "run_disposition": {"enum": ["resume", "fail"]},
                    "proposal": {"type": "object"},
                    "proposal_sha256": text,
                    "confirmation_phrase": text,
                },
                required=["run_id", "expected_revision"],
            ),
        ),
        (
            "agent_hub_artifact",
            "Get, verify, export, or apply retention to an encrypted artifact.",
            _object(
                {
                    "action": {
                        "enum": [
                            "get",
                            "verify",
                            "prepare_export",
                            "apply_export",
                            "prune",
                        ]
                    },
                    "artifact_id": text,
                    "include_text": boolean,
                    "project_root": text,
                    "destination": text,
                    "proposal": {"type": "object"},
                    "proposal_sha256": text,
                },
                required=["action"],
            ),
        ),
        (
            "agent_hub_feedback",
            "Record a revision-fenced quality or acceptance signal.",
            _object(
                {
                    "run_id": text,
                    "step_id": text,
                    "expected_revision": integer,
                    "outcome": {"enum": ["accepted", "partial", "rejected", "verified", "failed"]},
                    "rating": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                required=["run_id", "expected_revision", "outcome"],
            ),
        ),
        (
            "agent_hub_policy",
            "Get or digest-fence a project policy update, or the user-global routing prior.",
            _object(
                {
                    "action": {"enum": ["get", "prepare_update", "apply_update"]},
                    "project_root": text,
                    "target": {
                        "enum": ["policy"],
                        "description": "Only the project policy is editable.",
                    },
                    "patch": {"type": "object"},
                    "expected_revision": integer,
                    "proposal": {"type": "object"},
                    "proposal_sha256": text,
                },
                required=["action", "project_root"],
            ),
        ),
        (
            "agent_hub_handoff",
            "Use the project HANDOFF prepare/apply and takeover boundary, or read its applied history and section-level diffs.",
            _object(
                {
                    "action": {
                        "enum": [
                            "get",
                            "prepare_update",
                            "apply_update",
                            "takeover",
                            "history",
                            "diff",
                        ],
                        "description": "history and diff are read-only. diff without target_sequence compares a snapshot against the working file, which reveals edits made outside Agent Hub.",
                    },
                    "project_root": text,
                    "arguments": {"type": "object"},
                },
                required=["action", "project_root"],
            ),
        ),
        (
            "agent_hub_doctor",
            "Run read-only v2 diagnostics and return a repair plan when requested.",
            _object(
                {
                    "project_root": text,
                    "live": boolean,
                    "repair": {"enum": ["none", "prepare"]},
                },
                required=["project_root"],
            ),
        ),
    ]
    return [
        {"name": name, "description": description, "inputSchema": schema}
        for name, description, schema in definitions
    ]
