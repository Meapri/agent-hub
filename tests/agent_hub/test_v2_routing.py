from __future__ import annotations

import pytest

from agent_hub.v2.contracts import TASK_SCHEMA
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.routing import route, routing_context
from agent_hub.v2.store import HubStore


def _task():
    return {
        "schema": TASK_SCHEMA,
        "intent": "검토해 주세요.",
        "capability": "review",
        "inline_input": "fixture",
    }


def _ready():
    return {"claude": True, "grok": True, "gemini": True, "gpt": True}


def test_shadow_routing_never_changes_planner_choice(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")

    decision = route(
        store=store,
        task=_task(),
        planner_provider="gpt",
        routing_mode="shadow",
        provider_allowlist=list(_ready()),
        readiness=_ready(),
    )

    assert decision["selected_provider"] == "gpt"
    assert decision["reason_code"] == "shadow_preserves_planner"


def test_pinned_routing_keeps_the_explicit_provider(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")

    decision = route(
        store=store,
        task=_task(),
        planner_provider="gpt",
        routing_mode="pinned",
        provider_allowlist=list(_ready()),
        readiness=_ready(),
    )

    assert decision["selected_provider"] == "gpt"
    assert decision["reason_code"] == "pinned_preserves_planner"


def test_pinned_routing_fails_when_explicit_provider_is_ineligible(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    readiness = _ready()
    readiness["gpt"] = False

    with pytest.raises(HubV2Error) as error:
        route(
            store=store,
            task=_task(),
            planner_provider="gpt",
            routing_mode="pinned",
            provider_allowlist=list(readiness),
            readiness=readiness,
        )

    assert error.value.code == "pinned_provider_unavailable"


def test_auto_preserves_planner_until_exact_context_has_twenty_samples(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    context = routing_context(_task())
    for _ in range(19):
        store.record_routing_sample(
            context=context,
            provider="claude",
            model=None,
            capability="review",
            success=True,
            quality=1.0,
            latency_ms=100,
            total_tokens=100,
            signal_weight=5.0,
        )

    cold = route(
        store=store,
        task=_task(),
        planner_provider="gpt",
        routing_mode="auto",
        provider_allowlist=list(_ready()),
        readiness=_ready(),
    )
    assert cold["selected_provider"] == "gpt"

    store.record_routing_sample(
        context=context,
        provider="claude",
        model=None,
        capability="review",
        success=True,
        quality=1.0,
        latency_ms=100,
        total_tokens=100,
        signal_weight=5.0,
    )
    for _ in range(20):
        store.record_routing_sample(
            context=context,
            provider="gpt",
            model=None,
            capability="review",
            success=True,
            quality=0.6,
            latency_ms=1_000,
            total_tokens=1_000,
            signal_weight=5.0,
        )
    learned = route(
        store=store,
        task=_task(),
        planner_provider="gpt",
        routing_mode="auto",
        provider_allowlist=list(_ready()),
        readiness=_ready(),
    )
    assert learned["selected_provider"] == "claude"
    assert learned["reason_code"] == "statistical_auto"


def test_hard_filters_remove_unready_and_unsupported_providers(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    task = {
        "schema": TASK_SCHEMA,
        "intent": "Search.",
        "capability": "search",
        "inline_input": "fixture",
    }

    decision = route(
        store=store,
        task=task,
        planner_provider="gpt",
        routing_mode="shadow",
        provider_allowlist=["gpt", "grok"],
        readiness={"gpt": True, "grok": True},
    )

    assert decision["selected_provider"] == "grok"
    excluded = {
        item["provider"]: item["excluded_reason"]
        for item in decision["candidates"]
        if not item["eligible"]
    }
    assert excluded["gpt"] == "capability_unsupported"


def test_circuit_breaker_is_a_hard_routing_filter(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")
    for _ in range(3):
        health = store.record_provider_outcome(
            provider="gpt",
            success=False,
            error_code="provider_failure",
        )
    assert health["circuit_open"] is True

    decision = route(
        store=store,
        task=_task(),
        planner_provider="gpt",
        routing_mode="shadow",
        provider_allowlist=list(_ready()),
        readiness=_ready(),
        circuit_open={"gpt": True},
    )

    assert decision["selected_provider"] != "gpt"
    gpt = next(item for item in decision["candidates"] if item["provider"] == "gpt")
    assert gpt["excluded_reason"] == "circuit_open"


def test_context_limit_excludes_only_models_that_cannot_accept_input(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")

    decision = route(
        store=store,
        task=_task(),
        planner_provider="gpt",
        routing_mode="shadow",
        provider_allowlist=["gpt", "claude"],
        readiness={"gpt": True, "claude": True},
        models={
            "gpt": "gpt-5.3-codex-spark",
            "claude": "claude-sonnet-5",
        },
        estimated_input_tokens=125_000,
    )

    assert decision["selected_provider"] == "claude"
    gpt = next(item for item in decision["candidates"] if item["provider"] == "gpt")
    assert gpt["excluded_reason"] == "context_limit"
    assert gpt["max_input_tokens"] == 121_600


def test_pinned_context_limit_returns_actionable_context_error(tmp_path):
    store = HubStore(tmp_path / "state.sqlite3")

    with pytest.raises(HubV2Error) as error:
        route(
            store=store,
            task=_task(),
            planner_provider="gpt",
            routing_mode="pinned",
            provider_allowlist=["gpt", "claude"],
            readiness={"gpt": True, "claude": True},
            models={
                "gpt": "gpt-5.3-codex-spark",
                "claude": "claude-sonnet-5",
            },
            estimated_input_tokens=125_000,
        )

    assert error.value.code == "provider_context_limit"
    assert error.value.safe_details == {
        "provider": "gpt",
        "model": "gpt-5.3-codex-spark",
        "estimated_input_tokens": 125_000,
        "max_input_tokens": 121_600,
    }
