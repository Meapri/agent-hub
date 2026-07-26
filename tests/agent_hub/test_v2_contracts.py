from __future__ import annotations

from importlib import resources
import json

import pytest

from agent_hub.v2.contracts import (
    PLAN_SCHEMA,
    PROVIDER_MANIFEST_SCHEMA,
    TASK_SCHEMA,
    ensure_public_model_id,
    validate_plan,
    validate_provider_manifest,
    validate_task,
)
from agent_hub.v2.errors import HubV2Error, safe_unexpected_error
from agent_hub.v2.migrate import plan_v1_import
from agent_hub.v2.provider_manifests import builtin_provider_manifests


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


def test_builtin_provider_manifests_are_v2_conformant():
    manifests = builtin_provider_manifests()

    assert [item["provider_id"] for item in manifests] == [
        "claude",
        "grok",
        "gemini",
        "gpt",
    ]
    assert all(item["schema"] == PROVIDER_MANIFEST_SCHEMA for item in manifests)
    assert "search" not in next(item for item in manifests if item["provider_id"] == "gpt")[
        "capabilities"
    ]


@pytest.mark.parametrize("model", ["MODEL_PLACEHOLDER_M71", "model_internal", "foo-placeholder"])
def test_generation_rejects_placeholder_model_ids(model):
    with pytest.raises(HubV2Error, match="placeholder"):
        ensure_public_model_id(model)


def test_safe_unexpected_error_does_not_accept_exception_text():
    result = safe_unexpected_error(operation="fixture")

    assert result["error"]["code"] == "internal_error"
    assert "fixture secret" not in json.dumps(result)


def test_v1_import_plan_is_read_only_and_does_not_rewrite_source(tmp_path):
    source = tmp_path / "runs"
    source.mkdir()
    run = source / "abc123def456.json"
    original = json.dumps(
        {
            "run_id": "abc123def456",
            "run_kind": "adaptive",
            "status": "completed",
            "state_schema_version": 2,
            "prompt": "must not appear in import metadata",
        }
    )
    run.write_text(original)

    plan = plan_v1_import(source)

    assert plan["read_only"] is True
    assert plan["entries"][0]["mode"] == "archive_metadata_only"
    assert "prompt" not in json.dumps(plan)
    assert run.read_text() == original


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
        "egress_manifest_v2",
    }
