from __future__ import annotations

import json

from agent_hub import operations
from orchestrate_codex import runner, store


def _state(run_id: str, project_root: str, *, created_at: float, status: str = "running"):
    return {
        "run_id": run_id,
        "run_kind": "fixed",
        "recipe_id": "direct_chat",
        "state_schema_version": 2,
        "store_revision": 0,
        "project_root": project_root,
        "created_at": created_at,
        "updated_at": created_at,
        "status": status,
        "cursor": 0,
        "steps": [
            {
                "id": "chat",
                "tool": "claude_codex_chat",
                "status": "pending",
            }
        ],
        "user_args": {"prompt": "private prompt"},
        "artifacts": {"draft": "private result"},
        "_handoff_snapshot": {
            "file_sha256": "a" * 64,
            "text": "private handoff",
        },
    }


def test_fixed_run_canonicalizes_project_root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))

    state = runner.start_run("direct_chat", args={"prompt": "hello"}, project_root=".")

    assert state["project_root"] == str(project.resolve())
    assert state["user_args"]["project_root"] == str(project.resolve())
    assert state["created_at"] == state["updated_at"]


def test_adaptive_run_stores_the_same_canonical_project_scope(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    state = operations._new_adaptive_state(
        {"schema": "adaptive_plan_v1", "steps": []},
        {"project_root": str(project)},
        handoff_snapshot={},
    )

    assert state["project_root"] == str(project.resolve())
    assert state["options"]["project_root"] == str(project.resolve())
    assert state["created_at"] == state["updated_at"]


def test_project_run_listing_is_exact_bounded_and_redacted(tmp_path, monkeypatch):
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))

    for run_id, root, created, status in (
        ("111111111111", project, 1.0, "running"),
        ("222222222222", project, 2.0, "completed"),
        ("333333333333", project, 3.0, "failed"),
        ("444444444444", other, 4.0, "running"),
    ):
        store.create(_state(run_id, str(root.resolve()), created_at=created, status=status))
    store.create(_state("555555555555", ".", created_at=5.0))
    (store.state_dir() / "666666666666.json").write_text("{broken", encoding="utf-8")

    first = store.list_run_summaries(str(project.resolve()), limit=2)

    assert [item["run_id"] for item in first["runs"]] == [
        "333333333333",
        "222222222222",
    ]
    assert first["next_cursor"]
    serialized = json.dumps(first)
    assert "private prompt" not in serialized
    assert "private result" not in serialized
    assert "private handoff" not in serialized
    assert first["skipped"]["unscoped"] >= 1
    assert first["skipped"]["corrupt"] >= 1
    assert all(item["project_root"] == str(project.resolve()) for item in first["runs"])

    second = store.list_run_summaries(
        str(project.resolve()),
        limit=2,
        cursor=first["next_cursor"],
    )
    assert [item["run_id"] for item in second["runs"]] == ["111111111111"]
    assert second["next_cursor"] is None

    completed = store.list_run_summaries(
        str(project.resolve()),
        status="completed",
    )
    assert [item["run_id"] for item in completed["runs"]] == ["222222222222"]


def test_project_run_listing_never_exposes_active_lease_token(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    store.create(_state("777777777777", str(project.resolve()), created_at=1.0))
    claim = store.claim("777777777777", expected_revision=0, lease_seconds=60)
    try:
        result = operations.dispatch_tool(
            "agent_hub_list_runs",
            {"project_root": str(project), "limit": 10},
        )
        assert result["success"] is True
        assert result["data"]["runs"][0]["lease_active"] is True
        assert claim.token not in json.dumps(result)
        assert "_lease" not in json.dumps(result)
    finally:
        store.abort_claim(claim)


def test_run_listing_cursor_is_bound_to_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    for index, run_id in enumerate(("888888888888", "999999999999"), start=1):
        store.create(
            _state(run_id, str(project.resolve()), created_at=float(index))
        )
    first = store.list_run_summaries(str(project.resolve()), limit=1)

    result = operations.dispatch_tool(
        "agent_hub_list_runs",
        {"project_root": str(other), "cursor": first["next_cursor"]},
    )

    assert result["success"] is False
    assert result["error"]["type"] == "ValueError"


def test_canonical_action_claim_commits_once_without_token_leak(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    started = runner.start_run(
        "direct_chat",
        args={"prompt": "hello"},
        project_root=str(project),
    )
    action = started["next_action"]

    claimed = operations.dispatch_tool(
        "agent_hub_claim_run_action",
        {
            "run_id": started["run_id"],
            "expected_revision": started["store_revision"],
            "action_id": action["action_id"],
        },
    )

    assert claimed["success"] is True
    token = claimed["data"]["claim_token"]
    loaded = operations.dispatch_tool(
        "agent_hub_get_run",
        {"run_id": started["run_id"]},
    )
    assert token not in json.dumps(loaded)
    assert "_lease" not in json.dumps(loaded)

    completed = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {
            "run_id": started["run_id"],
            "action_id": action["action_id"],
            "claim_token": token,
            "base_revision": claimed["data"]["base_revision"],
            "result_text": "done",
            "success": True,
        },
    )
    assert completed["success"] is True
    assert completed["data"]["status"] == "completed"

    replay = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {
            "run_id": started["run_id"],
            "action_id": action["action_id"],
            "claim_token": token,
            "base_revision": claimed["data"]["base_revision"],
            "result_text": "replacement",
        },
    )
    assert replay["success"] is False
    assert replay["error"]["type"] == "run_lease_lost"
