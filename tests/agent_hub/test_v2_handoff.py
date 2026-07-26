from __future__ import annotations

from pathlib import Path
import subprocess

from agent_hub.core import handoff
from agent_hub.v2.contracts import PLAN_SCHEMA, TASK_SCHEMA, validate_plan
from agent_hub.v2.crypto import ArtifactCipher, StaticKeyProvider
from agent_hub.v2.service import HubService
from agent_hub.v2.store import HubStore


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _body(label: str) -> str:
    return (
        f"- **원래 목표**: {label} handoff를 안전하게 유지합니다.\n"
        "- **현재 단계**: SHA fence 적용을 준비했습니다.\n"
        "- **완료**: 결정적 검증을 완료했습니다.\n"
        "- **미완**: 최종 apply만 남았습니다.\n"
        "- **변경 파일**: `HANDOFF.md`\n"
        "- **검증 실행 결과**: 준비 결과가 유효합니다.\n"
        "- **현재 리스크**: 외부 수정은 충돌해야 합니다.\n"
        "- **Do-Not-Repeat**: SHA 검증을 우회하지 마세요.\n"
        "- **다음 한 걸음**: `HANDOFF.md` 준비 결과를 apply하세요.\n"
    )


def _service(tmp_path: Path) -> HubService:
    return HubService(
        HubStore(tmp_path / "state.sqlite3"),
        cipher=ArtifactCipher(StaticKeyProvider(b"h" * 32)),
    )


def test_core_handoff_prepare_apply_is_sha_fenced(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    target = repo / "HANDOFF.md"
    target.write_text("# Handoff\n", encoding="utf-8")

    prepared = handoff.prepare_handoff_update(repo, body=_body("V2"))
    target.write_text("# Concurrent edit\n", encoding="utf-8")

    try:
        handoff.apply_handoff_update(
            repo,
            file=prepared["target"],
            content=prepared["content"],
            expected_sha256=prepared["expected_sha256"],
        )
    except handoff.HandoffRevisionConflict:
        pass
    else:
        raise AssertionError("stale HANDOFF update must be rejected")


def test_v2_handoff_service_prepares_and_applies_managed_block(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    target = repo / "HANDOFF.md"
    target.write_text("# Project handoff\n", encoding="utf-8")
    service = _service(tmp_path)

    prepared = service.dispatch(
        "agent_hub_handoff",
        {
            "action": "prepare_update",
            "project_root": str(repo),
            "arguments": {"body": _body("Service")},
        },
    )

    assert prepared["success"] is True
    proposal = prepared["data"]
    assert proposal["quality"]["valid"] is True
    applied = service.dispatch(
        "agent_hub_handoff",
        {
            "action": "apply_update",
            "project_root": str(repo),
            "arguments": {
                "file": proposal["target"],
                "content": proposal["content"],
                "expected_sha256": proposal["expected_sha256"],
            },
        },
    )
    assert applied["success"] is True
    assert handoff.START_MARKER in target.read_text(encoding="utf-8")


def test_v2_handoff_managed_revision_conflict_is_safe_and_actionable(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    target = repo / "HANDOFF.md"
    target.write_text("# Project handoff\n", encoding="utf-8")
    service = _service(tmp_path)

    result = service.dispatch(
        "agent_hub_handoff",
        {
            "action": "prepare_update",
            "project_root": str(repo),
            "arguments": {
                "body": _body("Conflict"),
                "base_managed_sha256": "0" * 64,
            },
        },
    )

    assert result["success"] is False
    assert result["error"]["code"] == "handoff_revision_conflict"
    assert result["error"]["scope"] == "handoff"
    assert result["error"]["retryable"] is True
    assert result["error"]["safe_details"] == {
        "revision_kind": "managed_block",
        "expected_sha256": "0" * 64,
        "current_sha256": None,
    }
    assert result["error"]["next_action"] == {
        "type": "call_tool",
        "tool": "agent_hub_handoff",
        "action": "prepare_update",
    }


def test_v2_takeover_capsule_contains_only_run_and_digest_identity(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    service = _service(tmp_path)
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Prepare a capsule.",
                "capability": "chat",
                "inline_input": "",
                "constraints": {"provider_allowlist": ["gpt"]},
                "retention": "ephemeral",
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
    run = service.store.create_run(
        plan=plan,
        project_root=str(repo),
        idempotency_key="takeover-fixture",
    )

    result = service.dispatch(
        "agent_hub_handoff",
        {
            "action": "takeover",
            "project_root": str(repo),
            "arguments": {"run_id": run["run_id"]},
        },
    )

    capsule = result["data"]["capsule"]
    assert capsule["schema"] == "agent_hub_takeover_capsule_v2"
    assert capsule["run_id"] == run["run_id"]
    assert len(capsule["plan_sha256"]) == 64
    assert len(capsule["capsule_sha256"]) == 64
    assert "prompt" not in str(capsule).lower()
    assert "content" not in str(capsule).lower()
