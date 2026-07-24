from __future__ import annotations

import json
import os

import pytest

from agent_hub import operations, orchestrator
from agent_hub.core import handoff
from orchestrate_codex import runner, store


def _start_fixed(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    started = runner.start_run(
        "direct_chat",
        args={
            "prompt": "private prompt sk-example-secret-value",
            "handoff_mode": "off",
        },
        project_root=str(project),
    )
    return project, started


def _cancel(project, run_id, revision, *, reason="user_requested"):
    return operations.dispatch_tool(
        "agent_hub_cancel_run",
        {
            "project_root": str(project),
            "run_id": run_id,
            "expected_revision": revision,
            "reason_code": reason,
        },
    )


def _archive(project, run_id, revision):
    return operations.dispatch_tool(
        "agent_hub_archive_run",
        {
            "project_root": str(project),
            "run_id": run_id,
            "expected_revision": revision,
        },
    )


def _gc(project, run_id, **extra):
    return operations.dispatch_tool(
        "agent_hub_gc_run",
        {
            "project_root": str(project),
            "run_id": run_id,
            **extra,
        },
    )


def _events(project, run_id):
    return operations.dispatch_tool(
        "agent_hub_get_run_events",
        {
            "project_root": str(project),
            "run_id": run_id,
        },
    )


def test_cancel_is_revision_fenced_idempotent_and_redacted(tmp_path, monkeypatch):
    project, started = _start_fixed(tmp_path, monkeypatch)

    cancelled = _cancel(
        project,
        started["run_id"],
        started["store_revision"],
        reason="superseded",
    )

    assert cancelled["success"] is True
    assert cancelled["data"]["status"] == "cancelled"
    assert cancelled["data"]["previous_status"] == "running"
    assert cancelled["data"]["store_revision"] == 1
    assert cancelled["data"]["changed"] is True
    assert cancelled["data"]["reason_code"] == "superseded"

    replay = _cancel(project, started["run_id"], 1, reason="superseded")
    assert replay["success"] is True
    assert replay["data"]["changed"] is False
    assert replay["data"]["store_revision"] == 1

    journal = _events(project, started["run_id"])
    assert [item["type"] for item in journal["data"]["events"]] == [
        "run_created",
        "run_cancelled",
    ]
    event = journal["data"]["events"][-1]
    assert event["base_revision"] == 0
    assert event["resulting_revision"] == 1
    assert event["previous_status"] == "running"
    assert event["reason_code"] == "superseded"
    serialized = json.dumps(journal)
    assert "private prompt" not in serialized
    assert "sk-example-secret-value" not in serialized

    loaded = operations.dispatch_tool(
        "agent_hub_get_run",
        {"run_id": started["run_id"]},
    )
    assert loaded["data"]["done"] is True
    assert loaded["data"]["next_action"]["type"] == "failed"
    assert "expected_revision" not in loaded["data"]["next_action"]

    continued = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {
            "run_id": started["run_id"],
            "expected_revision": 1,
        },
    )
    assert continued["success"] is False
    assert continued["data"]["status"] == "cancelled"
    assert store.load_strict(started["run_id"])["store_revision"] == 1


def test_cancel_invalidates_an_active_claim_and_fences_late_result(
    tmp_path,
    monkeypatch,
):
    project, started = _start_fixed(tmp_path, monkeypatch)
    claimed = runner.claim_next_action(
        run_id=started["run_id"],
        expected_revision=0,
        action_id=started["next_action"]["action_id"],
    )

    cancelled = _cancel(project, started["run_id"], 0)

    assert cancelled["success"] is True
    persisted = store.load_strict(started["run_id"])
    assert persisted["status"] == "cancelled"
    assert persisted["store_revision"] == 1
    assert "_lease" not in persisted
    with pytest.raises(store.RunLeaseLost):
        runner.commit_claimed_action(
            claimed,
            result_text="late provider result",
            leaf_result={
                "success": True,
                "provider": "gpt",
                "model": "gpt-5.6-sol",
            },
        )
    assert [
        item["type"] for item in _events(project, started["run_id"])["data"]["events"]
    ] == ["run_created", "run_cancelled"]


def test_cancel_rejects_stale_wrong_project_and_terminal_state(
    tmp_path,
    monkeypatch,
):
    project, started = _start_fixed(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()

    stale = _cancel(project, started["run_id"], 1)
    assert stale["success"] is False
    assert stale["error"]["type"] == "run_revision_conflict"
    wrong_project = _cancel(other, started["run_id"], 0)
    assert wrong_project["success"] is False
    assert "different project" in wrong_project["text"]
    assert store.load_strict(started["run_id"])["store_revision"] == 0

    claimed = runner.claim_next_action(
        run_id=started["run_id"],
        expected_revision=0,
        action_id=started["next_action"]["action_id"],
    )
    completed = runner.commit_claimed_action(
        claimed,
        result_text="done",
        leaf_result={"success": True, "provider": "gpt"},
    )
    assert completed["status"] == "completed"

    terminal = _cancel(project, started["run_id"], 1)
    assert terminal["success"] is False
    assert "cannot be cancelled" in terminal["text"]
    assert store.load_strict(started["run_id"])["status"] == "completed"


def test_archive_requires_terminal_state_and_is_idempotent(tmp_path, monkeypatch):
    project, started = _start_fixed(tmp_path, monkeypatch)

    active = _archive(project, started["run_id"], 0)
    assert active["success"] is False
    assert "cannot be archived" in active["text"]

    assert _cancel(project, started["run_id"], 0)["success"] is True
    archived = _archive(project, started["run_id"], 1)
    assert archived["success"] is True
    assert archived["data"]["status"] == "archived"
    assert archived["data"]["previous_status"] == "cancelled"
    assert archived["data"]["store_revision"] == 2
    assert archived["data"]["changed"] is True

    replay = _archive(project, started["run_id"], 2)
    assert replay["success"] is True
    assert replay["data"]["changed"] is False
    assert replay["data"]["store_revision"] == 2
    assert [item["type"] for item in _events(project, started["run_id"])["data"]["events"]] == [
        "run_created",
        "run_cancelled",
        "run_archived",
    ]

    takeover = operations.dispatch_tool(
        "agent_hub_prepare_takeover",
        {
            "project_root": str(project),
            "run_id": started["run_id"],
        },
    )
    assert takeover["success"] is False
    assert "terminal run" in takeover["text"]


def test_adaptive_cancel_prevents_provider_dispatch(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    plan = {
        "schema": "agent_hub_plan_v1",
        "goal": "Do not dispatch after cancellation",
        "rationale": "Lifecycle fence test.",
        "steps": [
            {
                "id": "answer",
                "capability": "chat",
                "provider": "gpt",
                "depends_on": [],
                "fallback_providers": [],
                "instruction": "Answer.",
                "reasoning_effort": "medium",
                "final": True,
            }
        ],
    }
    state = operations._new_adaptive_state(
        plan,
        {
            "project_root": str(project),
            "prompt": "private adaptive prompt",
        },
        handoff_snapshot=handoff.load_handoff(str(project), mode="off"),
    )
    persisted = store.create(state)
    assert _cancel(project, persisted["run_id"], 0)["success"] is True

    calls = []
    monkeypatch.setattr(
        operations,
        "_adaptive_step_call",
        lambda *_args, **_kwargs: calls.append(True),
    )
    continued = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {
            "run_id": persisted["run_id"],
            "expected_revision": 1,
        },
    )

    assert continued["success"] is False
    assert continued["data"]["status"] == "cancelled"
    assert calls == []
    assert store.load_strict(persisted["run_id"])["store_revision"] == 1


@pytest.mark.parametrize(
    ("planner_status", "expected_error"),
    [
        ("blocked", "adaptive_blocked"),
        ("unexpected", "adaptive_invalid_status"),
    ],
)
def test_adaptive_unknown_terminal_results_fail_closed(
    tmp_path,
    monkeypatch,
    planner_status,
    expected_error,
):
    project = tmp_path / planner_status
    project.mkdir()
    monkeypatch.setenv(
        "ORCHESTRATE_CODEX_STATE_DIR",
        str(tmp_path / f"runs-{planner_status}"),
    )
    plan = {
        "schema": "agent_hub_plan_v1",
        "goal": "Fail closed",
        "rationale": "Status normalization test.",
        "steps": [],
    }
    state = operations._new_adaptive_state(
        plan,
        {"project_root": str(project), "prompt": "private"},
        handoff_snapshot=handoff.load_handoff(str(project), mode="off"),
    )
    persisted = store.create(state)
    monkeypatch.setattr(
        orchestrator,
        "execute_plan",
        lambda *_args, **_kwargs: {
            "status": planner_status,
            "error": "",
            "results": {},
            "waves": [],
            "leaf_calls": 0,
        },
    )

    result = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {"run_id": persisted["run_id"], "expected_revision": 0},
    )

    assert result["success"] is False
    assert result["data"]["status"] == "failed"
    assert result["data"]["error"] == expected_error
    assert store.load_strict(persisted["run_id"])["status"] == "failed"


def test_gc_is_dry_run_by_default_and_requires_revision_and_digest(
    tmp_path,
    monkeypatch,
):
    project, started = _start_fixed(tmp_path, monkeypatch)
    assert _cancel(project, started["run_id"], 0)["success"] is True

    nonarchived = _gc(project, started["run_id"])
    assert nonarchived["success"] is False
    assert "only archived runs" in nonarchived["text"]

    assert _archive(project, started["run_id"], 1)["success"] is True
    plan = _gc(project, started["run_id"])
    assert plan["success"] is True
    assert plan["data"]["deleted"] is False
    assert plan["data"]["apply_required"] is True
    assert plan["data"]["store_revision"] == 2
    assert len(plan["data"]["state_sha256"]) == 64
    state_path = store.state_dir() / f"{started['run_id']}.json"
    lock_path = store.state_dir() / f".{started['run_id']}.lock"
    assert state_path.exists()

    missing_fences = _gc(project, started["run_id"], apply=True)
    assert missing_fences["success"] is False
    wrong_revision = _gc(
        project,
        started["run_id"],
        apply=True,
        expected_revision=1,
        expected_state_sha256=plan["data"]["state_sha256"],
    )
    assert wrong_revision["success"] is False
    assert wrong_revision["error"]["type"] == "run_revision_conflict"
    wrong_digest = _gc(
        project,
        started["run_id"],
        apply=True,
        expected_revision=2,
        expected_state_sha256="0" * 64,
    )
    assert wrong_digest["success"] is False
    assert wrong_digest["error"]["type"] == "run_state_digest_conflict"
    assert state_path.exists()

    runner._RUNS[started["run_id"]] = store.load_strict(started["run_id"])
    deleted = _gc(
        project,
        started["run_id"],
        apply=True,
        expected_revision=2,
        expected_state_sha256=plan["data"]["state_sha256"],
    )
    assert deleted["success"] is True
    assert deleted["data"]["deleted"] is True
    assert not state_path.exists()
    assert lock_path.exists()
    assert started["run_id"] not in runner._RUNS
    assert operations.dispatch_tool(
        "agent_hub_get_run",
        {"run_id": started["run_id"]},
    )["success"] is False
    assert _events(project, started["run_id"])["success"] is False


def test_gc_rejects_wrong_project_and_hardlinked_state(tmp_path, monkeypatch):
    project, started = _start_fixed(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    assert _cancel(project, started["run_id"], 0)["success"] is True
    assert _archive(project, started["run_id"], 1)["success"] is True
    plan = _gc(project, started["run_id"])["data"]

    wrong_project = _gc(
        other,
        started["run_id"],
        apply=True,
        expected_revision=2,
        expected_state_sha256=plan["state_sha256"],
    )
    assert wrong_project["success"] is False
    assert "different project" in wrong_project["text"]

    state_path = store.state_dir() / f"{started['run_id']}.json"
    hardlink = store.state_dir() / "hardlink-copy.json"
    os.link(state_path, hardlink)
    try:
        rejected = _gc(
            project,
            started["run_id"],
            apply=True,
            expected_revision=2,
            expected_state_sha256=plan["state_sha256"],
        )
        assert rejected["success"] is False
        assert "hard-linked" in rejected["text"]
        assert state_path.exists()
    finally:
        hardlink.unlink()


def test_run_lifecycle_tool_annotations_are_explicit():
    specs = {item["name"]: item for item in operations.tool_definitions()}

    for name in ("agent_hub_cancel_run", "agent_hub_archive_run"):
        assert specs[name]["annotations"] == {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    assert specs["agent_hub_gc_run"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    }
