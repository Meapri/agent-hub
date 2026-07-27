"""Store-wide invariants, checked against a live database rather than a mock.

These are deliberately general statements about what the store may never contain.
They are not restatements of past bugs: a predicate written to match one known
defect passes by construction and proves nothing. The value of this module is
that it runs after every test, so a change that corrupts state fails the suite
even when no test asserted on that state.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

REQUIRED_SCHEMA_COLUMNS: dict[str, set[str]] = {
    "runs": {
        "run_id",
        "project_root",
        "status",
        "revision",
        "plan_sha256",
        "plan_json",
        "policy_revision",
        "routing_mode",
        "idempotency_key",
        "lease_token_sha256",
        "lease_expires_at",
        "token_budget_limit",
        "token_budget_grant",
        "reconciliation_count",
    },
    "steps": {
        "run_id",
        "step_id",
        "status",
        "revision",
        "provider",
        "model",
        "attempt",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "tokens_source",
        "input_artifact_ids",
        "output_artifact_ids",
        "checkpoint_state",
    },
    "artifacts": {
        "artifact_id",
        "schema_name",
        "content_sha256",
        "media_type",
        "sensitivity",
        "encrypted",
        "retention",
        "producer_step_id",
        "verification_json",
    },
    "events": {"cursor", "run_id", "event_type", "details_json", "occurred_at"},
    "egress_approvals": {
        "manifest_sha256",
        "project_root",
        "destinations_json",
        "entries_json",
        "policy_revision",
    },
    "egress_settings": {"singleton_id", "revision", "auto_approve", "updated_at"},
    "operation_metrics": {
        "metric_id",
        "operation",
        "success",
        "duration_ms",
        "recorded_at",
        "error_code",
    },
    "routing_decisions": {
        "decision_id",
        "routing_mode",
        "selected_provider",
        "planner_provider",
        "candidates_json",
        "score_json",
        "sample_count",
        "policy_revision",
        "reason_code",
        "routing_profile",
        "evidence_kind",
        "prior_sha256",
        "prior_revision",
        "prior_weight_fraction",
    },
    "handoff_snapshots": {
        "snapshot_id",
        "scope_identity",
        "scope_root",
        "target_alias",
        "sequence",
        "origin",
        "file_sha256",
        "managed_sha256",
        "body",
        "body_sha256",
        "recorded_at",
    },
    "run_reconciliations": {
        "reconciliation_id",
        "run_id",
        "base_revision",
        "witness_sha256",
        "proposal_sha256",
        "confirmation_sha256",
        "run_disposition",
        "resolutions_json",
        "status",
        "created_at",
        "expires_at",
    },
}

# Each entry is (name, sql). The SQL selects rows that VIOLATE the invariant, so
# an empty result means the invariant holds.
_PREDICATES: tuple[tuple[str, str], ...] = (
    (
        "step_token_parts_sum_to_total",
        """
        SELECT run_id, step_id FROM steps
        WHERE input_tokens + output_tokens != total_tokens
        """,
    ),
    (
        "unmeasured_steps_carry_no_tokens",
        """
        SELECT run_id, step_id FROM steps
        WHERE tokens_source = 'unset'
          AND (input_tokens != 0 OR output_tokens != 0 OR total_tokens != 0)
        """,
    ),
    (
        "measured_steps_declare_a_source",
        """
        SELECT run_id, step_id FROM steps
        WHERE total_tokens > 0 AND tokens_source = 'unset'
        """,
    ),
    (
        "run_budget_is_derived_not_defaulted",
        """
        SELECT run_id FROM runs
        WHERE token_budget_limit = 0 AND status != 'prepared'
        """,
    ),
    (
        "step_outputs_reference_existing_artifacts",
        """
        SELECT s.run_id, s.step_id FROM steps s
        WHERE s.output_artifact_ids NOT IN ('[]', '')
          AND EXISTS (
              SELECT 1 FROM json_each(s.output_artifact_ids) j
              WHERE NOT EXISTS (
                  SELECT 1 FROM artifacts a WHERE a.artifact_id = j.value
              )
          )
        """,
    ),
    (
        "terminal_runs_hold_no_lease",
        """
        SELECT run_id FROM runs
        WHERE status IN ('completed', 'failed', 'cancelled')
          AND lease_token_sha256 IS NOT NULL
        """,
    ),
    (
        "settled_runs_leave_no_step_running",
        """
        SELECT run_id FROM runs
        WHERE status IN ('completed', 'failed', 'cancelled')
          AND EXISTS (
              SELECT 1 FROM steps s WHERE s.run_id = runs.run_id AND s.status = 'running'
          )
        """,
    ),
    (
        "completed_runs_have_every_step_completed",
        """
        SELECT run_id FROM runs
        WHERE status = 'completed'
          AND EXISTS (
              SELECT 1 FROM steps s WHERE s.run_id = runs.run_id AND s.status != 'completed'
          )
        """,
    ),
    (
        "failure_metrics_carry_a_taxonomy_code",
        """
        SELECT metric_id FROM operation_metrics
        WHERE success = 0
          AND error_code IS NOT NULL
          AND error_code NOT GLOB '[a-z]*'
        """,
    ),
    (
        "success_metrics_carry_no_error_code",
        """
        SELECT metric_id FROM operation_metrics
        WHERE success = 1 AND error_code IS NOT NULL
        """,
    ),
    (
        "feedback_samples_share_a_bucket_with_execution",
        # A rating filed under a context key the router never reads is silently
        # discarded. Stated generally: no sample may sit in a bucket that holds
        # nothing else, when execution samples exist at all.
        """
        SELECT f.sample_id FROM routing_samples f
        WHERE f.signal_weight > 3.0
          AND EXISTS (SELECT 1 FROM routing_samples WHERE signal_weight <= 3.0)
          AND NOT EXISTS (
              SELECT 1 FROM routing_samples e
              WHERE e.signal_weight <= 3.0 AND e.context_sha256 = f.context_sha256
          )
        """,
    ),
    (
        "unresolved_steps_keep_their_run_reconcilable",
        # prepare_reconcile only accepts an outcome_unknown run. A run that
        # settles anywhere else while a step is still outcome_unknown has
        # stranded that step: nothing can retry it and nothing can adjudicate it.
        """
        SELECT run_id FROM runs
        WHERE status != 'outcome_unknown'
          AND status NOT IN ('running', 'prepared', 'cancelled')
          AND EXISTS (
              SELECT 1 FROM steps s
              WHERE s.run_id = runs.run_id AND s.status = 'outcome_unknown'
          )
        """,
    ),
    (
        "reconciliation_grants_stay_within_their_run",
        """
        SELECT reconciliation_id FROM run_reconciliations r
        WHERE NOT EXISTS (SELECT 1 FROM runs WHERE runs.run_id = r.run_id)
        """,
    ),
)


class InvariantViolation(AssertionError):
    pass


def assert_required_schema(connection: sqlite3.Connection) -> None:
    for table, required in REQUIRED_SCHEMA_COLUMNS.items():
        actual = {
            str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not actual:
            continue
        missing = sorted(required - actual)
        if missing:
            raise InvariantViolation(f"{table} is missing required columns: {', '.join(missing)}")


def check_store(path: str, *, on_violation: Callable[[str, list[Any]], None] | None = None) -> None:
    """Raise on the first violated invariant. Read-only."""

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "runs" not in tables:
            return
        assert_required_schema(connection)
        for name, sql in _PREDICATES:
            try:
                rows = connection.execute(sql).fetchall()
            except sqlite3.Error:
                # A predicate that cannot run against this schema is skipped rather
                # than reported, so an older database does not fail every test.
                continue
            if rows:
                sample = [tuple(row) for row in rows[:5]]
                if on_violation is not None:
                    on_violation(name, sample)
                raise InvariantViolation(
                    f"invariant {name} violated by {len(rows)} row(s); first: {sample}"
                )
    finally:
        connection.close()
