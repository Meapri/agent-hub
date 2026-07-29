"""The planner is judged against the list it was given.

`agent_hub_plan` failed 6 times on this machine with planner_egress_violation,
most recently the day this was written, and it is a terminal code with no
retry. The cause was two allowlists. The prompt announced, under the heading
"Allowed capabilities and providers (hard constraint)", the static
CAPABILITY_PROVIDERS table -- claude, grok, gemini, gpt -- and invited
`fallback_providers: ["compatible alternatives, if useful"]`. The runtime then
enforced the run's approved egress destinations, which default to the single
requested provider. A planner that obeyed its instructions lost the whole plan.

The evidence that this was a prompt problem and not a model problem: the
constraint the prompt does state, the capability list, produced zero
planner_capability_violation failures over the same period.
"""

from __future__ import annotations

import json

import pytest

from agent_hub import orchestrator
from agent_hub.v2 import provider_runtime
from agent_hub.v2.provider_runtime import RUNTIME_PLANNER_CAPABILITIES

APPROVED = ("gpt",)


def _plan_json(provider: str, *, fallbacks: list[str] | None = None) -> str:
    return json.dumps(
        {
            "schema": "agent_hub_plan_v1",
            "goal": "Plan.",
            "rationale": "fixture",
            "steps": [
                {
                    "id": "answer",
                    "capability": "chat",
                    "provider": provider,
                    "depends_on": [],
                    "fallback_providers": fallbacks or [],
                    "instruction": "Answer.",
                    "reasoning_effort": "medium",
                    "final": True,
                }
            ],
        }
    )


# --- what the planner is told ----------------------------------------------


def test_the_manifest_offers_only_the_approved_destinations():
    manifest = orchestrator.capability_manifest(
        allowed_capabilities=RUNTIME_PLANNER_CAPABILITIES,
        allowed_providers=APPROVED,
    )

    for capability, entry in manifest.items():
        assert set(entry["providers"]) <= set(APPROVED), capability


def test_a_capability_no_approved_provider_can_serve_is_not_offered():
    # gpt has no search capability, so offering a search step would only invite
    # one the runtime has to reject.
    assert "search" not in orchestrator.capability_manifest(
        allowed_capabilities=RUNTIME_PLANNER_CAPABILITIES,
        allowed_providers=("gpt",),
    )
    assert "search" in orchestrator.capability_manifest(
        allowed_capabilities=RUNTIME_PLANNER_CAPABILITIES,
        allowed_providers=("claude",),
    )


def test_the_prompt_states_the_rule_it_will_be_judged_by():
    prompt = orchestrator.planner_prompt(
        "Plan.",
        allowed_capabilities=RUNTIME_PLANNER_CAPABILITIES,
        allowed_providers=APPROVED,
    )

    assert "fallback_providers entry must appear" in prompt
    # The unapproved providers must not appear in the constraint the prompt
    # calls hard. Naming them there is what produced the violations.
    constraint = prompt.split("Contract:")[0]
    for absent in ("claude", "grok", "gemini"):
        assert absent not in constraint, absent


# --- and judged by the same one ---------------------------------------------


@pytest.mark.parametrize(
    "step",
    [
        {"provider": "claude", "fallback_providers": []},
        {"provider": "gpt", "fallback_providers": ["gemini"]},
    ],
)
def test_validate_plan_refuses_an_unapproved_destination(step):
    plan = {
        "schema": "agent_hub_plan_v1",
        "goal": "Plan.",
        "steps": [
            {
                "id": "answer",
                "capability": "chat",
                "instruction": "Answer.",
                "final": True,
                **step,
            }
        ],
    }

    with pytest.raises(ValueError) as refused:
        orchestrator.validate_plan(
            plan,
            allowed_capabilities=RUNTIME_PLANNER_CAPABILITIES,
            allowed_providers=APPROVED,
        )

    # The repair loop hands this string back to the planner, so it has to say
    # what to choose instead.
    assert "gpt" in str(refused.value)


def test_no_approved_list_leaves_the_previous_behaviour_alone():
    plan = {
        "schema": "agent_hub_plan_v1",
        "goal": "Plan.",
        "steps": [
            {
                "id": "answer",
                "capability": "chat",
                "provider": "claude",
                "instruction": "Answer.",
                "final": True,
            }
        ],
    }

    validated = orchestrator.validate_plan(
        plan,
        allowed_capabilities=RUNTIME_PLANNER_CAPABILITIES,
    )

    assert validated["steps"][0]["provider"] == "claude"


def test_what_the_prompt_offers_is_exactly_what_the_validator_accepts():
    """The defect was these two disagreeing. Nothing else in this file catches
    a change that widens both at once, so tie them together."""

    offered = orchestrator.capability_manifest(
        allowed_capabilities=RUNTIME_PLANNER_CAPABILITIES,
        allowed_providers=APPROVED,
    )

    for capability, entry in offered.items():
        for provider in entry["providers"]:
            # Not every capability may be final, so the offered one leads and a
            # chat step closes the DAG.
            orchestrator.validate_plan(
                {
                    "schema": "agent_hub_plan_v1",
                    "goal": "Plan.",
                    "steps": [
                        {
                            "id": "seed",
                            "capability": "chat",
                            "provider": provider,
                            "instruction": "Start.",
                            "final": False,
                        },
                        {
                            "id": "offered",
                            "capability": capability,
                            "provider": provider,
                            "depends_on": ["seed"],
                            "instruction": "Do the offered work.",
                            "final": False,
                        },
                        {
                            "id": "answer",
                            "capability": "chat",
                            "provider": provider,
                            "depends_on": ["offered"],
                            "instruction": "Answer.",
                            "final": True,
                        },
                    ],
                },
                allowed_capabilities=RUNTIME_PLANNER_CAPABILITIES,
                allowed_providers=APPROVED,
            )


# --- and repaired in the loop that already exists ---------------------------


def test_an_unapproved_provider_is_repaired_rather_than_losing_the_plan(monkeypatch):
    prompts: list[str] = []
    replies = [_plan_json("claude"), _plan_json("gpt")]

    def chat(_provider, arguments):
        prompts.append(arguments["prompt"])
        return {"success": True, "text": replies[len(prompts) - 1], "model": "gpt-fixture"}

    monkeypatch.setattr(provider_runtime, "chat", chat)

    result = provider_runtime.plan(
        "gpt",
        prompt="Plan.",
        model="gpt-fixture",
        max_steps=4,
        max_leaf_calls=4,
        max_tokens=1024,
        timeout_seconds=30,
        approved_destinations=list(APPROVED),
    )

    assert result["success"] is True
    assert result["data"]["planner"]["attempts"] == 2
    # The second prompt has to carry the reason, or the planner repeats itself.
    assert "not an approved destination" in prompts[1]


def test_the_first_prompt_already_carries_the_restriction(monkeypatch):
    prompts: list[str] = []

    def chat(_provider, arguments):
        prompts.append(arguments["prompt"])
        return {"success": True, "text": _plan_json("gpt"), "model": "gpt-fixture"}

    monkeypatch.setattr(provider_runtime, "chat", chat)

    provider_runtime.plan(
        "gpt",
        prompt="Plan.",
        model="gpt-fixture",
        max_steps=4,
        max_leaf_calls=4,
        max_tokens=1024,
        timeout_seconds=30,
        approved_destinations=list(APPROVED),
    )

    assert len(prompts) == 1
    assert '"providers": ["gpt"]' in prompts[0]


# --- and it is the same set the runtime fence uses --------------------------


def test_the_service_hands_the_worker_the_set_its_fence_will_use(tmp_path):
    """The fence and the prompt must read the same list.

    `_PlannerWorker` deliberately ignores the restriction, so
    test_plan_apply_rejects_provider_outside_approved_destinations still proves
    the fence holds. This proves the planner was given a fair chance first.
    """

    from tests.agent_hub.test_v2_service import _approve_review, _PlannerWorker
    from agent_hub.v2.crypto import ArtifactCipher, StaticKeyProvider
    from agent_hub.v2.contracts import TASK_SCHEMA
    from agent_hub.v2.service import HubService
    from agent_hub.v2.store import HubStore

    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_PlannerWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    (tmp_path / "fact.txt").write_text("safe fact")
    task = {
        "schema": TASK_SCHEMA,
        "intent": "Use GPT only.",
        "capability": "write",
        "inline_input": "",
        "constraints": {"provider_allowlist": ["gpt"]},
    }
    prepared = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "prepare",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "source_paths": ["fact.txt"],
            "task": task,
        },
    )["data"]
    service.dispatch(
        "agent_hub_plan",
        {
            "mode": "apply",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "task": task,
            "proposal": prepared,
            "proposal_sha256": prepared["proposal_sha256"],
            "expected_policy_revision": 0,
            "approval_request_id": _approve_review(service, prepared),
        },
    )

    assert _PlannerWorker.last_params["approved_destinations"] == sorted(
        prepared["manifest"]["destinations"]
    )
