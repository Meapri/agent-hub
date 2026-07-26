from __future__ import annotations

from importlib import resources
import json

import pytest

from agent_hub.v2.contracts import (
    PLAN_SCHEMA,
    PROVIDER_MANIFEST_SCHEMA,
    TASK_SCHEMA,
    ensure_public_model_id,
    input_token_limit,
    output_token_limit,
    total_token_limit,
    validate_plan,
    validate_provider_manifest,
    validate_task,
)
from agent_hub.v2.errors import HubV2Error, safe_unexpected_error
from agent_hub.v2.provider_manifests import builtin_provider_manifests, model_input_limit


def _task():
    return {
        "schema": TASK_SCHEMA,
        "intent": "Review the implementation.",
        "capability": "review",
        "inline_input": "safe fixture",
        "constraints": {"provider_allowlist": ["claude", "gpt"]},
        "retention": "ephemeral",
    }


def test_task_contract_normalizes_defaults():
    task = validate_task(_task())

    assert task["schema"] == TASK_SCHEMA
    assert task["input_artifacts"] == []
    assert task["constraints"]["provider_allowlist"] == ["claude", "gpt"]


def test_task_contract_separates_input_output_and_total_token_budgets():
    canonical = validate_task(
        {
            **_task(),
            "constraints": {
                "provider_allowlist": ["gpt"],
                "max_input_tokens": 12_000,
                "max_output_tokens": 4_096,
                "max_total_tokens": 20_000,
            },
        }
    )
    legacy = validate_task(
        {
            **_task(),
            "constraints": {
                "provider_allowlist": ["gpt"],
                "max_tokens": 8_192,
            },
        }
    )

    assert input_token_limit(canonical["constraints"]) == 12_000
    assert output_token_limit(canonical["constraints"]) == 4_096
    assert total_token_limit(canonical["constraints"]) == 20_000
    assert input_token_limit(legacy["constraints"]) is None
    assert output_token_limit(legacy["constraints"]) == 8_192
    assert total_token_limit(legacy["constraints"]) == 8_192


@pytest.mark.parametrize(
    ("payload", "code", "fields"),
    [
        ({**_task(), "retentin": "ephemeral"}, "invalid_request", ["retentin"]),
        (
            {
                **_task(),
                "constraints": {"provider_allowlist": ["gpt"], "max_token": 10},
            },
            "invalid_request",
            ["max_token"],
        ),
    ],
)
def test_task_contract_rejects_unknown_fields(payload, code, fields):
    with pytest.raises(HubV2Error) as error:
        validate_task(payload)

    assert error.value.code == code
    assert error.value.safe_details == {"fields": fields}


def test_plan_contract_rejects_cycles():
    with pytest.raises(HubV2Error, match="cycle"):
        validate_plan(
            {
                "schema": PLAN_SCHEMA,
                "task": _task(),
                "steps": [
                    {
                        "id": "a",
                        "capability": "review",
                        "depends_on": ["b"],
                        "instruction": "a",
                    },
                    {
                        "id": "b",
                        "capability": "review",
                        "depends_on": ["a"],
                        "instruction": "b",
                    },
                ],
                "routing_mode": "shadow",
                "policy_revision": 0,
            }
        )


def test_plan_contract_hashes_validated_content():
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": _task(),
            "steps": [
                {
                    "id": "inspect",
                    "capability": "inspect",
                    "instruction": "Inspect the safe fixture.",
                },
                {
                    "id": "review",
                    "capability": "review",
                    "depends_on": ["inspect"],
                    "instruction": "Review it.",
                },
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )

    assert len(plan["plan_sha256"]) == 64


def test_plan_contract_rejects_content_changed_after_digest():
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": _task(),
            "steps": [
                {
                    "id": "review",
                    "capability": "review",
                    "instruction": "Review it.",
                }
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )
    plan["steps"][0]["instruction"] = "Changed after review."

    with pytest.raises(HubV2Error) as error:
        validate_plan(plan)

    assert error.value.code == "plan_digest_conflict"


def test_plan_contract_rejects_unknown_step_fields():
    with pytest.raises(HubV2Error) as error:
        validate_plan(
            {
                "schema": PLAN_SCHEMA,
                "task": _task(),
                "steps": [
                    {
                        "id": "review",
                        "capability": "review",
                        "instruction": "Review it.",
                        "fallback_provider": "gpt",
                    }
                ],
                "routing_mode": "shadow",
                "policy_revision": 0,
            }
        )

    assert error.value.code == "invalid_plan"
    assert error.value.safe_details == {"fields": ["fallback_provider"]}


def test_provider_manifest_rejects_domain_paths():
    with pytest.raises(HubV2Error, match="host names"):
        validate_provider_manifest(
            {
                "schema": PROVIDER_MANIFEST_SCHEMA,
                "provider_id": "fixture",
                "adapter_version": "1",
                "protocol_version": "2.0",
                "capabilities": ["chat"],
                "auth_owner": "fixture",
                "auth_mode": "none",
                "allowed_domains": ["example.com/path"],
            }
        )


def test_provider_manifest_rejects_unsupported_worker_protocol():
    with pytest.raises(HubV2Error) as error:
        validate_provider_manifest(
            {
                "schema": PROVIDER_MANIFEST_SCHEMA,
                "provider_id": "fixture",
                "adapter_version": "1",
                "protocol_version": "3.0",
                "capabilities": ["chat"],
                "auth_owner": "fixture",
                "auth_mode": "none",
                "allowed_domains": ["example.com"],
            }
        )

    assert error.value.code == "unsupported_protocol_version"
    assert error.value.safe_details == {"supported": ["2.0"]}


def test_builtin_provider_manifests_are_v2_conformant():
    manifests = builtin_provider_manifests()

    assert [item["provider_id"] for item in manifests] == [
        "claude",
        "grok",
        "gemini",
        "gpt",
    ]
    assert all(item["schema"] == PROVIDER_MANIFEST_SCHEMA for item in manifests)
    assert (
        "search"
        not in next(item for item in manifests if item["provider_id"] == "gpt")["capabilities"]
    )
    assert all(item["context_limits"]["default_max_input_tokens"] > 0 for item in manifests)


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("claude", "claude-opus-5", 1_000_000),
        ("claude", "claude-haiku-4-5-20251001", 200_000),
        ("grok", "grok-4.5", 500_000),
        ("grok", "grok-4.20-0309-reasoning", 1_000_000),
        ("gemini", "gemini-3.6-flash-high", 1_048_576),
        ("gemini", "gpt-oss-120b", 131_072),
        ("gpt", "gpt-5.6-sol", 258_400),
        ("gpt", "gpt-5.3-codex-spark", 121_600),
    ],
)
def test_builtin_model_input_limits_use_longest_matching_override(
    provider,
    model,
    expected,
):
    assert model_input_limit(provider, model)["max_input_tokens"] == expected


def test_provider_manifest_rejects_duplicate_context_limit_prefixes():
    with pytest.raises(HubV2Error) as error:
        validate_provider_manifest(
            {
                "schema": PROVIDER_MANIFEST_SCHEMA,
                "provider_id": "fixture",
                "adapter_version": "1",
                "protocol_version": "2.0",
                "capabilities": ["chat"],
                "auth_owner": "fixture",
                "auth_mode": "none",
                "allowed_domains": ["example.com"],
                "context_limits": {
                    "default_max_input_tokens": 10,
                    "model_overrides": [
                        {"model_prefix": "fixture-", "max_input_tokens": 20},
                        {"model_prefix": "fixture-", "max_input_tokens": 30},
                    ],
                },
            }
        )

    assert error.value.code == "invalid_provider_manifest"


@pytest.mark.parametrize("model", ["MODEL_PLACEHOLDER_M71", "model_internal", "foo-placeholder"])
def test_generation_rejects_placeholder_model_ids(model):
    with pytest.raises(HubV2Error, match="placeholder"):
        ensure_public_model_id(model)


def test_safe_unexpected_error_does_not_accept_exception_text():
    result = safe_unexpected_error(operation="fixture")

    assert result["error"]["code"] == "internal_error"
    assert "fixture secret" not in json.dumps(result)


def test_packaged_contract_fixture_contains_all_public_v2_schemas():
    path = resources.files("agent_hub.v2").joinpath("schemas", "contracts.json")
    fixture = json.loads(path.read_text(encoding="utf-8"))

    assert set(fixture["$defs"]) == {
        "task_v2",
        "plan_v2",
        "run_v3",
        "event_v2",
        "artifact_v2",
        "provider_manifest_v2",
        "routing_decision_v1",
        "egress_review_v1",
        "egress_manifest_v2",
    }
    run_properties = fixture["$defs"]["run_v3"]["properties"]
    assert run_properties["retryable_failed_steps"]["items"] == {"type": "string"}
    assert run_properties["next_action"]["properties"]["tool"]["const"] == "agent_hub_continue"
