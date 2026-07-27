from __future__ import annotations

import sqlite3

from pathlib import Path

import pytest

from agent_hub.v2.contracts import PLAN_SCHEMA, TASK_SCHEMA, validate_plan
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.store import STORE_SCHEMA_VERSION, HubStore


def _plan():
    return validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Inspect the fixture.",
                "capability": "inspect",
                "inline_input": "fixture",
            },
            "steps": [
                {
                    "id": "inspect",
                    "capability": "inspect",
                    "instruction": "Inspect the fixture.",
                }
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )


def test_store_uses_wal_permissions_and_integrity(tmp_path):
    store = HubStore(tmp_path / "state" / "state.sqlite3")

    health = store.health()

    assert health["ok"] is True
    assert health["journal_mode"] == "wal"
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700


def test_global_egress_settings_default_off_and_use_revision_cas(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")

    initial = store.egress_settings()
    enabled = store.update_egress_settings(
        auto_approve=True,
        expected_revision=initial["revision"],
    )

    assert initial["auto_approve"] is False
    assert enabled["auto_approve"] is True
    assert enabled["revision"] == initial["revision"] + 1
    with pytest.raises(HubV2Error) as stale:
        store.update_egress_settings(
            auto_approve=False,
            expected_revision=initial["revision"],
        )
    assert stale.value.code == "egress_settings_revision_conflict"
    assert store.egress_settings()["auto_approve"] is True


def test_run_creation_is_idempotent_and_events_are_cursor_based(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    first = store.create_run(
        plan=_plan(),
        project_root=str(tmp_path),
        idempotency_key="fixture.create",
    )
    second = store.create_run(
        plan=_plan(),
        project_root=str(tmp_path),
        idempotency_key="fixture.create",
    )

    assert first["run_id"] == second["run_id"]
    assert first["revision"] == 0
    page = store.events(first["run_id"], project_root=str(tmp_path))
    assert [event["type"] for event in page["events"]] == ["run_created"]
    assert page["next_cursor"] > 0


def test_claim_finalize_cas_and_token_fence(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    run = store.create_run(
        plan=_plan(),
        project_root=str(tmp_path),
        idempotency_key="fixture.claim",
    )
    claim = store.claim_run(run["run_id"], expected_revision=0)

    with pytest.raises(HubV2Error) as stale:
        store.claim_run(run["run_id"], expected_revision=0)
    assert stale.value.code == "revision_conflict"

    with pytest.raises(HubV2Error) as wrong_token:
        store.finalize_claim(
            run["run_id"],
            claim_token="wrong",
            expected_revision=claim.revision,
            status="paused",
            event_type="run_paused",
        )
    assert wrong_token.value.code == "lease_lost"

    # The wave only finalizes as completed once every step is completed, so the
    # fixture has to reach that state too rather than jumping straight to it.
    finished = store.update_step(
        run["run_id"],
        step_id="inspect",
        expected_run_revision=claim.revision,
        status="completed",
    )
    completed = store.finalize_claim(
        run["run_id"],
        claim_token=claim.claim_token,
        expected_revision=finished["revision"],
        status="completed",
        event_type="run_completed",
    )
    assert completed["status"] == "completed"
    assert completed["revision"] == finished["revision"] + 1


def test_step_failure_preserves_request_checkpoint_and_provider_identity(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    run = store.create_run(
        plan=_plan(),
        project_root=str(tmp_path),
        idempotency_key="fixture.checkpoint",
    )
    claim = store.claim_run(run["run_id"], expected_revision=0)
    running = store.update_step(
        run["run_id"],
        step_id="inspect",
        expected_run_revision=claim.revision,
        status="running",
        provider="gpt",
        checkpoint={
            "phase": "provider_request_pending",
            "request_sha256": "a" * 64,
            "retry_safe": False,
        },
    )

    unknown = store.update_step(
        run["run_id"],
        step_id="inspect",
        expected_run_revision=running["revision"],
        status="outcome_unknown",
        checkpoint={"phase": "outcome_unknown", "error_code": "provider_timeout"},
    )

    step = unknown["steps"][0]
    assert step["provider"] == "gpt"
    assert step["checkpoint"]["request_sha256"] == "a" * 64
    assert step["checkpoint"]["phase"] == "outcome_unknown"


def test_claim_owner_reconciles_every_running_step_without_leaving_hangs(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Reconcile coordinator failure.",
                "capability": "chat",
                "inline_input": "",
            },
            "steps": [
                {
                    "id": "local",
                    "capability": "inspect",
                    "instruction": "Inspect locally.",
                },
                {
                    "id": "external",
                    "capability": "chat",
                    "instruction": "Call a provider.",
                },
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )
    run = store.create_run(
        plan=plan,
        project_root=str(tmp_path),
        idempotency_key="fixture.reconcile",
    )
    claim = store.claim_run(run["run_id"], expected_revision=0)
    running = store.update_step(
        run["run_id"],
        step_id="local",
        expected_run_revision=claim.revision,
        status="running",
        checkpoint={"phase": "local_step_pending", "retry_safe": True},
    )
    running = store.update_step(
        run["run_id"],
        step_id="external",
        expected_run_revision=running["revision"],
        status="running",
        checkpoint={"phase": "provider_request_pending", "retry_safe": False},
    )

    reconciled = store.reconcile_running_steps(
        run["run_id"],
        claim_token=claim.claim_token,
        expected_revision=running["revision"],
        reason_code="run_internal_error",
    )

    statuses = {step["step_id"]: step["status"] for step in reconciled["run"]["steps"]}
    assert statuses == {"local": "queued", "external": "outcome_unknown"}
    assert reconciled["requeued_step_ids"] == ["local"]
    assert reconciled["outcome_unknown_step_ids"] == ["external"]
    assert all(status != "running" for status in statuses.values())


def test_claim_renewal_extends_lease_without_changing_revision(tmp_path):
    clock = [100.0]
    store = HubStore(tmp_path / "state.sqlite3", clock=lambda: clock[0])
    run = store.create_run(
        plan=_plan(),
        project_root=str(tmp_path),
        idempotency_key="fixture.renew",
    )
    claim = store.claim_run(run["run_id"], expected_revision=0, lease_seconds=10)
    clock[0] = 105.0

    expires = store.renew_claim(
        run["run_id"],
        claim_token=claim.claim_token,
        expected_revision=claim.revision,
        lease_seconds=10,
    )

    renewed = store.get_run(run["run_id"])
    assert expires == 115.0
    assert renewed["revision"] == claim.revision
    assert renewed["lease_expires_at"] == 115.0
    assert renewed["lease_active"] is True

    clock[0] = 116.0
    assert store.get_run(run["run_id"])["lease_active"] is False


def test_expired_lease_without_dispatched_step_is_recovered_as_retryable(tmp_path):
    clock = [100.0]
    store = HubStore(tmp_path / "state.sqlite3", clock=lambda: clock[0])
    run = store.create_run(
        plan=_plan(),
        project_root=str(tmp_path),
        idempotency_key="fixture.recover",
    )
    store.claim_run(run["run_id"], expected_revision=0, lease_seconds=1)
    clock[0] = 102.0

    recovery = store.recover_expired_leases()
    recovered = store.get_run(run["run_id"])

    assert recovery["retryable_runs"] == [
        {"run_id": run["run_id"], "revision": recovered["revision"]}
    ]
    assert recovered["status"] == "queued"
    assert recovered["lease_active"] is False


def test_expired_external_step_is_fenced_as_outcome_unknown(tmp_path):
    clock = [100.0]
    store = HubStore(tmp_path / "state.sqlite3", clock=lambda: clock[0])
    run = store.create_run(
        plan=_plan(),
        project_root=str(tmp_path),
        idempotency_key="fixture.unknown",
    )
    claim = store.claim_run(run["run_id"], expected_revision=0, lease_seconds=1)
    running = store.update_step(
        run["run_id"],
        step_id="inspect",
        expected_run_revision=claim.revision,
        status="running",
        checkpoint={"phase": "provider_request_pending", "retry_safe": False},
    )
    clock[0] = 102.0

    recovery = store.recover_expired_leases()
    recovered = store.get_run(run["run_id"])

    assert recovery["outcome_unknown_runs"] == [
        {"run_id": run["run_id"], "revision": running["revision"] + 1}
    ]
    assert recovered["status"] == "outcome_unknown"
    assert recovered["steps"][0]["status"] == "outcome_unknown"


def test_store_schema_is_backed_up_and_drops_retired_import_table(tmp_path):
    path = tmp_path / "state.sqlite3"
    HubStore(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
        connection.execute(
            """
            CREATE TABLE legacy_imports (
                source_sha256 TEXT PRIMARY KEY,
                source_name TEXT NOT NULL
            )
            """
        )
        connection.execute("DROP TABLE artifact_exports")
        connection.execute("DROP TABLE provenance_edges")
        connection.execute("DROP TABLE routing_daily_aggregates")
        connection.commit()
    finally:
        connection.close()

    migrated = HubStore(path)

    assert migrated.health()["schema_version"] == STORE_SCHEMA_VERSION
    assert list(
        (tmp_path / "backups").glob(f"pre-migration-v3-to-v{STORE_SCHEMA_VERSION}-*.sqlite3")
    )
    connection = sqlite3.connect(path)
    try:
        retired = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'legacy_imports'"
        ).fetchone()
    finally:
        connection.close()
    assert retired is None


def test_store_migrates_schema_eight_global_egress_fields(tmp_path):
    path = tmp_path / "state.sqlite3"
    HubStore(path)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            ALTER TABLE egress_reviews RENAME TO egress_reviews_v9;
            CREATE TABLE egress_reviews (
                review_id TEXT PRIMARY KEY,
                proposal_sha256 TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                project_root TEXT NOT NULL,
                policy_revision INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT,
                destinations_json TEXT NOT NULL,
                entries_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                decided_at REAL,
                consumed_at REAL
            );
            DROP TABLE egress_reviews_v9;
            DROP TABLE egress_settings;
            UPDATE meta SET value = '8' WHERE key = 'schema_version';
            """
        )
        connection.commit()
    finally:
        connection.close()

    migrated = HubStore(path)
    connection = sqlite3.connect(path)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(egress_reviews)").fetchall()
        }
    finally:
        connection.close()

    assert migrated.health()["schema_version"] == STORE_SCHEMA_VERSION
    assert {"decision_source", "decision_settings_revision"} <= columns
    assert migrated.egress_settings()["auto_approve"] is False
    assert list(
        (tmp_path / "backups").glob(f"pre-migration-v8-to-v{STORE_SCHEMA_VERSION}-*.sqlite3")
    )


def test_store_records_only_egress_approval_metadata(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    digest = "a" * 64
    approval = store.record_egress_approval(
        project_root=str(tmp_path),
        manifest={
            "manifest_sha256": digest,
            "policy_revision": 0,
            "entries": [
                {
                    "path_alias": "src/module.py",
                    "sha256": "b" * 64,
                    "chars": 20,
                    "classification": "project",
                }
            ],
        },
    )

    assert approval["entries"][0]["path_alias"] == "src/module.py"
    assert "content" not in str(approval)
    assert store.get_egress_approval(digest) == approval


def test_store_summarizes_bounded_content_free_operation_metrics(tmp_path):
    clock = [1_000_000.0]
    store = HubStore(tmp_path / "state.sqlite3", clock=lambda: clock[0])
    for duration, success in ((10, True), (20, True), (30, False), (100, True)):
        store.record_operation_metric(
            operation="agent_hub_get",
            success=success,
            duration_ms=duration,
        )

    metrics = store.operation_metrics()

    assert metrics["content_recorded"] is False
    assert metrics["operations"]["agent_hub_get"] == {
        "count": 4,
        "success_rate": 0.75,
        "latency_ms": {"p50": 20, "p95": 100, "max": 100},
        "failures": {
            "count": 1,
            "unrecorded": 0,
            "top_codes": [{"code": "unclassified_error", "count": 1}],
            "distinct_codes": 1,
        },
    }


def test_operation_metrics_rank_failure_codes_without_free_text(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    recorded = (
        (False, "egress_approval_required"),
        (False, "egress_approval_required"),
        (False, "provider_timeout"),
        (False, "Traceback (most recent call last): secret-token"),
        # A trailing newline must not sneak past the taxonomy check, and provider
        # codes reach the store unvalidated, so this is a reachable input.
        (False, "provider_timeout\n"),
        (False, "인증실패"),
        (False, None),
        (True, None),
    )
    for success, code in recorded:
        store.record_operation_metric(
            operation="agent_hub_plan",
            success=success,
            duration_ms=5,
            error_code=code,
        )

    failures = store.operation_metrics()["operations"]["agent_hub_plan"]["failures"]

    assert failures["count"] == 7
    assert failures["top_codes"] == [
        {"code": "unclassified_error", "count": 4},
        {"code": "egress_approval_required", "count": 2},
        {"code": "provider_timeout", "count": 1},
    ]
    # Every failure carries a code, so NULL can only mean a pre-schema-10 row.
    assert failures["unrecorded"] == 0
    assert all("secret-token" not in item["code"] for item in failures["top_codes"])
    assert all(item["code"].isascii() for item in failures["top_codes"])


def test_operation_metrics_report_pre_schema_10_rows_as_unrecorded(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    store.record_operation_metric(
        operation="agent_hub_plan",
        success=False,
        duration_ms=5,
        error_code="provider_timeout",
    )
    # Simulate a row written before schema 10, which is the only way a failure
    # row can legitimately carry a NULL code.
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            "INSERT INTO operation_metrics(operation, success, duration_ms, recorded_at)"
            " VALUES('agent_hub_plan', 0, 5, 1.0)"
        )
        connection.commit()
    finally:
        connection.close()

    failures = store.operation_metrics()["operations"]["agent_hub_plan"]["failures"]

    assert failures["count"] == 2
    assert failures["unrecorded"] == 1
    assert failures["top_codes"] == [{"code": "provider_timeout", "count": 1}]


def test_operation_metrics_ignore_error_code_on_success(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    store.record_operation_metric(
        operation="agent_hub_get",
        success=True,
        duration_ms=1,
        error_code="provider_timeout",
    )

    failures = store.operation_metrics()["operations"]["agent_hub_get"]["failures"]

    assert failures == {"count": 0, "unrecorded": 0, "top_codes": [], "distinct_codes": 0}


def test_events_drop_prompt_and_unknown_details(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    run = store.create_run(
        plan=_plan(),
        project_root=str(tmp_path),
        idempotency_key="fixture.redaction",
    )
    claim = store.claim_run(run["run_id"], expected_revision=0)
    store.finalize_claim(
        run["run_id"],
        claim_token=claim.claim_token,
        expected_revision=claim.revision,
        status="paused",
        event_type="run_paused",
        details={
            "prompt": "secret prompt",
            "raw_exception": "secret exception",
            "reason_code": "provider_timeout",
        },
    )

    page = store.events(run["run_id"])
    assert page["events"][-1]["details"] == {"reason_code": "provider_timeout"}


def test_durable_artifact_requires_encrypted_content(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")

    with pytest.raises(HubV2Error) as error:
        store.put_artifact(
            content=b"plain",
            media_type="text/plain",
            sensitivity="project",
            encrypted=False,
        )
    assert error.value.code == "artifact_encryption_required"

    artifact = store.put_artifact(
        content=b"ciphertext",
        media_type="text/plain",
        sensitivity="project",
        encrypted=True,
        content_sha256="a" * 64,
    )
    stored = store.get_artifact(artifact["artifact_id"], include_content=True)
    assert stored["content"] == b"ciphertext"
    assert stored["content_sha256"] == "a" * 64


def test_expired_artifact_prunes_content_but_preserves_provenance(tmp_path):
    clock = [100.0]
    store = HubStore(tmp_path / "state.sqlite3", clock=lambda: clock[0])
    source = store.put_artifact(
        content=b"encrypted source",
        media_type="text/plain",
        sensitivity="project",
        encrypted=True,
        delete_after=101.0,
    )
    derived = store.put_artifact(
        content=b"encrypted derived",
        media_type="text/plain",
        sensitivity="project",
        encrypted=True,
        source_refs=[source["artifact_id"]],
    )
    clock[0] = 102.0

    result = store.prune_expired_artifacts()
    tombstone = store.get_artifact(source["artifact_id"], include_content=True)
    child = store.get_artifact(derived["artifact_id"])

    assert result["pruned_content_count"] == 1
    assert result["deleted_metadata_count"] == 0
    assert tombstone["content"] is None
    assert tombstone["content_sha256"] == source["content_sha256"]
    assert tombstone["verification"]["content_pruned"] is True
    assert tombstone["retention"] == "metadata_only"
    assert child["provenance"]["sources"] == [source["artifact_id"]]


def test_feedback_is_revision_fenced_and_weighted(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    run = store.create_run(
        plan=_plan(),
        project_root=str(tmp_path),
        idempotency_key="fixture.feedback",
    )

    feedback = store.record_feedback(
        run_id=run["run_id"],
        expected_revision=0,
        outcome="accepted",
        rating=5,
    )

    assert feedback["signal_weight"] == 5.0
    with pytest.raises(HubV2Error) as duplicate:
        store.record_feedback(
            run_id=run["run_id"],
            expected_revision=0,
            outcome="accepted",
            rating=5,
        )
    assert duplicate.value.code == "feedback_conflict"


def test_backup_is_new_private_sqlite_file(tmp_path):
    store = HubStore(tmp_path / "state" / "state.sqlite3")
    destination = tmp_path / "backup" / "copy.sqlite3"

    result = store.backup(destination)

    backup = Path(result["path"])
    assert backup.exists()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert len(result["sha256"]) == 64


def _budgeted_plan(max_total_tokens: int):
    return validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Inspect the fixture.",
                "capability": "inspect",
                "inline_input": "fixture",
                "constraints": {"max_total_tokens": max_total_tokens},
            },
            "steps": [
                {
                    "id": "inspect",
                    "capability": "inspect",
                    "instruction": "Inspect the fixture.",
                }
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )


def test_run_denormalizes_plan_token_budget_and_accumulates_step_usage(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    run = store.create_run(
        plan=_budgeted_plan(1_000),
        project_root=str(tmp_path),
        idempotency_key="fixture.tokens",
    )

    assert run["token_usage"]["max_total_tokens"] == 1_000
    assert run["token_usage"]["total_tokens"] == 0
    assert run["token_usage"]["exhausted"] is False

    claim = store.claim_run(run["run_id"], expected_revision=run["revision"])
    updated = store.update_step(
        run["run_id"],
        step_id="inspect",
        expected_run_revision=claim.revision,
        status="running",
        token_usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "source": "reported",
        },
    )
    # A second attempt bills again, so the ledger sums attempts instead of replacing.
    updated = store.update_step(
        run["run_id"],
        step_id="inspect",
        expected_run_revision=updated["revision"],
        status="completed",
        token_usage={
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "source": "reported",
        },
    )

    step = updated["steps"][0]
    assert (step["input_tokens"], step["output_tokens"], step["total_tokens"]) == (110, 55, 165)
    assert step["tokens_source"] == "reported"
    assert updated["token_usage"]["total_tokens"] == 165
    assert updated["token_usage"]["remaining_tokens"] == 835


def test_exhausted_run_is_resumable_through_a_token_budget_grant(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    run = store.create_run(
        plan=_budgeted_plan(100),
        project_root=str(tmp_path),
        idempotency_key="fixture.grant",
    )
    claim = store.claim_run(run["run_id"], expected_revision=run["revision"])
    updated = store.update_step(
        run["run_id"],
        step_id="inspect",
        expected_run_revision=claim.revision,
        status="failed",
        checkpoint={"retry_safe": True, "error_code": "fallback_exhausted"},
        token_usage={
            "input_tokens": 80,
            "output_tokens": 40,
            "total_tokens": 120,
            "source": "reported",
        },
    )
    paused = store.finalize_claim(
        run["run_id"],
        claim_token=claim.claim_token,
        expected_revision=updated["revision"],
        status="paused",
        event_type="run_paused",
        details={"reason_code": "wave_budget"},
    )

    assert paused["token_usage"]["exhausted"] is True
    action = paused["next_action"]
    assert action["tool"] == "agent_hub_continue"
    assert action["arguments"]["token_budget_grant"] == 100
    assert action["arguments"]["retry_failed_steps"] == ["inspect"]

    granted = store.grant_token_budget(
        paused["run_id"],
        expected_revision=paused["revision"],
        additional_tokens=action["arguments"]["token_budget_grant"],
    )

    assert granted["token_usage"]["granted_tokens"] == 100
    assert granted["token_usage"]["max_total_tokens"] == 200
    assert granted["token_usage"]["exhausted"] is False
    # Status stays paused so a following requeue still passes its own fencing.
    assert granted["status"] == "paused"
    requeued = store.requeue_failed_steps(
        granted["run_id"],
        expected_revision=granted["revision"],
        step_ids=["inspect"],
    )
    assert requeued["status"] == "queued"
    # Already-spent tokens survive the requeue.
    assert requeued["token_usage"]["total_tokens"] == 120


def test_token_budget_grant_is_revision_fenced_and_rejects_claimed_runs(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    run = store.create_run(
        plan=_budgeted_plan(100),
        project_root=str(tmp_path),
        idempotency_key="fixture.grant.fence",
    )

    with pytest.raises(HubV2Error) as stale:
        store.grant_token_budget(run["run_id"], expected_revision=99, additional_tokens=10)
    assert stale.value.code == "revision_conflict"

    with pytest.raises(HubV2Error) as zero:
        store.grant_token_budget(
            run["run_id"], expected_revision=run["revision"], additional_tokens=0
        )
    assert zero.value.code == "invalid_request"

    claim = store.claim_run(run["run_id"], expected_revision=run["revision"])
    with pytest.raises(HubV2Error) as claimed:
        store.grant_token_budget(
            run["run_id"], expected_revision=claim.revision, additional_tokens=10
        )
    assert claimed.value.code == "token_grant_not_allowed"


def test_capability_token_estimate_needs_enough_samples(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    context = {"capability": "write", "intent_sha256": "a" * 64}

    empty = store.capability_token_estimate(capability="write", provider="claude")
    assert empty["median_total_tokens"] is None
    assert empty["source"] == "insufficient_samples"

    for total in (100, 300, 200):
        store.record_routing_sample(
            context=context,
            provider="claude",
            model="m",
            capability="write",
            success=True,
            quality=None,
            latency_ms=10,
            total_tokens=total,
            signal_weight=1.0,
        )

    estimate = store.capability_token_estimate(capability="write", provider="claude")
    assert estimate["sample_count"] == 3
    assert estimate["median_total_tokens"] == 200
    assert estimate["source"] == "routing_samples_provider"
