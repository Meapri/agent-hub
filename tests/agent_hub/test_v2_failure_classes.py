"""The failure taxonomy, and the end-to-end behaviour it is supposed to produce.

The motivating defect: three runs in the author's own database sit at
status=failed, retry_safe=False, error_code=operation_failed. They cannot be
retried and cannot be reconciled, because the code that classified them was
spread across five sites that disagreed. These tests pin the chain from "a
provider reports a failure it does not name" to "an operator can settle the run".
"""

from __future__ import annotations

import secrets
import time

import pytest

from agent_hub.v2 import failure_classes, provider_runtime
from agent_hub.v2.contracts import PLAN_SCHEMA, TASK_SCHEMA, validate_plan
from agent_hub.v2.crypto import ArtifactCipher, StaticKeyProvider
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.failure_classes import (
    FAILURE_CLASSES,
    UNCLASSIFIED_PROVIDER_FAILURE,
    classify,
)
from agent_hub.v2.provider_worker import _raise_failed_payload, handle_request
from agent_hub.v2.service import HubService
from agent_hub.v2.store import HubStore


class _FakeWorker:
    """Returns success unless the subclass says otherwise."""

    def __init__(self, provider):
        self.provider = provider

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "status":
            return {"success": True, "data": {"providers": {self.provider: {"ready": True}}}}
        if method == "catalog":
            return {"success": True, "warnings": [], "data": {"models": {}}}
        if method == "invoke":
            return {
                "success": True,
                "text": f"completed by {self.provider}",
                "model": f"{self.provider}-fixture",
                "usage": {"total_tokens": 10},
            }
        raise AssertionError(method)

    def cancel(self):
        return True


def _worker_raising(code: str):
    class _Raising(_FakeWorker):
        def request(self, method, params=None, timeout=30.0, request_id=None):
            if method == "invoke":
                # Shaped like the daemon-side promotion in ProviderClient._decode,
                # which forwards whatever code the worker reported.
                raise HubV2Error(code, "The provider failed.", scope="provider")
            return super().request(method, params=params, timeout=timeout, request_id=request_id)

    return _Raising


def _plan():
    return validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Answer the fixture.",
                "capability": "chat",
                "inline_input": "x",
                "retention": "durable_private",
            },
            "steps": [
                {
                    "id": "answer",
                    "capability": "chat",
                    "instruction": "Answer.",
                    "routing_requirements": {"planner_provider": "gpt"},
                }
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )


def _run_until_settled(service, run_id):
    for _ in range(200):
        current = service.store.get_run(run_id)
        if current["status"] != "running":
            return current
        time.sleep(0.01)
    raise AssertionError("run never settled")


def _drive(tmp_path, code: str):
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_worker_raising(code),
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": _plan(),
            "project_root": str(tmp_path),
            "idempotency_key": f"cls.{secrets.token_hex(4)}",
        },
    )
    run_id = started["data"]["run_id"]
    service.dispatch("agent_hub_continue", {"run_id": run_id, "expected_revision": 0})
    return service, _run_until_settled(service, run_id)


# --- the table itself -------------------------------------------------------


def test_an_unrecognized_code_is_never_treated_as_safe_to_resend():
    # The whole point of the conservative default: a code nobody enumerated
    # must not be guessed into the one class that re-sends automatically.
    assert classify("some_code_a_provider_invented_yesterday") == "ambiguous"
    assert classify(None) == "ambiguous"
    assert classify("") == "ambiguous"
    assert failure_classes.is_retryable("some_code_a_provider_invented_yesterday") is False


def test_every_classified_code_names_one_of_the_three_classes():
    assert set(FAILURE_CLASSES.values()) <= {"retry_safe", "ambiguous", "terminal"}


def test_the_wire_retryable_flag_cannot_contradict_the_table():
    # provider_client raises this with retryable=True. An agent that believes
    # the flag would re-send a request that may already have run, so the
    # taxonomy has to win on the way out.
    ambiguous = HubV2Error("provider_timeout", "timed out", scope="provider", retryable=True)
    assert classify("provider_timeout") == "ambiguous"
    assert ambiguous.public()["retryable"] is False

    # Codes outside the table are not step outcomes, so their own flag stands.
    unrelated = HubV2Error("daemon_unavailable", "no daemon", scope="runtime", retryable=True)
    assert failure_classes.is_known("daemon_unavailable") is False
    assert unrelated.public()["retryable"] is True


def test_the_runtime_names_its_own_unclassified_failure():
    # provider_runtime substitutes this when a payload reports failure without
    # naming it, and the table has to have an entry for the name it chose --
    # otherwise the substitution is indistinguishable from a provider typo.
    assert UNCLASSIFIED_PROVIDER_FAILURE in FAILURE_CLASSES
    assert classify(UNCLASSIFIED_PROVIDER_FAILURE) == "ambiguous"


def test_an_unnamed_provider_payload_failure_becomes_the_unclassified_code(monkeypatch):
    # This is the exact shape that produced the stuck operation_failed rows:
    # success=False with nothing identifying the error.
    monkeypatch.setattr(
        provider_runtime,
        "invoke",
        lambda *_a, **_k: {"success": False, "text": "upstream blew up"},
    )
    result = handle_request(
        "gpt",
        {
            "id": "x",
            "method": "invoke",
            "params": {
                "task": {
                    "schema": TASK_SCHEMA,
                    "intent": "Chat.",
                    "capability": "chat",
                    "inline_input": "fixture",
                }
            },
        },
    )

    assert result["success"] is False
    assert result["error"]["code"] == UNCLASSIFIED_PROVIDER_FAILURE
    assert result["error"]["retryable"] is False


# --- what the classes mean for a live run -----------------------------------


def test_an_unnamed_provider_failure_parks_for_reconciliation_instead_of_stranding(tmp_path):
    """The completion criterion for the taxonomy work.

    A failure isomorphic to the three stuck runs must reach outcome_unknown,
    not failed/retry_safe=False, and must then be settleable by an operator.
    """

    service, settled = _drive(tmp_path, UNCLASSIFIED_PROVIDER_FAILURE)
    step = settled["steps"][0]

    assert step["status"] == "outcome_unknown"
    assert step["checkpoint"]["error_code"] == UNCLASSIFIED_PROVIDER_FAILURE
    assert step["checkpoint"]["retry_safe"] is False
    assert settled["outcome_unknown_steps"] == ["answer"]

    # And it is reachable from here: the run settles through reconciliation.
    resolutions = [{"step_id": "answer", "verdict": "delivered_discarded"}]
    proposal = service.dispatch(
        "agent_hub_cancel",
        {
            "action": "prepare_reconcile",
            "run_id": settled["run_id"],
            "expected_revision": settled["revision"],
            "resolutions": resolutions,
            "run_disposition": "fail",
        },
    )["data"]
    applied = service.dispatch(
        "agent_hub_cancel",
        {
            "action": "apply_reconcile",
            "run_id": settled["run_id"],
            "expected_revision": settled["revision"],
            "resolutions": resolutions,
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

    assert applied["success"] is True
    assert applied["data"]["status"] == "failed"
    assert applied["data"]["steps"][0]["status"] != "outcome_unknown"


def test_a_code_missing_from_the_table_is_announced_as_a_gap(tmp_path):
    service, settled = _drive(tmp_path, "provider_invented_a_new_name")
    events = service.store.events(settled["run_id"])["events"]

    gaps = [item for item in events if item["type"] == "provider_failure_unclassified"]
    assert [item["details"]["error_code"] for item in gaps] == ["provider_invented_a_new_name"]
    assert gaps[0]["details"]["reason_code"] == "ambiguous"
    assert settled["steps"][0]["status"] == "outcome_unknown"


@pytest.mark.parametrize(
    "code",
    [code for code, value in FAILURE_CLASSES.items() if value == "terminal"][:4],
)
def test_terminal_failures_end_the_step_without_asking_an_operator(tmp_path, code):
    _service, settled = _drive(tmp_path, code)
    step = settled["steps"][0]

    assert step["status"] == "failed"
    assert step["checkpoint"]["retry_safe"] is False
    assert settled["outcome_unknown_steps"] == []


def test_a_declined_request_stays_retryable_without_an_operator(tmp_path):
    _service, settled = _drive(tmp_path, "rate_limit")
    step = settled["steps"][0]

    # One provider, no fallbacks: the loop exhausts and summarises, and the
    # summary has to stay safe because every attempt under it was safe.
    assert step["status"] == "failed"
    assert step["checkpoint"]["error_code"] == "fallback_exhausted"
    assert step["checkpoint"]["retry_safe"] is True
    assert settled["retryable_failed_steps"] == ["answer"]


def test_an_unnamed_failure_records_which_payload_shape_produced_it():
    """The dead end this closes.

    provider_unclassified_failure says the provider failed and would not say
    why. For agent_hub_execute there is no run, so no event carries anything
    else, and the reason is simply gone. The payload's key names identify which
    adapter path produced it while carrying none of its content.
    """

    with pytest.raises(HubV2Error) as unnamed:
        _raise_failed_payload(
            {"success": False, "text": "upstream blew up", "model": "m", "warnings": []}
        )

    details = unnamed.value.safe_details
    assert details["reason_code"] == UNCLASSIFIED_PROVIDER_FAILURE
    assert details["payload_keys"] == ["model", "success", "text", "warnings"]
    # The values are the provider's; only the shape crosses the boundary.
    assert "upstream blew up" not in str(details)


def test_a_named_failure_does_not_carry_the_payload_shape():
    with pytest.raises(HubV2Error) as named:
        _raise_failed_payload({"success": False, "error": {"type": "rate_limit"}})

    assert "payload_keys" not in named.value.safe_details
