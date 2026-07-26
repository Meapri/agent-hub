from __future__ import annotations

import sqlite3

from pathlib import Path

import pytest

from agent_hub.v2.contracts import PLAN_SCHEMA, TASK_SCHEMA, validate_plan
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.store import HubStore


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

    completed = store.finalize_claim(
        run["run_id"],
        claim_token=claim.claim_token,
        expected_revision=claim.revision,
        status="completed",
        event_type="run_completed",
    )
    assert completed["status"] == "completed"
    assert completed["revision"] == 2


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

    assert migrated.health()["schema_version"] == 4
    assert list((tmp_path / "backups").glob("pre-migration-v3-to-v4-*.sqlite3"))
    connection = sqlite3.connect(path)
    try:
        retired = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'legacy_imports'"
        ).fetchone()
    finally:
        connection.close()
    assert retired is None


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
