"""A refusal a caller can act on names the argument to change.

Two live codes on this machine, both argument-shaped and neither saying which
argument. `durable_run_required` fired 5 times: agent_hub_execute refuses
recorded work, but the message ("Recorded work must use agent_hub_start") does
not say whether `record` or `task.retention` asked for it, and neither field was
described in the schema. `invalid_egress_proposal` fired 4 times, the last on the
day this was written: apply needs the whole prepare result, but the message
("The egress proposal is incomplete") does not say which key went missing.

Same rule as the handoff work: the message is returned to the caller verbatim,
so it is assembled from authored literals and closed-set field names only.
"""

from __future__ import annotations

import pytest

from agent_hub.v2.contracts import TASK_SCHEMA
from agent_hub.v2.crypto import ArtifactCipher, StaticKeyProvider
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.service import HubService
from agent_hub.v2.store import HubStore
from agent_hub.v2.tools import tool_definitions
from agent_hub.v2 import egress

from tests.agent_hub.test_v2_service import _FakeWorker


def _service(tmp_path):
    return HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_FakeWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )


def _task(**overrides):
    return {
        "schema": TASK_SCHEMA,
        "intent": "Answer the question.",
        "capability": "chat",
        "inline_input": "",
        **overrides,
    }


# --- agent_hub_execute refusing recorded work -------------------------------


@pytest.mark.parametrize(
    ("arguments", "field"),
    [
        ({"record": True}, "record"),
        ({"task_overrides": {"retention": "durable_private"}}, "task.retention"),
    ],
)
def test_the_refusal_says_which_argument_asked_for_a_recorded_run(tmp_path, arguments, field):
    service = _service(tmp_path)
    overrides = arguments.pop("task_overrides", {})

    refused = service.dispatch(
        "agent_hub_execute",
        {"task": _task(**overrides), "project_root": str(tmp_path), **arguments},
    )

    assert refused["error"]["code"] == "durable_run_required"
    assert refused["error"]["safe_details"]["field"] == field
    assert field in refused["error"]["message"]


def test_the_refusal_offers_the_other_way_out(tmp_path):
    """Switching tools is not the only fix: dropping the field also works, and
    a caller who only wanted an inline answer should hear that."""

    refused = _service(tmp_path).dispatch(
        "agent_hub_execute",
        {"task": _task(), "project_root": str(tmp_path), "record": True},
    )

    assert "drop it" in refused["error"]["message"]
    assert refused["error"]["next_action"]["tool"] == "agent_hub_start"


def test_the_two_fields_that_trigger_it_are_described(tmp_path):
    execute = next(item for item in tool_definitions() if item["name"] == "agent_hub_execute")
    properties = execute["inputSchema"]["properties"]

    assert "agent_hub_start" in properties["record"]["description"]
    retention = properties["task"]["properties"]["retention"]
    assert "ephemeral" in retention["description"]
    assert "agent_hub_start" in retention["description"]


def test_an_ephemeral_task_is_not_refused(tmp_path):
    """The default must stay usable, or the fix above is just a wider door."""

    service = _service(tmp_path)

    answered = service.dispatch(
        "agent_hub_execute",
        {"task": _task(), "project_root": str(tmp_path)},
    )

    assert answered["error"] is None


# --- agent_hub_plan apply refusing a trimmed proposal -----------------------


@pytest.mark.parametrize("dropped", ["manifest", "fact_pack"])
def test_a_trimmed_proposal_names_the_missing_key(dropped):
    proposal = {"manifest": {}, "fact_pack": {}}
    proposal.pop(dropped)

    with pytest.raises(HubV2Error) as refused:
        egress.verify_egress_approval(
            proposal,
            approved_manifest_sha256="0" * 64,
            expected_policy_revision=0,
        )

    assert refused.value.code == "invalid_egress_proposal"
    assert refused.value.safe_details["missing"] == [dropped]
    assert dropped in refused.value.message


def test_a_missing_proposal_says_to_send_the_prepare_result(tmp_path):
    refused = _service(tmp_path).dispatch(
        "agent_hub_plan",
        {
            "mode": "apply",
            "project_root": str(tmp_path),
            "task": _task(),
            "proposal_sha256": "0" * 64,
        },
    )

    assert refused["error"]["code"] == "invalid_egress_proposal"
    assert refused["error"]["safe_details"]["field"] == "proposal"
    assert "unmodified" in refused["error"]["message"]


def test_a_digest_mismatch_says_not_to_recompute_it(tmp_path):
    refused = _service(tmp_path).dispatch(
        "agent_hub_plan",
        {
            "mode": "apply",
            "project_root": str(tmp_path),
            "task": _task(),
            "proposal": {"proposal_sha256": "a" * 64},
            "proposal_sha256": "b" * 64,
        },
    )

    assert refused["error"]["code"] == "proposal_digest_conflict"
    assert refused["error"]["safe_details"]["field"] == "proposal_sha256"
    assert "recompute" in refused["error"]["message"]


def test_no_refusal_message_carries_caller_data():
    """These messages are returned verbatim, so they must be assembled from
    authored text and closed-set key names -- never from the proposal itself."""

    proposal = {"fact_pack": {"secret": "s3cret-value"}}

    with pytest.raises(HubV2Error) as refused:
        egress.verify_egress_approval(
            proposal,
            approved_manifest_sha256="0" * 64,
            expected_policy_revision=0,
        )

    assert "s3cret-value" not in refused.value.message
    assert refused.value.safe_details == {"missing": ["manifest"]}
