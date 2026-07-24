from __future__ import annotations

import json

from agent_hub import operations
from agent_hub.core import handoff
from orchestrate_codex import events, runner, store


def _project_and_run(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    started = runner.start_run(
        "direct_chat",
        args={"prompt": "private prompt sk-example-secret-value"},
        project_root=str(project),
    )
    return project, started


def _read(project, run_id, **extra):
    return operations.dispatch_tool(
        "agent_hub_get_run_events",
        {
            "project_root": str(project),
            "run_id": run_id,
            **extra,
        },
    )


def test_fixed_events_are_committed_only_and_redacted(tmp_path, monkeypatch):
    project, started = _project_and_run(tmp_path, monkeypatch)
    created = _read(project, started["run_id"])

    assert created["success"] is True
    assert [item["type"] for item in created["data"]["events"]] == ["run_created"]
    creation = created["data"]["events"][0]
    assert creation["seq"] == 1
    assert creation["base_revision"] is None
    assert creation["resulting_revision"] == 0
    assert creation["prompt_chars"] > 0
    assert len(creation["prompt_sha256"]) == 64
    assert "private prompt" not in json.dumps(created)
    assert "sk-example-secret-value" not in json.dumps(created)

    action = started["next_action"]
    claimed = operations.dispatch_tool(
        "agent_hub_claim_run_action",
        {
            "run_id": started["run_id"],
            "expected_revision": 0,
            "action_id": action["action_id"],
        },
    )
    token = claimed["data"]["claim_token"]
    while_claimed = _read(project, started["run_id"])
    assert [item["type"] for item in while_claimed["data"]["events"]] == [
        "run_created"
    ]

    committed = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {
            "run_id": started["run_id"],
            "action_id": action["action_id"],
            "claim_token": token,
            "base_revision": 0,
            "result_text": "private result sk-another-secret-value",
            "leaf_result": {
                "success": True,
                "provider": "gpt",
                "model": "gpt-5.6-sol",
                "usage": {"input_tokens": 12, "output_tokens": 7},
            },
        },
    )
    assert committed["success"] is True
    assert "events" not in committed["data"]
    assert committed["data"]["event_journal"] == {
        "schema": "agent_hub_run_event_summary_v1",
        "retained": 2,
        "latest_seq": 2,
        "dropped": 0,
    }

    journal = _read(project, started["run_id"])
    assert [item["type"] for item in journal["data"]["events"]] == [
        "run_created",
        "action_completed",
    ]
    action_event = journal["data"]["events"][1]
    assert action_event["base_revision"] == 0
    assert action_event["resulting_revision"] == 1
    assert action_event["action_id"] == action["action_id"]
    assert action_event["provider"] == "gpt"
    assert action_event["model"] == "gpt-5.6-sol"
    assert action_event["usage"] == {"input_tokens": 12, "output_tokens": 7}
    serialized = json.dumps(journal)
    assert "private result" not in serialized
    assert "sk-another-secret-value" not in serialized
    assert token not in serialized


def test_failed_action_event_records_category_without_error_text(
    tmp_path,
    monkeypatch,
):
    project, started = _project_and_run(tmp_path, monkeypatch)
    action = started["next_action"]
    claimed = operations.dispatch_tool(
        "agent_hub_claim_run_action",
        {
            "run_id": started["run_id"],
            "expected_revision": 0,
            "action_id": action["action_id"],
        },
    )
    secret_error = "provider unavailable authorization=private-secret"
    failed = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {
            "run_id": started["run_id"],
            "action_id": action["action_id"],
            "claim_token": claimed["data"]["claim_token"],
            "base_revision": 0,
            "success": False,
            "error": secret_error,
        },
    )
    assert failed["success"] is True

    journal = _read(project, started["run_id"])
    event = journal["data"]["events"][-1]
    assert event["type"] == "action_failed"
    assert event["success"] is False
    assert event["retryable"] is True
    assert event["error_type"]
    assert secret_error not in json.dumps(event)


def test_adaptive_commit_records_completed_steps_without_result_body(
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
        handoff_snapshot=handoff.load_handoff(
            str(project),
            mode="off",
        ),
    )
    persisted = store.create(state)
    monkeypatch.setattr(
        operations,
        "_adaptive_step_call",
        lambda *_args, **_kwargs: {
            "success": True,
            "text": "private adaptive result",
            "provider": "gpt",
            "model": "gpt-5.6-sol",
        },
    )

    completed = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {
            "run_id": persisted["run_id"],
            "expected_revision": 0,
        },
    )
    assert completed["success"] is True
    assert completed["data"]["status"] == "completed"

    journal = _read(project, persisted["run_id"])
    assert [item["type"] for item in journal["data"]["events"]] == [
        "run_created",
        "workflow_completed",
    ]
    event = journal["data"]["events"][-1]
    assert event["completed_step_ids"] == ["answer"]
    assert event["pending_steps"] == 0
    assert event["leaf_calls"] >= 0
    assert "private adaptive result" not in json.dumps(journal)


def test_event_journal_prunes_and_reports_pagination_gaps(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    state = {
        "run_id": "123456789abc",
        "run_kind": "fixed",
        "recipe_id": "direct_chat",
        "project_root": str(project.resolve()),
        "store_revision": 0,
        "status": "running",
        "created_at": 1.0,
        "updated_at": 1.0,
        "steps": [],
    }
    for index in range(events.MAX_EVENTS + 5):
        events.append_event(
            state,
            "test_event",
            at=float(index + 1),
            base_revision=index if index else None,
            resulting_revision=index,
            status="running",
        )
    store.create(state)

    first = _read(project, state["run_id"], limit=100)
    assert first["success"] is True
    assert len(first["data"]["events"]) == 100
    assert first["data"]["oldest_available_seq"] == 6
    assert first["data"]["latest_seq"] == events.MAX_EVENTS + 5
    assert first["data"]["events_dropped"] == 5
    assert first["data"]["gap"] is True
    assert first["data"]["has_more"] is True

    second = _read(
        project,
        state["run_id"],
        after_seq=first["data"]["next_after_seq"],
        limit=100,
    )
    assert len(second["data"]["events"]) == 100
    assert second["data"]["has_more"] is False

    other = tmp_path / "other"
    other.mkdir()
    denied = _read(other, state["run_id"])
    assert denied["success"] is False
    assert "different project" in denied["text"]
