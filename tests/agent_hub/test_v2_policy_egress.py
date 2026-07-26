from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.v2.egress import prepare_egress, verify_egress_approval
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.experimental import (
    ExperimentalRuntime,
    ExperimentalRuntimeRegistry,
)
from agent_hub.v2.policy import (
    apply_policy_update,
    load_policy,
    prepare_policy_update,
)


def test_policy_prepare_apply_is_revision_and_digest_fenced(tmp_path):
    initial = load_policy(str(tmp_path))
    assert initial.exists is False
    assert initial.policy["routing_mode"] == "shadow"
    assert initial.policy["experimental"] == {
        "isolated_tool_worker": False,
        "local_model": False,
        "remote_worker": False,
    }

    proposal = prepare_policy_update(
        str(tmp_path),
        patch={"routing_mode": "advisory"},
        expected_revision=0,
    )
    applied = apply_policy_update(
        str(tmp_path),
        proposal=proposal,
        proposal_sha256=proposal["proposal_sha256"],
    )

    assert applied.policy["revision"] == 1
    assert applied.policy["routing_mode"] == "advisory"
    assert applied.policy["experimental"]["remote_worker"] is False
    assert Path(applied.path).stat().st_mode & 0o777 == 0o600

    with pytest.raises(HubV2Error) as stale:
        apply_policy_update(
            str(tmp_path),
            proposal=proposal,
            proposal_sha256=proposal["proposal_sha256"],
        )
    assert stale.value.code == "policy_file_conflict"


def test_experimental_runtime_requires_project_flag_and_tool_sandbox(tmp_path):
    registry = ExperimentalRuntimeRegistry()
    policy = load_policy(str(tmp_path)).policy
    runtime = ExperimentalRuntime(
        feature="isolated_tool_worker",
        runtime_id="fixture.tool-worker",
        sandboxed=True,
    )

    with pytest.raises(HubV2Error) as disabled:
        registry.register(runtime, policy=policy)
    assert disabled.value.code == "experimental_feature_disabled"

    enabled = {
        **policy,
        "experimental": {
            **policy["experimental"],
            "isolated_tool_worker": True,
        },
    }
    with pytest.raises(HubV2Error) as unsandboxed:
        registry.register(
            ExperimentalRuntime(
                feature="isolated_tool_worker",
                runtime_id="fixture.unsafe",
                sandboxed=False,
            ),
            policy=enabled,
        )
    assert unsandboxed.value.code == "experimental_sandbox_required"

    status = registry.register(runtime, policy=enabled)
    assert status["features"]["isolated_tool_worker"] == {
        "enabled": True,
        "registered": True,
        "runtime_id": "fixture.tool-worker",
        "sandboxed": True,
    }


def test_policy_rejects_unknown_or_non_boolean_experimental_flags(tmp_path):
    with pytest.raises(HubV2Error) as unknown:
        prepare_policy_update(
            str(tmp_path),
            patch={"experimental": {"future_shell": True}},
            expected_revision=0,
        )
    assert unknown.value.code == "invalid_policy"

    with pytest.raises(HubV2Error) as non_boolean:
        prepare_policy_update(
            str(tmp_path),
            patch={"experimental": {"local_model": "yes"}},
            expected_revision=0,
        )
    assert non_boolean.value.code == "invalid_policy"


def test_egress_prepare_redacts_secret_lines_and_uses_relative_aliases(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "safe.txt").write_text(
        "safe line\napi_key = '123456789-secret-value'\nlast line\n"
    )

    proposal = prepare_egress(
        project_root=str(tmp_path),
        provider="claude",
        model="claude-opus-5",
        source_paths=["src/safe.txt"],
        policy_revision=0,
        estimated_max_tokens=1024,
    )

    entry = proposal["manifest"]["entries"][0]
    content = proposal["fact_pack"]["items"][0]["content"]
    assert entry["path_alias"] == "src/safe.txt"
    assert str(tmp_path) not in str(proposal["manifest"])
    assert "123456789-secret-value" not in content
    assert entry["secret_candidates_redacted"] == 1


def test_egress_apply_rejects_manifest_or_fact_pack_tampering(tmp_path):
    (tmp_path / "safe.txt").write_text("safe")
    proposal = prepare_egress(
        project_root=str(tmp_path),
        provider="gpt",
        model="gpt-5.6-sol",
        source_paths=["safe.txt"],
        policy_revision=0,
        estimated_max_tokens=100,
    )
    digest = proposal["manifest"]["manifest_sha256"]
    proposal["fact_pack"]["items"][0]["content"] = "tampered"

    with pytest.raises(HubV2Error) as error:
        verify_egress_approval(
            proposal,
            approved_manifest_sha256=digest,
            expected_policy_revision=0,
        )
    assert error.value.code == "egress_proposal_tampered"


def test_egress_rejects_path_escape(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("safe")

    with pytest.raises(HubV2Error) as error:
        prepare_egress(
            project_root=str(tmp_path),
            provider="gpt",
            model=None,
            source_paths=["../outside.txt"],
            policy_revision=0,
            estimated_max_tokens=100,
        )
    assert error.value.code == "invalid_source_path"
