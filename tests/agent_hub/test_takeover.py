from __future__ import annotations

from copy import deepcopy
import json

from agent_hub import operations
from agent_hub.core import takeover
from orchestrate_codex import runner, store


def _start_fixed(tmp_path, monkeypatch, *, handoff: bool = False):
    project = tmp_path / "project"
    project.mkdir()
    if handoff:
        (project / "HANDOFF.md").write_text(
            "# HANDOFF\n\n## 다음 한 걸음\n\n- 원래 작업을 계속합니다.\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    started = runner.start_run(
        "direct_chat",
        args={
            "prompt": "private prompt",
            "handoff_mode": "required" if handoff else "off",
        },
        project_root=str(project),
    )
    return project, started


def _prepare(project, run_id):
    return operations.dispatch_tool(
        "agent_hub_prepare_takeover",
        {
            "project_root": str(project),
            "run_id": run_id,
        },
    )


def test_fixed_takeover_capsule_is_project_bound_and_redacted(tmp_path, monkeypatch):
    project, started = _start_fixed(tmp_path, monkeypatch)

    prepared = _prepare(project, started["run_id"])

    assert prepared["success"] is True
    capsule = prepared["data"]["capsule"]
    assert capsule["schema"] == "agent_hub_takeover_v1"
    assert capsule["project_root"] == str(project.resolve())
    assert capsule["expected_revision"] == started["store_revision"]
    assert capsule["action_id"] == started["next_action"]["action_id"]
    assert capsule["next_action"] == {
        "type": "call_tool",
        "stage_id": "chat",
        "tool": started["next_action"]["tool"],
        "action_id": started["next_action"]["action_id"],
    }
    serialized = json.dumps(prepared, ensure_ascii=False)
    assert "private prompt" not in serialized
    assert "arguments" not in capsule["next_action"]
    assert "claim_token" not in serialized
    assert "_lease" not in serialized

    other = tmp_path / "other"
    other.mkdir()
    denied = _prepare(other, started["run_id"])
    assert denied["success"] is False
    assert "different project" in denied["text"]


def test_fixed_takeover_revalidates_claims_and_commits_once(tmp_path, monkeypatch):
    project, started = _start_fixed(tmp_path, monkeypatch)
    capsule = _prepare(project, started["run_id"])["data"]["capsule"]

    resumed = operations.dispatch_tool(
        "agent_hub_resume_takeover",
        {
            "project_root": str(project),
            "capsule": capsule,
        },
    )

    assert resumed["success"] is True
    assert resumed["data"]["resume_mode"] == "claimed_fixed_action"
    assert resumed["data"]["state_mutated"] is True
    token = resumed["data"]["claim_token"]
    assert token not in json.dumps(capsule)
    assert resumed["data"]["action_id"] == capsule["action_id"]

    loaded = operations.dispatch_tool(
        "agent_hub_get_run",
        {"run_id": started["run_id"]},
    )
    listed = operations.dispatch_tool(
        "agent_hub_list_runs",
        {"project_root": str(project)},
    )
    assert token not in json.dumps(loaded)
    assert token not in json.dumps(listed)

    committed = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {
            "run_id": started["run_id"],
            "action_id": capsule["action_id"],
            "claim_token": token,
            "base_revision": resumed["data"]["base_revision"],
            "result_text": "done",
        },
    )
    assert committed["success"] is True
    assert committed["data"]["status"] == "completed"

    replay = operations.dispatch_tool(
        "agent_hub_resume_takeover",
        {
            "project_root": str(project),
            "capsule": capsule,
        },
    )
    assert replay["success"] is False
    assert "stale takeover capsule" in replay["text"]


def test_takeover_rejects_tampering_wrong_project_and_active_claim(
    tmp_path,
    monkeypatch,
):
    project, started = _start_fixed(tmp_path, monkeypatch)
    capsule = _prepare(project, started["run_id"])["data"]["capsule"]
    tampered = deepcopy(capsule)
    tampered["expected_revision"] = 99

    rejected = operations.dispatch_tool(
        "agent_hub_resume_takeover",
        {"project_root": str(project), "capsule": tampered},
    )
    assert rejected["success"] is False
    assert "digest mismatch" in rejected["text"]

    recomputed = deepcopy(capsule)
    recomputed["next_action"]["tool"] = "grok_codex_chat"
    unsigned = {
        key: value
        for key, value in recomputed.items()
        if key != "capsule_sha256"
    }
    recomputed["capsule_sha256"] = takeover._digest(unsigned)
    stale = operations.dispatch_tool(
        "agent_hub_resume_takeover",
        {"project_root": str(project), "capsule": recomputed},
    )
    assert stale["success"] is False
    assert "next_action changed" in stale["text"]

    other = tmp_path / "other"
    other.mkdir()
    wrong_project = operations.dispatch_tool(
        "agent_hub_resume_takeover",
        {"project_root": str(other), "capsule": capsule},
    )
    assert wrong_project["success"] is False
    assert "different project" in wrong_project["text"]

    claim = runner.claim_next_action(
        run_id=started["run_id"],
        expected_revision=started["store_revision"],
        action_id=started["next_action"]["action_id"],
    )
    try:
        busy = operations.dispatch_tool(
            "agent_hub_resume_takeover",
            {"project_root": str(project), "capsule": capsule},
        )
        assert busy["success"] is False
        assert busy["error"]["type"] == "run_lease_active"
        assert claim.store_claim.token not in json.dumps(busy)
    finally:
        runner.abort_action_claim(claim)


def test_takeover_refuses_handoff_drift_without_leaving_a_lease(
    tmp_path,
    monkeypatch,
):
    project, started = _start_fixed(tmp_path, monkeypatch, handoff=True)
    capsule = _prepare(project, started["run_id"])["data"]["capsule"]
    (project / "HANDOFF.md").write_text(
        "# HANDOFF\n\n## 다음 한 걸음\n\n- 바뀐 작업을 실행합니다.\n",
        encoding="utf-8",
    )

    paused = operations.dispatch_tool(
        "agent_hub_resume_takeover",
        {"project_root": str(project), "capsule": capsule},
    )

    assert paused["success"] is False
    assert paused["error"]["type"] == "handoff_drift"
    persisted = store.load_strict(started["run_id"])
    assert "_lease" not in persisted


def test_takeover_preflights_unbounded_or_deep_capsules(tmp_path, monkeypatch):
    project, started = _start_fixed(tmp_path, monkeypatch)
    capsule = _prepare(project, started["run_id"])["data"]["capsule"]

    oversized = deepcopy(capsule)
    oversized["next_action"]["tool"] = "x" * (
        takeover.MAX_CAPSULE_STRING_CHARS + 1
    )
    large_result = operations.dispatch_tool(
        "agent_hub_resume_takeover",
        {"project_root": str(project), "capsule": oversized},
    )
    assert large_result["success"] is False
    assert "string is too long" in large_result["text"]

    deeply_nested = deepcopy(capsule)
    nested = {}
    deeply_nested["next_action"]["extra"] = nested
    for _ in range(takeover.MAX_CAPSULE_DEPTH + 1):
        nested["child"] = {}
        nested = nested["child"]
    deep_result = operations.dispatch_tool(
        "agent_hub_resume_takeover",
        {"project_root": str(project), "capsule": deeply_nested},
    )
    assert deep_result["success"] is False
    assert "nested too deeply" in deep_result["text"]


def test_adaptive_takeover_returns_revision_fenced_continue_instruction(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    plan = {
        "schema": "agent_hub_plan_v1",
        "goal": "Answer once",
        "rationale": "One bounded step.",
        "steps": [
            {
                "id": "answer",
                "capability": "chat",
                "provider": "gpt",
                "depends_on": [],
                "fallback_providers": [],
                "instruction": "Answer.",
                "final": True,
            }
        ],
    }
    state = operations._new_adaptive_state(
        plan,
        {"project_root": str(project)},
        handoff_snapshot={},
    )
    persisted = store.create(state)
    capsule = _prepare(project, persisted["run_id"])["data"]["capsule"]

    assert capsule["run_kind"] == "adaptive"
    assert capsule["plan_sha256"]
    assert capsule["action_id"] is None
    assert capsule["next_action"]["pending_steps"] == ["answer"]

    resumed = operations.dispatch_tool(
        "agent_hub_resume_takeover",
        {"project_root": str(project), "capsule": capsule},
    )
    assert resumed["success"] is True
    assert resumed["data"]["resume_mode"] == "validated_adaptive_continue"
    assert resumed["data"]["state_mutated"] is False
    assert resumed["data"]["base_revision"] == persisted["store_revision"]
    assert resumed["data"]["action"]["arguments"] == {
        "run_id": persisted["run_id"],
        "expected_revision": persisted["store_revision"],
    }
    assert "claim_token" not in json.dumps(resumed)
