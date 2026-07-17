from __future__ import annotations

import json
import subprocess
from threading import Barrier

import pytest

from agent_hub import operations, orchestrator


def _plan():
    return {
        "schema": "agent_hub_plan_v1",
        "goal": "Produce an evidence-backed answer",
        "rationale": "Independent analysis followed by synthesis.",
        "steps": [
            {
                "id": "analyze_code",
                "capability": "chat",
                "provider": "claude",
                "depends_on": [],
                "fallback_providers": ["gemini"],
                "instruction": "Analyze the code evidence.",
                "final": False,
            },
            {
                "id": "challenge_assumptions",
                "capability": "chat",
                "provider": "grok",
                "depends_on": [],
                "fallback_providers": ["gemini"],
                "instruction": "Challenge unsupported assumptions.",
                "final": False,
            },
            {
                "id": "synthesize",
                "capability": "write",
                "provider": "gemini",
                "depends_on": ["analyze_code", "challenge_assumptions"],
                "fallback_providers": ["claude"],
                "instruction": "Synthesize the evidence and dissent.",
                "final": True,
            },
        ],
    }


def _policy_root(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Rules\n\n- Ground claims.\n", encoding="utf-8")
    return str(tmp_path)


def test_validator_accepts_llm_chosen_dag():
    plan = orchestrator.validate_plan(_plan())
    assert plan["final_step"] == "synthesize"
    assert plan["expected_max_calls"] == 6
    assert plan["plan_sha256"]


def test_validator_rejects_hallucinated_capability_from_planner():
    plan = _plan()
    plan["steps"][0]["capability"] = "security_scan"
    plan["steps"][0]["provider"] = "github_security_agent"
    with pytest.raises(ValueError, match="unsupported capability"):
        orchestrator.validate_plan(plan)


def test_validator_rejects_cycles_and_orphans():
    cyclic = _plan()
    cyclic["steps"][0]["depends_on"] = ["synthesize"]
    with pytest.raises(ValueError, match="cycle"):
        orchestrator.validate_plan(cyclic)

    orphan = _plan()
    orphan["steps"][2]["depends_on"] = ["analyze_code"]
    with pytest.raises(ValueError, match="do not feed"):
        orchestrator.validate_plan(orphan)


def test_scheduler_runs_dependency_frontier_in_parallel_then_synthesizes():
    barrier = Barrier(2)
    seen = []

    def invoke(step, provider, dependencies):
        seen.append((step["id"], provider, set(dependencies)))
        if not dependencies:
            barrier.wait(timeout=2)
            return {"success": True, "text": step["id"]}
        assert set(dependencies) == {"analyze_code", "challenge_assumptions"}
        return {"success": True, "text": "final answer"}

    result = orchestrator.execute_plan(_plan(), invoke=invoke, max_concurrency=2)
    assert result["success"] is True
    assert result["text"] == "final answer"
    assert set(result["waves"][0]["ready_steps"]) == {
        "analyze_code",
        "challenge_assumptions",
    }
    assert result["waves"][1]["ready_steps"] == ["synthesize"]


def test_scheduler_uses_fallback_and_fails_closed_before_dependents():
    called = []

    def fallback(step, provider, _dependencies):
        called.append((step["id"], provider))
        if step["id"] == "analyze_code" and provider == "claude":
            return {"success": False, "error": "claude unavailable"}
        return {"success": True, "text": "ok"}

    recovered = orchestrator.execute_plan(_plan(), invoke=fallback)
    assert recovered["success"] is True
    assert ("analyze_code", "gemini") in called

    def broken(step, provider, _dependencies):
        if step["id"] == "challenge_assumptions":
            return {"success": False, "error": f"{provider} unavailable"}
        return {"success": True, "text": "ok"}

    failed = orchestrator.execute_plan(_plan(), invoke=broken)
    assert failed["success"] is False
    assert failed["status"] == "failed"
    assert "challenge_assumptions" in failed["failed_steps"]
    assert "synthesize" in failed["blocked_steps"]
    assert "synthesize" not in failed["results"]


def test_adaptive_plan_uses_llm_then_local_validator(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)

    monkeypatch.setattr(
        operations,
        "_chat_raw",
        lambda provider, _arguments: {
            "success": True,
            "model": f"{provider}-planner",
            "text": json.dumps(_plan()),
            "consistency": {
                "policy_source": str(tmp_path / "AGENTS.md"),
                "policy_sha256": "policy-hash",
                "request_sha256": "request-hash",
            },
        },
    )
    result = operations.dispatch_tool(
        "agent_hub_plan_workflow",
        {
            "workflow_id": "adaptive",
            "prompt": "Analyze and synthesize",
            "project_root": root,
            "planner_provider": "gemini",
        },
    )
    assert result["success"] is True
    assert result["data"]["dynamic"] is True
    assert result["data"]["plan"]["planner"]["provider"] == "gemini"
    assert result["data"]["plan"]["planner"]["policy_sha256"] == "policy-hash"


def test_adaptive_planner_repairs_one_invalid_plan(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)
    responses = iter(
        [
            {
                "success": True,
                "text": '{"schema":"agent_hub_plan_v1","goal":"x","steps":[]}',
            },
            {"success": True, "text": json.dumps(_plan())},
        ]
    )
    monkeypatch.setattr(operations, "_chat_raw", lambda *_args, **_kwargs: next(responses))
    result = operations.dispatch_tool(
        "agent_hub_plan_workflow",
        {
            "workflow_id": "adaptive",
            "prompt": "Analyze and synthesize",
            "project_root": root,
            "planner_repair_attempts": 1,
        },
    )
    assert result["success"] is True
    assert result["data"]["plan"]["planner"]["attempts"] == 2
    assert result["data"]["plan"]["planner"]["attempt_log"][0]["success"] is False


def test_adaptive_run_executes_supplied_llm_plan(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)

    def planner_must_not_run(*_args, **_kwargs):
        raise AssertionError("a supplied reviewed plan must not call the planner")

    def fake_step(step, provider, dependencies, **_kwargs):
        return {
            "success": True,
            "provider": provider,
            "text": "final" if step["final"] else step["id"],
            "data": {"dependencies": sorted(dependencies)},
        }

    monkeypatch.setattr(operations, "_chat_raw", planner_must_not_run)
    monkeypatch.setattr(operations, "_adaptive_step_call", fake_step)
    result = operations.dispatch_tool(
        "agent_hub_run_workflow",
        {
            "workflow_id": "adaptive",
            "plan": _plan(),
            "project_root": root,
            "max_concurrency": 2,
        },
    )
    assert result["success"] is True
    assert result["text"] == "final"
    assert result["data"]["dynamic"] is True
    assert len(result["data"]["waves"]) == 2


def test_workflow_catalog_and_schema_expose_adaptive_mode():
    listed = operations.dispatch_tool("agent_hub_list_workflows", {})
    adaptive = next(item for item in listed["data"]["workflows"] if item["id"] == "adaptive")
    assert adaptive["dynamic"] is True
    explained = operations.dispatch_tool(
        "agent_hub_get_workflow", {"workflow_id": "adaptive"}
    )
    assert explained["data"]["schema"] == "agent_hub_plan_v1"
    planned = next(
        item
        for item in operations.tool_definitions()
        if item["name"] == "agent_hub_plan_workflow"
    )
    assert planned["annotations"]["readOnlyHint"] is False
    assert "planner_provider" in planned["inputSchema"]["properties"]
    assert len(operations.tool_definitions()) == 26


def test_adaptive_review_requires_a_completed_result(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True
    )
    (repo / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "new.py").write_text("VALUE = 1\n", encoding="utf-8")

    monkeypatch.setattr(
        operations,
        "_chat_raw",
        lambda *_args, **_kwargs: {
            "success": True,
            "text": "I need to inspect new.py first.",
            "warnings": [],
        },
    )
    incomplete = operations.dispatch_tool(
        "agent_hub_review_diff",
        {
            "provider": "claude",
            "cwd": str(repo),
            "require_complete": True,
            "include_untracked": True,
        },
    )
    assert incomplete["success"] is False
    assert "incomplete_review_output" in incomplete["warnings"]

    monkeypatch.setattr(
        operations,
        "_chat_raw",
        lambda *_args, **_kwargs: {
            "success": True,
            "text": "No findings in new.py.\n[AGENT_HUB_REVIEW_COMPLETE]",
            "warnings": [],
        },
    )
    complete = operations.dispatch_tool(
        "agent_hub_review_diff",
        {
            "provider": "claude",
            "cwd": str(repo),
            "require_complete": True,
            "include_untracked": True,
        },
    )
    assert complete["success"] is True
    assert complete["text"] == "No findings in new.py."
