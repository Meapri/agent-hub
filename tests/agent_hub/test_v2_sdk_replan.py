from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.v2.contracts import PLAN_SCHEMA, TASK_SCHEMA, validate_plan
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.sdk import (
    AuthStub,
    MockTransport,
    TimeoutCancelFixture,
    approve_provider_registration,
    check_provider,
    load_workflow,
    prepare_provider_registration,
    scan_redaction,
)
from agent_hub.v2.store import HubStore


def _plan(two_steps=False):
    steps = [
        {
            "id": "one",
            "capability": "chat",
            "instruction": "One.",
        }
    ]
    if two_steps:
        steps.append(
            {
                "id": "two",
                "capability": "review",
                "depends_on": ["one"],
                "instruction": "Two.",
            }
        )
    return validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Fixture.",
                "capability": "chat",
                "inline_input": "",
            },
            "steps": steps,
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )


def test_provider_conformance_rejects_secret_shaped_fields():
    def request(method, params):
        if method == "initialize":
            return {
                "manifest": {
                    "schema": "agent_hub_provider_v2",
                    "provider_id": "fixture",
                    "adapter_version": "1",
                    "protocol_version": "2.0",
                    "capabilities": ["chat"],
                    "auth_owner": "fixture",
                    "auth_mode": "none",
                    "allowed_domains": [],
                }
            }
        if method == "status":
            return {"access_token": "must never escape"}
        return {"models": [{"id": "fixture-public"}]}

    report = check_provider("fixture", request)

    assert report["passed"] is False
    assert next(
        check for check in report["checks"] if check["name"] == "status_redaction"
    )["reason_code"] == "secret_field_exposed"


def test_builtin_workflow_packages_are_valid():
    root = Path(__file__).parents[2] / "src/agent_hub/v2/workflows"

    workflows = [load_workflow(path) for path in sorted(root.glob("*.json"))]

    assert [item["id"] for item in workflows] == [
        "code-review",
        "decision",
        "document-write",
        "inspect",
        "release",
    ]


def test_provider_sdk_fixtures_and_digest_lock():
    transport = MockTransport({"status": {"ready": True}})
    assert transport.request("status", {})["ready"] is True
    assert transport.calls[0]["method"] == "status"
    assert AuthStub().status()["credential_exposed"] is False
    fixture = TimeoutCancelFixture()
    assert fixture.cancel() is True
    fixture.invoke(0.01)
    assert scan_redaction({"nested": {"api_key": "private"}})["passed"] is False

    proposal = prepare_provider_registration(
        {
            "schema": "agent_hub_provider_v2",
            "provider_id": "fixture",
            "adapter_version": "1.0.0",
            "protocol_version": "2.0",
            "capabilities": ["chat"],
            "reasoning_effort": [],
            "auth_owner": "fixture",
            "auth_mode": "stub",
            "allowed_domains": ["example.com"],
            "supports_cancel": True,
            "supports_streaming": False,
            "supports_idempotency": True,
            "settings_schema": {"type": "object"},
        },
        package_sha256="a" * 64,
    )
    lock = approve_provider_registration(
        proposal,
        proposal_sha256=proposal["proposal_sha256"],
    )
    assert lock["approved"] is True


def test_replan_preserves_completed_steps_and_replaces_only_pending(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    run = store.create_run(
        plan=_plan(two_steps=True),
        project_root=str(tmp_path),
        idempotency_key="fixture.replan",
    )
    claim = store.claim_run(run["run_id"], expected_revision=0)
    updated = store.update_step(
        run["run_id"],
        step_id="one",
        expected_run_revision=claim.revision,
        status="completed",
    )
    paused = store.finalize_claim(
        run["run_id"],
        claim_token=claim.claim_token,
        expected_revision=updated["revision"],
        status="paused",
        event_type="run_paused",
    )
    candidate = _plan(two_steps=True)
    candidate["steps"][1]["instruction"] = "Replacement pending step."
    candidate.pop("plan_sha256")
    candidate = validate_plan(candidate)

    replanned = store.replace_pending_plan(
        run["run_id"],
        expected_revision=paused["revision"],
        candidate_plan=candidate,
        reason_code="deterministic_verification_failed",
    )

    assert replanned["replan_count"] == 1
    assert replanned["steps"][0]["status"] == "completed"
    assert replanned["steps"][1]["status"] == "queued"

    with pytest.raises(HubV2Error) as exhausted:
        store.replace_pending_plan(
            run["run_id"],
            expected_revision=replanned["revision"],
            candidate_plan=candidate,
            reason_code="fallback_exhausted",
        )
    assert exhausted.value.code == "replan_budget_exhausted"
