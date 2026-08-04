"""Provider selection, which is now a function of its arguments and nothing else.

The router this replaced consulted stored statistics, so the same step could
select differently on Tuesday than on Monday and a failed run could not be
reproduced from its inputs. These tests pin the properties that make the
replacement worth having: the same arguments give the same answer, the caller's
stated order is honoured, and a caller who named a provider is never quietly
given a different one.
"""

from __future__ import annotations

import pytest

from agent_hub.v2.contracts import TASK_SCHEMA
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.provider_selection import select_provider

ALL = ["claude", "gpt", "gemini", "grok"]


def _task(capability: str = "chat") -> dict:
    return {
        "schema": TASK_SCHEMA,
        "intent": "Answer the fixture.",
        "capability": capability,
        "inline_input": "",
    }


def _select(**overrides):
    kwargs = {
        "task": _task(),
        "planner_provider": "claude",
        "provider_allowlist": ALL,
        "readiness": dict.fromkeys(ALL, True),
    }
    kwargs.update(overrides)
    return select_provider(**kwargs)


def test_the_same_arguments_always_give_the_same_answer():
    # The property the scoring router could not offer: a run that failed can be
    # explained from its inputs, without asking what the database held that day.
    first = _select()
    second = _select()

    assert first == second


def test_the_planner_provider_is_used_when_it_is_eligible():
    decision = _select(planner_provider="grok")

    assert decision["selected_provider"] == "grok"
    assert decision["reason_code"] == "planner_provider_eligible"


def test_an_ineligible_planner_provider_falls_through_in_allowlist_order():
    # Allowlist order is the caller's stated preference, so it decides who is
    # next -- not a score, and not alphabetical order.
    decision = _select(
        planner_provider="claude",
        provider_allowlist=["claude", "grok", "gemini", "gpt"],
        readiness={"claude": False, "grok": True, "gemini": True, "gpt": True},
    )

    assert decision["selected_provider"] == "grok"
    assert decision["reason_code"] == "planner_provider_ineligible"
    assert decision["fallbacks"] == ["gemini", "gpt"]


def test_a_pinned_provider_is_never_silently_replaced():
    # The caller named it. Substituting would answer a question they did not ask.
    with pytest.raises(HubV2Error) as unavailable:
        _select(
            planner_provider="claude",
            pinned=True,
            readiness={"claude": False, "gpt": True, "gemini": True, "grok": True},
        )

    assert unavailable.value.code == "pinned_provider_unavailable"
    assert unavailable.value.retryable is True


@pytest.mark.parametrize(
    ("reason", "overrides"),
    [
        # Each case knocks out grok only, so the call still succeeds through
        # claude and the candidate list can be inspected.
        ("not_allowed", {"provider_allowlist": ["claude", "gpt", "gemini"]}),
        # claude does not serve image generation; grok and gemini do.
        (
            "capability_unsupported",
            {"task": _task("image"), "planner_provider": "grok", "excluded": "claude"},
        ),
        ("not_ready", {"readiness": {**dict.fromkeys(ALL, True), "grok": False}}),
        ("circuit_open", {"circuit_open": {"grok": True}}),
        (
            "context_limit",
            {
                "estimated_input_tokens": 200_000,
                "models": {"grok": "grok-fixture"},
                "model_limits": {
                    "grok": {
                        "provider": "grok",
                        "model": "grok-fixture",
                        "max_input_tokens": 1_000,
                    }
                },
            },
        ),
    ],
)
def test_each_exclusion_reason_is_named_on_the_candidate_it_applies_to(reason, overrides):
    excluded = overrides.pop("excluded", "grok")
    decision = _select(**overrides)

    candidate = next(item for item in decision["candidates"] if item["provider"] == excluded)

    assert candidate["eligible"] is False
    assert candidate["excluded_reason"] == reason
    assert excluded not in decision["fallbacks"]
    assert decision["selected_provider"] != excluded


def test_the_first_applicable_exclusion_reason_wins():
    # A provider can fail several checks at once. Reporting the earliest one
    # keeps the message stable and points at the cause the caller controls.
    decision = _select(
        provider_allowlist=["claude", "gpt", "gemini"],
        readiness={**dict.fromkeys(ALL, True), "grok": False},
        circuit_open={"grok": True},
    )

    grok = next(item for item in decision["candidates"] if item["provider"] == "grok")

    assert grok["excluded_reason"] == "not_allowed"


def test_an_input_too_large_for_everyone_says_so_rather_than_no_provider():
    # "nobody is available" and "this input is too big" need different remedies,
    # so they must not collapse into the same error.
    with pytest.raises(HubV2Error) as too_big:
        _select(estimated_input_tokens=100_000_000)

    assert too_big.value.code == "provider_context_limit"
    assert too_big.value.retryable is False
    assert too_big.value.safe_details["blocked_provider_count"] > 0


def test_no_eligible_provider_is_distinct_from_a_context_limit():
    with pytest.raises(HubV2Error) as none_ready:
        _select(readiness=dict.fromkeys(ALL, False))

    assert none_ready.value.code == "no_eligible_provider"


def test_selection_reads_no_history_and_writes_nothing():
    # select_provider takes no store. If it ever grows one, this fails to
    # compile rather than quietly reintroducing a stateful decision.
    import inspect

    parameters = set(inspect.signature(select_provider).parameters)

    assert "store" not in parameters
    assert parameters == {
        "task",
        "planner_provider",
        "provider_allowlist",
        "readiness",
        "pinned",
        "circuit_open",
        "models",
        "model_limits",
        "estimated_input_tokens",
        # Data the caller hands in, not a source this function reads for
        # itself. Same arguments still give the same answer.
        "login_commands",
    }


def test_fallbacks_exclude_the_selected_provider_and_everything_ineligible():
    decision = _select(
        planner_provider="gpt",
        provider_allowlist=["gpt", "claude", "grok"],
        readiness={"gpt": True, "claude": True, "grok": False, "gemini": True},
    )

    assert decision["selected_provider"] == "gpt"
    # gemini is ready but not allowlisted; grok is allowlisted but not ready.
    assert decision["fallbacks"] == ["claude"]
