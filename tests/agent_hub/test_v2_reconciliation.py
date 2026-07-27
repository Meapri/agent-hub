from __future__ import annotations

import secrets

import pytest

from agent_hub.v2.contracts import PLAN_SCHEMA, TASK_SCHEMA, validate_plan
from agent_hub.v2.crypto import ArtifactCipher, StaticKeyProvider
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.service import HubService
from agent_hub.v2.store import MAX_RUN_RECONCILIATIONS, HubStore


def _plan():
    return validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Write and review the fixture.",
                "capability": "chat",
                "inline_input": "x",
                "retention": "durable_private",
            },
            "steps": [
                {
                    "id": "draft",
                    "capability": "chat",
                    "instruction": "Draft it.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
                {
                    "id": "review",
                    "capability": "review",
                    "instruction": "Review it.",
                    "depends_on": ["draft"],
                    "routing_requirements": {"planner_provider": "gpt"},
                },
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )


def _service(tmp_path) -> HubService:
    return HubService(
        HubStore(tmp_path / "state.sqlite3"),
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )


def _stuck_run(service: HubService, tmp_path):
    """Drive a run into outcome_unknown with one ambiguous external step."""

    run = service.store.create_run(
        plan=_plan(),
        project_root=str(tmp_path),
        idempotency_key=f"rec.{secrets.token_hex(4)}",
    )
    claim = service.store.claim_run(run["run_id"], expected_revision=run["revision"])
    updated = service.store.update_step(
        run["run_id"],
        step_id="draft",
        expected_run_revision=claim.revision,
        status="outcome_unknown",
        provider="gpt",
        checkpoint={
            "phase": "outcome_unknown",
            "retry_safe": False,
            "error_code": "provider_timeout",
            "request_sha256": "a" * 64,
        },
    )
    return service.store.finalize_claim(
        run["run_id"],
        claim_token=claim.claim_token,
        expected_revision=updated["revision"],
        status="outcome_unknown",
        event_type="run_paused",
        details={"reason_code": "outcome_unknown"},
    )


def test_get_offers_only_the_non_resending_verdict_as_next_action(tmp_path):
    service = _service(tmp_path)
    stuck = _stuck_run(service, tmp_path)

    assert stuck["outcome_unknown_steps"] == ["draft"]
    action = stuck["next_action"]

    assert action["tool"] == "agent_hub_cancel"
    assert action["arguments"]["action"] == "prepare_reconcile"
    # The safety property: executing next_action verbatim can never re-send.
    assert action["arguments"]["run_disposition"] == "fail"
    assert [item["verdict"] for item in action["arguments"]["resolutions"]] == [
        "delivered_discarded"
    ]


def test_resend_verdict_is_announced_by_the_confirmation_phrase(tmp_path):
    service = _service(tmp_path)
    stuck = _stuck_run(service, tmp_path)

    resend = service.dispatch(
        "agent_hub_cancel",
        {
            "action": "prepare_reconcile",
            "run_id": stuck["run_id"],
            "expected_revision": stuck["revision"],
            "resolutions": [{"step_id": "draft", "verdict": "not_delivered"}],
            "run_disposition": "resume",
        },
    )["data"]
    discard = service.dispatch(
        "agent_hub_cancel",
        {
            "action": "prepare_reconcile",
            "run_id": stuck["run_id"],
            "expected_revision": stuck["revision"],
            "resolutions": [{"step_id": "draft", "verdict": "delivered_discarded"}],
            "run_disposition": "fail",
        },
    )["data"]

    assert resend["confirmation_phrase"].startswith("resend-")
    assert resend["resend_requested"] is True
    assert discard["confirmation_phrase"].startswith("discard-")
    assert discard["resend_requested"] is False


def test_not_delivered_requeues_the_step_and_resumes_the_run(tmp_path):
    service = _service(tmp_path)
    stuck = _stuck_run(service, tmp_path)
    proposal = service.dispatch(
        "agent_hub_cancel",
        {
            "action": "prepare_reconcile",
            "run_id": stuck["run_id"],
            "expected_revision": stuck["revision"],
            "resolutions": [{"step_id": "draft", "verdict": "not_delivered"}],
            "run_disposition": "resume",
        },
    )["data"]

    applied = service.dispatch(
        "agent_hub_cancel",
        {
            "action": "apply_reconcile",
            "run_id": stuck["run_id"],
            "expected_revision": stuck["revision"],
            "resolutions": [{"step_id": "draft", "verdict": "not_delivered"}],
            "run_disposition": "resume",
            "proposal": {
                key: value
                for key, value in proposal.items()
                if key not in {"confirmation_phrase", "confirmation_prompt"}
            },
            "proposal_sha256": proposal["proposal_sha256"],
            "confirmation_phrase": proposal["confirmation_phrase"],
        },
    )

    assert applied["success"] is True
    run = applied["data"]
    assert run["status"] == "queued"
    assert run["outcome_unknown_steps"] == []
    assert run["reconciliation_count"] == 1
    draft = next(step for step in run["steps"] if step["step_id"] == "draft")
    assert draft["status"] == "queued"
    assert draft["checkpoint"]["result_origin"] == "human_reconciliation"


def test_delivered_recovered_stores_human_text_as_a_verified_artifact(tmp_path):
    service = _service(tmp_path)
    stuck = _stuck_run(service, tmp_path)
    resolutions = [
        {"step_id": "draft", "verdict": "delivered_recovered", "result_text": "recovered output"}
    ]
    proposal = service.dispatch(
        "agent_hub_cancel",
        {
            "action": "prepare_reconcile",
            "run_id": stuck["run_id"],
            "expected_revision": stuck["revision"],
            "resolutions": resolutions,
            "run_disposition": "resume",
        },
    )["data"]

    # The proposal carries only a digest of the recovered text, never the text.
    assert "recovered output" not in str(proposal)
    assert proposal["resolutions"][0]["result_sha256"]

    applied = service.dispatch(
        "agent_hub_cancel",
        {
            "action": "apply_reconcile",
            "run_id": stuck["run_id"],
            "expected_revision": stuck["revision"],
            "resolutions": resolutions,
            "run_disposition": "resume",
            "proposal": {
                key: value
                for key, value in proposal.items()
                if key not in {"confirmation_phrase", "confirmation_prompt"}
            },
            "proposal_sha256": proposal["proposal_sha256"],
            "confirmation_phrase": proposal["confirmation_phrase"],
        },
    )["data"]

    draft = next(step for step in applied["steps"] if step["step_id"] == "draft")
    assert draft["status"] == "completed"
    assert len(draft["output_artifact_ids"]) == 1

    artifact = service.dispatch(
        "agent_hub_artifact",
        {"action": "get", "artifact_id": draft["output_artifact_ids"][0]},
    )["data"]
    assert artifact["verification"]["source"] == "human_reconciliation"


def test_apply_requires_the_matching_confirmation_phrase(tmp_path):
    service = _service(tmp_path)
    stuck = _stuck_run(service, tmp_path)
    proposal = service.dispatch(
        "agent_hub_cancel",
        {
            "action": "prepare_reconcile",
            "run_id": stuck["run_id"],
            "expected_revision": stuck["revision"],
            "resolutions": [{"step_id": "draft", "verdict": "not_delivered"}],
            "run_disposition": "resume",
        },
    )["data"]
    reviewed = {
        key: value
        for key, value in proposal.items()
        if key not in {"confirmation_phrase", "confirmation_prompt"}
    }

    wrong = service.dispatch(
        "agent_hub_cancel",
        {
            "action": "apply_reconcile",
            "run_id": stuck["run_id"],
            "expected_revision": stuck["revision"],
            "resolutions": [{"step_id": "draft", "verdict": "not_delivered"}],
            "run_disposition": "resume",
            "proposal": reviewed,
            "proposal_sha256": proposal["proposal_sha256"],
            "confirmation_phrase": "discard-00000000",
        },
    )

    assert wrong["success"] is False
    assert wrong["error"]["code"] == "reconciliation_confirmation_mismatch"
    assert service.store.get_run(stuck["run_id"])["status"] == "outcome_unknown"


def test_apply_rejects_a_verdict_swapped_after_review(tmp_path):
    service = _service(tmp_path)
    stuck = _stuck_run(service, tmp_path)
    proposal = service.dispatch(
        "agent_hub_cancel",
        {
            "action": "prepare_reconcile",
            "run_id": stuck["run_id"],
            "expected_revision": stuck["revision"],
            "resolutions": [{"step_id": "draft", "verdict": "delivered_discarded"}],
            "run_disposition": "fail",
        },
    )["data"]

    escalated = service.dispatch(
        "agent_hub_cancel",
        {
            "action": "apply_reconcile",
            "run_id": stuck["run_id"],
            "expected_revision": stuck["revision"],
            # Swapping in the re-sending verdict must not ride a discard proposal.
            "resolutions": [{"step_id": "draft", "verdict": "not_delivered"}],
            "run_disposition": "fail",
            "proposal": {
                key: value
                for key, value in proposal.items()
                if key not in {"confirmation_phrase", "confirmation_prompt"}
            },
            "proposal_sha256": proposal["proposal_sha256"],
            "confirmation_phrase": proposal["confirmation_phrase"],
        },
    )

    assert escalated["success"] is False
    assert escalated["error"]["code"] == "proposal_digest_conflict"


def test_prepare_requires_every_ambiguous_step_to_be_judged(tmp_path):
    service = _service(tmp_path)
    run = service.store.create_run(
        plan=_plan(),
        project_root=str(tmp_path),
        idempotency_key=f"rec.partial.{secrets.token_hex(4)}",
    )
    claim = service.store.claim_run(run["run_id"], expected_revision=run["revision"])
    updated = service.store.update_step(
        run["run_id"],
        step_id="draft",
        expected_run_revision=claim.revision,
        status="outcome_unknown",
        checkpoint={"phase": "outcome_unknown", "error_code": "provider_timeout"},
    )
    updated = service.store.update_step(
        run["run_id"],
        step_id="review",
        expected_run_revision=updated["revision"],
        status="outcome_unknown",
        checkpoint={"phase": "outcome_unknown", "error_code": "provider_timeout"},
    )
    stuck = service.store.finalize_claim(
        run["run_id"],
        claim_token=claim.claim_token,
        expected_revision=updated["revision"],
        status="outcome_unknown",
        event_type="run_paused",
        details={"reason_code": "outcome_unknown"},
    )

    with pytest.raises(HubV2Error) as partial:
        service.store.prepare_run_reconciliation(
            stuck["run_id"],
            expected_revision=stuck["revision"],
            resolutions={
                "run_disposition": "resume",
                "resolutions": [{"step_id": "draft", "verdict": "not_delivered"}],
            },
        )

    assert partial.value.code == "reconciliation_incomplete"


def test_reconciliation_is_revision_fenced_and_single_use(tmp_path):
    service = _service(tmp_path)
    stuck = _stuck_run(service, tmp_path)
    resolutions = {
        "run_disposition": "fail",
        "resolutions": [{"step_id": "draft", "verdict": "delivered_discarded"}],
    }

    with pytest.raises(HubV2Error) as stale:
        service.store.prepare_run_reconciliation(
            stuck["run_id"], expected_revision=99, resolutions=resolutions
        )
    assert stale.value.code == "revision_conflict"

    first = service.store.prepare_run_reconciliation(
        stuck["run_id"], expected_revision=stuck["revision"], resolutions=resolutions
    )
    # Preparing again supersedes the earlier proposal so only one can be applied.
    second = service.store.prepare_run_reconciliation(
        stuck["run_id"], expected_revision=stuck["revision"], resolutions=resolutions
    )

    with pytest.raises(HubV2Error) as superseded:
        service.store.apply_run_reconciliation(
            stuck["run_id"],
            expected_revision=stuck["revision"],
            proposal={
                key: value
                for key, value in first.items()
                if key not in {"confirmation_phrase", "confirmation_prompt"}
            },
            proposal_sha256=first["proposal_sha256"],
            confirmation_phrase=first["confirmation_phrase"],
            recovered_artifacts={},
        )
    assert superseded.value.code == "reconciliation_not_found"

    applied = service.store.apply_run_reconciliation(
        stuck["run_id"],
        expected_revision=stuck["revision"],
        proposal={
            key: value
            for key, value in second.items()
            if key not in {"confirmation_phrase", "confirmation_prompt"}
        },
        proposal_sha256=second["proposal_sha256"],
        confirmation_phrase=second["confirmation_phrase"],
        recovered_artifacts={},
    )
    assert applied["status"] == "failed"


def test_reconciliation_has_a_bounded_number_of_attempts(tmp_path):
    service = _service(tmp_path)
    stuck = _stuck_run(service, tmp_path)
    run_id = stuck["run_id"]
    revision = stuck["revision"]

    for _ in range(MAX_RUN_RECONCILIATIONS):
        resolutions = {
            "run_disposition": "resume",
            "resolutions": [{"step_id": "draft", "verdict": "not_delivered"}],
        }
        proposal = service.store.prepare_run_reconciliation(
            run_id, expected_revision=revision, resolutions=resolutions
        )
        applied = service.store.apply_run_reconciliation(
            run_id,
            expected_revision=revision,
            proposal={
                key: value
                for key, value in proposal.items()
                if key not in {"confirmation_phrase", "confirmation_prompt"}
            },
            proposal_sha256=proposal["proposal_sha256"],
            confirmation_phrase=proposal["confirmation_phrase"],
            recovered_artifacts={},
        )
        # Return the run to outcome_unknown so the next round can be attempted.
        claim = service.store.claim_run(run_id, expected_revision=applied["revision"])
        updated = service.store.update_step(
            run_id,
            step_id="draft",
            expected_run_revision=claim.revision,
            status="outcome_unknown",
            checkpoint={"phase": "outcome_unknown", "error_code": "provider_timeout"},
        )
        settled = service.store.finalize_claim(
            run_id,
            claim_token=claim.claim_token,
            expected_revision=updated["revision"],
            status="outcome_unknown",
            event_type="run_paused",
            details={"reason_code": "outcome_unknown"},
        )
        revision = settled["revision"]

    with pytest.raises(HubV2Error) as exhausted:
        service.store.prepare_run_reconciliation(
            run_id,
            expected_revision=revision,
            resolutions={
                "run_disposition": "resume",
                "resolutions": [{"step_id": "draft", "verdict": "not_delivered"}],
            },
        )
    assert exhausted.value.code == "reconciliation_limit_exceeded"


def test_events_record_the_reconciliation_without_user_text(tmp_path):
    service = _service(tmp_path)
    stuck = _stuck_run(service, tmp_path)
    resolutions = [
        {"step_id": "draft", "verdict": "delivered_recovered", "result_text": "sensitive result"}
    ]
    proposal = service.dispatch(
        "agent_hub_cancel",
        {
            "action": "prepare_reconcile",
            "run_id": stuck["run_id"],
            "expected_revision": stuck["revision"],
            "resolutions": resolutions,
            "run_disposition": "resume",
        },
    )["data"]
    service.dispatch(
        "agent_hub_cancel",
        {
            "action": "apply_reconcile",
            "run_id": stuck["run_id"],
            "expected_revision": stuck["revision"],
            "resolutions": resolutions,
            "run_disposition": "resume",
            "proposal": {
                key: value
                for key, value in proposal.items()
                if key not in {"confirmation_phrase", "confirmation_prompt"}
            },
            "proposal_sha256": proposal["proposal_sha256"],
            "confirmation_phrase": proposal["confirmation_phrase"],
        },
    )

    page = service.dispatch("agent_hub_events", {"run_id": stuck["run_id"]})["data"]
    types = [event["type"] for event in page["events"]]

    assert "reconciliation_prepared" in types
    assert "run_reconciled" in types
    assert "sensitive result" not in str(page)
