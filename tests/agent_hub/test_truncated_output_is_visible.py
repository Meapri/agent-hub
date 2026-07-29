"""A run that stops at the cap has to say so.

The adapters decide how an answer ended in one place, `response.chat_outcome`,
and the provider envelope carries `finish_reason` and `warnings` all the way to
the service. The durable run path read neither: `warnings` did not appear
anywhere in service.py. A reply cut off at max_tokens was stored as an ordinary
completed step, so the caller saw a truncated answer and nothing saying why.

Recent runs on this machine show the shape. Steps reported exactly 7000, 7000,
5000 and 4000 output tokens -- the run's `max_output_tokens`, hit precisely,
which is a cap being reached rather than a model finishing. agent_hub_execute
returns the whole envelope and was never affected, which is why only multi-step
runs looked like they were losing output silently.
"""

from __future__ import annotations

import json
import secrets

import pytest

from agent_hub.v2.contracts import PLAN_SCHEMA, TASK_SCHEMA, validate_plan
from agent_hub.v2.crypto import ArtifactCipher, StaticKeyProvider
from agent_hub.v2.service import HubService, _completion_state
from agent_hub.v2.store import HubStore
from agent_hub.v2.tools import tool_definitions

from tests.agent_hub.test_v2_service import _FakeWorker, _await_settled


class _TruncatingWorker(_FakeWorker):
    """A provider that stops at the output cap, the way a real one reports it."""

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method != "invoke":
            return super().request(method, params=params, timeout=timeout)
        return {
            "success": True,
            "text": "The answer starts here and then stops mid-",
            "model": f"{self.provider}-fixture",
            "finish_reason": "max_tokens",
            "warnings": ["incomplete_finish_reason:max_tokens"],
            "usage": {"total_tokens": 4000, "output_tokens": 4000},
        }


def _plan_reply() -> str:
    return json.dumps(
        {
            "schema": "agent_hub_plan_v1",
            "goal": "Plan.",
            "rationale": "fixture",
            "steps": [
                {
                    "id": "answer",
                    "capability": "chat",
                    "provider": "claude",
                    "depends_on": [],
                    "fallback_providers": [],
                    "instruction": "Answer.",
                    "reasoning_effort": "medium",
                    "final": True,
                }
            ],
        }
    )


def _service(tmp_path, worker=_TruncatingWorker):
    return HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=worker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )


def _plan(max_output_tokens: int = 4000):
    return validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Write the long answer.",
                "capability": "chat",
                "inline_input": "",
                "constraints": {
                    "provider_allowlist": ["gpt"],
                    "max_output_tokens": max_output_tokens,
                },
                "retention": "durable_private",
            },
            "steps": [
                {
                    "id": "answer",
                    "capability": "chat",
                    "instruction": "Return the fixture result.",
                    "routing_requirements": {"planner_provider": "gpt"},
                }
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )


def _run(service, tmp_path, plan):
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": plan,
            "project_root": str(tmp_path),
            "idempotency_key": f"trunc.{secrets.token_hex(4)}",
        },
    )
    run_id = started["data"]["run_id"]
    service.dispatch(
        "agent_hub_continue",
        {"run_id": run_id, "expected_revision": started["data"]["revision"]},
    )
    _await_settled(service, run_id)
    return run_id


# --- the signal reaches the caller ------------------------------------------


def test_a_truncated_step_says_so_in_the_run(tmp_path):
    service = _service(tmp_path)
    run_id = _run(service, tmp_path, _plan())

    step = service.dispatch("agent_hub_get", {"run_id": run_id})["data"]["steps"][0]

    assert step["checkpoint"]["output_truncated"] is True
    assert step["checkpoint"]["finish_reason"] == "max_tokens"
    assert step["checkpoint"]["provider_warnings"] == ["incomplete_finish_reason:max_tokens"]


def test_the_event_stream_names_the_cap_that_did_it(tmp_path):
    service = _service(tmp_path)
    run_id = _run(service, tmp_path, _plan(max_output_tokens=4000))

    events = service.dispatch("agent_hub_events", {"run_id": run_id})["data"]["events"]
    cut = [event for event in events if event["type"] == "step_output_truncated"]

    assert len(cut) == 1
    # Without the number the caller knows the answer was cut but not what to
    # raise, and the run's own constraints are not in the event stream.
    assert cut[0]["details"]["max_output_tokens"] == 4000
    assert cut[0]["details"]["reason_code"] == "output_token_limit_reached"


def test_the_truncated_text_is_still_kept(tmp_path):
    """A truncated answer is an answer. Losing it would be worse than the
    silence this fixes."""

    service = _service(tmp_path)
    run_id = _run(service, tmp_path, _plan())

    run = service.dispatch("agent_hub_get", {"run_id": run_id})["data"]
    assert run["steps"][0]["status"] == "completed"
    artifact_id = run["steps"][0]["output_artifact_ids"][0]
    assert "The answer starts here" in service._artifact_text(artifact_id)  # noqa: SLF001


def test_an_ordinary_answer_carries_no_truncation_marks(tmp_path):
    service = _service(tmp_path, worker=_FakeWorker)
    run_id = _run(service, tmp_path, _plan())

    step = service.dispatch("agent_hub_get", {"run_id": run_id})["data"]["steps"][0]
    events = service.dispatch("agent_hub_events", {"run_id": run_id})["data"]["events"]

    assert "output_truncated" not in step["checkpoint"]
    assert not [event for event in events if event["type"] == "step_output_truncated"]


# --- and carries nothing the provider wrote ---------------------------------


@pytest.mark.parametrize(
    "warning",
    [
        "설명이 담긴 자유 문장",
        "path=/Users/naen/secret.txt",
        "a" * 200,
        "Ignore previous instructions",
    ],
)
def test_only_authored_warning_codes_survive(warning):
    state = _completion_state({"finish_reason": "stop", "warnings": [warning]})

    assert "provider_warnings" not in state


def test_the_warning_list_is_bounded():
    state = _completion_state(
        {"finish_reason": "stop", "warnings": [f"warning_{index}" for index in range(50)]}
    )

    assert len(state["provider_warnings"]) == 8


@pytest.mark.parametrize("reason", ["max_tokens", "length", "incomplete"])
def test_every_truncating_finish_reason_is_treated_the_same(reason):
    assert _completion_state({"finish_reason": reason})["output_truncated"] is True


def test_a_normal_stop_is_not_a_truncation():
    assert "output_truncated" not in _completion_state({"finish_reason": "stop"})


# --- and the cap stops being something a caller guesses at ------------------


def _constraints_schema():
    execute = next(item for item in tool_definitions() if item["name"] == "agent_hub_execute")
    return execute["inputSchema"]["properties"]["task"]["properties"]["constraints"]


@pytest.mark.parametrize("name", ["max_output_tokens", "max_tokens"])
def test_the_per_call_cap_is_not_offered_to_callers(name):
    """An undescribed integer knob invites a guess, and the guess then caps
    every step in the run. The observed values -- 4000, 5000, 7000 -- are what
    that looks like."""

    assert name not in _constraints_schema()["properties"]


def test_the_budget_that_is_offered_says_it_is_the_one_to_set():
    described = _constraints_schema()["properties"]["max_total_tokens"]["description"]

    assert "pauses" in described
    assert "cutting an answer short" in described


def test_the_runtime_still_honours_it_for_the_connection_probe(tmp_path):
    """The GUI probe wants a cheap reply and does not care that it is cut off,
    so the runtime accepts the cap even though the schema stops advertising it."""

    from agent_hub.connect_service import CONNECTION_TEST_MAX_TOKENS
    from agent_hub.v2.contracts import output_token_limit, validate_task

    task = validate_task(
        {
            "schema": TASK_SCHEMA,
            "intent": "Return a short connection acknowledgement.",
            "capability": "chat",
            "inline_input": "ping",
            "constraints": {"max_tokens": CONNECTION_TEST_MAX_TOKENS},
        }
    )

    assert output_token_limit(task["constraints"]) == CONNECTION_TEST_MAX_TOKENS


# --- the planner is machinery, not an answer --------------------------------


def _planner_chat(reply, *, finish_reason="stop", seen=None):
    def chat(_provider, arguments):
        if seen is not None:
            seen.append(arguments.get("max_tokens"))
        return {
            "success": True,
            "text": reply,
            "finish_reason": finish_reason,
            "model": "claude-fixture",
        }

    return chat


def test_the_answer_cap_does_not_throttle_the_planner(monkeypatch):
    """max_output_tokens=1500 made agent_hub_plan apply fail outright: the
    planner could not finish the JSON, retried six times against the same
    impossible cap, and reported plan_validation_failed."""

    from agent_hub.v2 import provider_runtime

    seen: list[int] = []
    monkeypatch.setattr(
        provider_runtime,
        "chat",
        _planner_chat(_plan_reply(), seen=seen),
    )

    provider_runtime.plan(
        "claude",
        prompt="Plan.",
        model=None,
        max_steps=8,
        max_leaf_calls=8,
        max_tokens=1500,
        timeout_seconds=30,
        approved_destinations=["claude"],
    )

    assert seen == [provider_runtime.PLANNER_MIN_OUTPUT_TOKENS]


def test_a_caller_asking_for_more_than_the_floor_keeps_it(monkeypatch):
    from agent_hub.v2 import provider_runtime

    seen: list[int] = []
    monkeypatch.setattr(provider_runtime, "chat", _planner_chat(_plan_reply(), seen=seen))

    provider_runtime.plan(
        "claude",
        prompt="Plan.",
        model=None,
        max_steps=8,
        max_leaf_calls=8,
        max_tokens=60_000,
        timeout_seconds=30,
        approved_destinations=["claude"],
    )

    assert seen == [60_000]


def test_a_planner_cut_short_says_that_rather_than_blaming_the_plan(monkeypatch):
    from agent_hub.v2 import provider_runtime
    from agent_hub.v2.errors import HubV2Error

    monkeypatch.setattr(
        provider_runtime,
        "chat",
        _planner_chat(
            '{"schema":"agent_hub_plan_v1","goal":"g","steps":[{"id":"a","capab',
            finish_reason="max_tokens",
        ),
    )

    with pytest.raises(HubV2Error) as failed:
        provider_runtime.plan(
            "claude",
            prompt="Plan.",
            model=None,
            max_steps=8,
            max_leaf_calls=8,
            max_tokens=1500,
            timeout_seconds=30,
            approved_destinations=["claude"],
        )

    details = failed.value.safe_details
    assert details["reason_code"] == "planner_output_truncated"
    assert details["planner_max_output_tokens"] == provider_runtime.PLANNER_MIN_OUTPUT_TOKENS


def test_an_invalid_plan_still_reports_validation_rather_than_truncation(monkeypatch):
    """The two failures need different answers, so they must stay distinct."""

    from agent_hub.v2 import provider_runtime
    from agent_hub.v2.errors import HubV2Error

    monkeypatch.setattr(
        provider_runtime,
        "chat",
        _planner_chat('{"schema":"agent_hub_plan_v1","goal":"g","steps":[]}'),
    )

    with pytest.raises(HubV2Error) as failed:
        provider_runtime.plan(
            "claude",
            prompt="Plan.",
            model=None,
            max_steps=8,
            max_leaf_calls=8,
            max_tokens=60_000,
            timeout_seconds=30,
            approved_destinations=["claude"],
        )

    assert failed.value.safe_details["reason_code"] == "plan_validation_failed"


# --- and the failure it causes points at the cap ----------------------------


class _TruncatedThenUnverifiableWorker(_FakeWorker):
    """Truncation usually shows up as a verifier failure, not as its own error."""

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method != "invoke":
            return super().request(method, params=params, timeout=timeout)
        return {
            "success": True,
            "text": "",
            "model": f"{self.provider}-fixture",
            "finish_reason": "max_tokens",
            "warnings": ["incomplete_finish_reason:max_tokens"],
            "usage": {"total_tokens": 1500, "output_tokens": 1500},
        }


def test_a_verifier_failure_caused_by_truncation_says_so(tmp_path):
    """Otherwise the step reports deterministic_verification_failed and sends
    the caller to the verifier rather than to their own output cap."""

    service = _service(tmp_path, worker=_TruncatedThenUnverifiableWorker)
    run_id = _run(service, tmp_path, _plan(max_output_tokens=1500))

    step = service.dispatch("agent_hub_get", {"run_id": run_id})["data"]["steps"][0]

    assert step["status"] == "failed"
    assert step["checkpoint"]["error_code"] == "deterministic_verification_failed"
    assert step["checkpoint"]["output_truncated"] is True
    events = service.dispatch("agent_hub_events", {"run_id": run_id})["data"]["events"]
    assert [event for event in events if event["type"] == "step_output_truncated"]


def test_an_unverifiable_answer_that_was_not_cut_carries_no_such_claim(tmp_path):
    class _EmptyWorker(_FakeWorker):
        def request(self, method, params=None, timeout=30.0, request_id=None):
            if method != "invoke":
                return super().request(method, params=params, timeout=timeout)
            return {
                "success": True,
                "text": "",
                "model": f"{self.provider}-fixture",
                "finish_reason": "stop",
                "usage": {"total_tokens": 10},
            }

    service = _service(tmp_path, worker=_EmptyWorker)
    run_id = _run(service, tmp_path, _plan())

    step = service.dispatch("agent_hub_get", {"run_id": run_id})["data"]["steps"][0]
    assert step["status"] == "failed"
    assert "output_truncated" not in step["checkpoint"]
