from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from agent_hub.v2.egress import (
    prepare_egress,
    redact_secret_lines,
    verify_egress_approval,
)
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
    (source / "safe.txt").write_text("safe line\napi_key = '123456789-secret-value'\nlast line\n")

    proposal = prepare_egress(
        project_root=str(tmp_path),
        provider="claude",
        model="claude-opus-5",
        destination_providers=["claude", "gemini"],
        source_paths=["src/safe.txt"],
        policy_revision=0,
        estimated_max_tokens=1024,
    )

    entry = proposal["manifest"]["entries"][0]
    content = proposal["fact_pack"]["items"][0]["content"]
    assert entry["path_alias"] == "src/safe.txt"
    assert proposal["manifest"]["destinations"] == ["claude", "gemini"]
    assert str(tmp_path) not in str(proposal["manifest"])
    assert "123456789-secret-value" not in content
    assert entry["secret_candidates_redacted"] == 1


def test_secret_redaction_catches_unquoted_assignment_values(tmp_path):
    (tmp_path / "secrets.env").write_text("api_key=unquoted_secret_value_123456\nsafe=yes\n")

    proposal = prepare_egress(
        project_root=str(tmp_path),
        provider="gpt",
        model=None,
        source_paths=["secrets.env"],
        policy_revision=0,
        estimated_max_tokens=100,
    )

    content = proposal["fact_pack"]["items"][0]["content"]
    assert "unquoted_secret_value" not in content
    assert content == "[REDACTED SECRET CANDIDATE]\nsafe=yes\n"


@pytest.mark.parametrize(
    "secret_line",
    [
        "password=fixture-password-value",
        "SECRET_KEY='fixture-secret-key-value'",
        "aws_secret_access_key=fixture-aws-secret-value",
        "token=github_pat_fixturefixturefixturefixture",
        "slack=xoxb-fixture-fixture-fixture",
        "stripe=sk_live_fixturefixturefixture",
        "aws_access_key=AKIAFIXTUREFIXTURE12",
        "jwt=eyJmaXh0dXJlMTIz.NGVtb2ZpeHR1cmU0NTY.c2lnbmF0dXJlNzg5",
    ],
)
def test_secret_redaction_catches_common_credential_shapes(tmp_path, secret_line):
    (tmp_path / "secrets.env").write_text(f"safe=yes\n{secret_line}\n")

    proposal = prepare_egress(
        project_root=str(tmp_path),
        provider="gpt",
        model=None,
        source_paths=["secrets.env"],
        policy_revision=0,
        estimated_max_tokens=100,
    )

    content = proposal["fact_pack"]["items"][0]["content"]
    assert content == "safe=yes\n[REDACTED SECRET CANDIDATE]\n"
    assert proposal["manifest"]["entries"][0]["secret_candidates_redacted"] == 1


def test_secret_redaction_removes_entire_private_key_block():
    private_key = (
        "before\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "ZmFrZS1wcml2YXRlLWtleS1ib2R5\n"
        "bW9yZS1mYWtlLWtleS1ib2R5\n"
        "-----END PRIVATE KEY-----\n"
        "after\n"
    )
    content, redacted_count = redact_secret_lines(private_key)
    assert "ZmFr" not in content
    assert content == (
        "before\n"
        "[REDACTED SECRET CANDIDATE]\n"
        "[REDACTED SECRET CANDIDATE]\n"
        "[REDACTED SECRET CANDIDATE]\n"
        "[REDACTED SECRET CANDIDATE]\n"
        "after\n"
    )
    assert redacted_count == 4


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".ssh/id_ed25519",
        ".aws/credentials",
        ".netrc",
        "deploy/private.pem",
        "config/credentials.json",
    ],
)
def test_egress_rejects_credential_sensitive_paths_before_reading(tmp_path, path):
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("safe-looking fixture")

    with pytest.raises(HubV2Error) as raised:
        prepare_egress(
            project_root=str(tmp_path),
            provider="gpt",
            model=None,
            source_paths=[path],
            policy_revision=0,
            estimated_max_tokens=100,
        )

    assert raised.value.code == "sensitive_source_denied"
    assert raised.value.safe_details == {"path": path}


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


def test_egress_identity_is_stable_across_collection_times(tmp_path, monkeypatch):
    (tmp_path / "safe.txt").write_text("stable source")
    monkeypatch.setattr("agent_hub.v2.egress.time.time", lambda: 100.0)
    first = prepare_egress(
        project_root=str(tmp_path),
        provider="gpt",
        model="gpt-5.6-sol",
        source_paths=["safe.txt"],
        policy_revision=0,
        estimated_max_tokens=100,
    )
    monkeypatch.setattr("agent_hub.v2.egress.time.time", lambda: 200.0)
    second = prepare_egress(
        project_root=str(tmp_path),
        provider="gpt",
        model="gpt-5.6-sol",
        source_paths=["safe.txt"],
        policy_revision=0,
        estimated_max_tokens=100,
    )

    assert first["fact_pack"]["collected_at"] != second["fact_pack"]["collected_at"]
    assert first["manifest"]["fact_pack_sha256"] == second["manifest"]["fact_pack_sha256"]
    assert first["manifest"]["manifest_sha256"] == second["manifest"]["manifest_sha256"]


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


def test_egress_manifest_covers_redacted_artifact_content(tmp_path):
    content = "safe\napi_key = '123456789-secret-value'\n"
    proposal = prepare_egress(
        project_root=str(tmp_path),
        provider="gpt",
        model=None,
        source_paths=[],
        policy_revision=0,
        estimated_max_tokens=100,
        artifact_sources=[
            {
                "artifact_id": "art_fixture",
                "content": content,
                "content_sha256": sha256(content.encode()).hexdigest(),
                "sensitivity": "project",
            }
        ],
    )

    entry = proposal["manifest"]["entries"][0]
    transmitted = proposal["fact_pack"]["items"][0]["content"]
    assert entry["kind"] == "artifact"
    assert entry["artifact_id"] == "art_fixture"
    assert "secret-value" not in transmitted
    assert entry["secret_candidates_redacted"] == 1
