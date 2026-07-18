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
    assert all(step["reasoning_effort"] == "medium" for step in plan["steps"])


def test_validator_accepts_codebase_depth_and_rejects_invalid_effort():
    plan = _plan()
    plan["steps"][0].update(
        {
            "capability": "inspect_codebase",
            "reasoning_effort": "high",
            "investigation_depth": "deep",
        }
    )
    normalized = orchestrator.validate_plan(plan)
    first = normalized["steps"][0]
    assert first["reasoning_effort"] == "high"
    assert first["investigation_depth"] == "deep"

    plan["steps"][0]["reasoning_effort"] = "maximum"
    with pytest.raises(ValueError, match="reasoning_effort"):
        orchestrator.validate_plan(plan)

    plan = _plan()
    plan["steps"][0]["investigation_depth"] = "deep"
    with pytest.raises(ValueError, match="only valid for inspect_codebase"):
        orchestrator.validate_plan(plan)


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
    captured = {}

    def fake_chat(provider, arguments):
        captured.update(arguments)
        return {
            "success": True,
            "model": f"{provider}-planner",
            "text": json.dumps(_plan()),
            "consistency": {
                "policy_source": str(tmp_path / "AGENTS.md"),
                "policy_sha256": "policy-hash",
                "request_sha256": "request-hash",
            },
        }

    monkeypatch.setattr(operations, "_chat_raw", fake_chat)
    result = operations.dispatch_tool(
        "agent_hub_plan_workflow",
        {
            "workflow_id": "adaptive",
            "prompt": "Analyze and synthesize",
            "project_root": root,
            "planner_provider": "gemini",
            "per_call_timeout": 600,
            "workflow_timeout": 290,
        },
    )
    assert result["success"] is True
    assert result["data"]["dynamic"] is True
    assert result["data"]["plan"]["planner"]["provider"] == "gemini"
    assert result["data"]["plan"]["planner"]["policy_sha256"] == "policy-hash"
    assert result["data"]["plan"]["goal"] == "Analyze and synthesize"
    assert captured["timeout_sec"] == 280


def test_adaptive_planner_cannot_shorten_the_reviewed_goal(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)
    shortened = _plan()
    shortened["goal"] = "short summary"
    monkeypatch.setattr(
        operations,
        "_chat_raw",
        lambda *_args, **_kwargs: {"success": True, "text": json.dumps(shortened)},
    )

    original = "Rewrite README with every supplied repository fact and installation command."
    result = operations.dispatch_tool(
        "agent_hub_plan_workflow",
        {
            "workflow_id": "adaptive",
            "prompt": original,
            "project_root": root,
        },
    )

    assert result["success"] is True
    assert result["data"]["plan"]["goal"] == original


def test_adaptive_context_requires_a_completed_current_response():
    context = operations._adaptive_context(
        "Write the README.",
        {"instruction": "Review its structure."},
        {},
    )

    assert "Complete this step in the current response" in context
    assert "do not announce, request, or defer" in context


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


def test_adaptive_write_infers_durable_task_from_goal(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)
    captured = {}

    def fake_write(arguments):
        captured.update(arguments)
        return operations.envelope(
            "write",
            {"success": True, "text": "# README\n\nComplete documentation."},
            provider="claude",
        )

    monkeypatch.setattr(operations, "_write", fake_write)
    result = operations._adaptive_step_call(
        {
            "id": "write_readme",
            "capability": "write",
            "provider": "claude",
            "depends_on": ["inspect_repo"],
            "fallback_providers": [],
            "instruction": "Write the final document.",
            "reasoning_effort": "high",
            "final": True,
        },
        "claude",
        {"inspect_repo": {"success": True, "text": "Evidence"}},
        args={"project_root": root},
        goal="Rewrite the repository README from verified evidence.",
    )

    assert result["success"] is True
    assert captured["task"] == "auto"
    built = operations.google_writing.build_prompt(captured)
    assert built["task"] == "readme"
    assert built["doc_class"] == "durable"


def test_scheduler_fails_with_structured_timeout_before_next_wave(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: now[0])
    plan = _plan()
    plan["steps"] = [plan["steps"][0], plan["steps"][2]]
    plan["steps"][1]["depends_on"] = ["analyze_code"]

    def invoke(step, _provider, _dependencies):
        now[0] = 10.0
        return {"success": True, "text": step["id"]}

    result = orchestrator.execute_plan(plan, invoke=invoke, max_elapsed_seconds=5)

    assert result["success"] is False
    assert result["status"] == "timed_out"
    assert result["error"] == "workflow_timeout_exceeded"
    assert result["blocked_steps"] == ["synthesize"]


def test_adaptive_run_clamps_each_call_to_remaining_workflow_budget(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)
    captured = {}
    plan = _plan()
    plan["steps"] = [plan["steps"][2]]
    plan["steps"][0]["depends_on"] = []

    def fake_step(step, provider, dependencies, **kwargs):
        captured.update(kwargs["args"])
        return {"success": True, "provider": provider, "text": "done", "data": {}}

    monkeypatch.setattr(operations, "_adaptive_step_call", fake_step)
    result = operations.dispatch_tool(
        "agent_hub_run_workflow",
        {
            "workflow_id": "adaptive",
            "plan": plan,
            "project_root": root,
            "per_call_timeout": 180,
            "workflow_timeout": 30,
        },
    )

    assert result["success"] is True
    assert 5 <= captured["per_call_timeout"] <= 30
    assert result["data"]["workflow_timeout"] == 30


def test_adaptive_run_schema_exposes_end_to_end_timeout():
    spec = next(
        item for item in operations.tool_definitions() if item["name"] == "agent_hub_run_workflow"
    )
    timeout = spec["inputSchema"]["properties"]["workflow_timeout"]
    assert timeout["default"] == 270
    assert timeout["maximum"] == 290


def test_adaptive_supervised_run_persists_and_resumes_one_wave_per_call(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = _policy_root(repo)
    state_dir = tmp_path / "runs"
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(state_dir))

    def fake_step(step, provider, dependencies, **_kwargs):
        return {
            "success": True,
            "provider": provider,
            "text": "final" if step["final"] else step["id"],
            "data": {"dependencies": sorted(dependencies)},
        }

    monkeypatch.setattr(operations, "_adaptive_step_call", fake_step)
    started = operations.dispatch_tool(
        "agent_hub_start_workflow",
        {
            "workflow_id": "adaptive",
            "plan": _plan(),
            "project_root": root,
            "workflow_timeout": 290,
        },
    )

    assert started["success"] is True
    assert started["data"]["status"] == "paused"
    run_id = started["data"]["run_id"]
    assert (state_dir / f"{run_id}.json").is_file()

    loaded_before_continue = operations.dispatch_tool(
        "agent_hub_get_run", {"run_id": run_id}
    )
    first = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {"state": loaded_before_continue["data"]},
    )
    assert first["success"] is True
    assert first["text"] == (
        "Adaptive workflow paused safely; call continue for the next wave."
    )
    assert first["data"]["status"] == "paused"
    assert set(first["data"]["results"]) == {
        "analyze_code",
        "challenge_assumptions",
    }
    assert first["data"]["pending_steps"] == ["synthesize"]

    second = operations.dispatch_tool(
        "agent_hub_continue_workflow", {"run_id": run_id}
    )
    assert second["success"] is True
    assert second["data"]["status"] == "completed"
    assert second["text"] == "final"
    loaded = operations.dispatch_tool("agent_hub_get_run", {"run_id": run_id})
    assert loaded["data"]["done"] is True
    assert loaded["data"]["status"] == "completed"


def test_adaptive_start_and_continue_schemas_expose_resumable_controls():
    specs = {item["name"]: item for item in operations.tool_definitions()}
    start_props = specs["agent_hub_start_workflow"]["inputSchema"]["properties"]
    continue_props = specs["agent_hub_continue_workflow"]["inputSchema"]["properties"]

    assert start_props["workflow_timeout"]["default"] == 270
    assert continue_props["workflow_timeout"]["maximum"] == 290
    assert continue_props["max_waves_per_call"]["default"] == 1


def test_end_to_end_timeout_returns_a_persisted_resume_run(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = _policy_root(repo)
    state_dir = tmp_path / "runs"
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(state_dir))
    monkeypatch.setattr(
        orchestrator,
        "execute_plan",
        lambda *_args, **_kwargs: {
            "success": False,
            "status": "timed_out",
            "error": "workflow_timeout_exceeded",
            "text": "budget exhausted",
            "results": {
                "analyze_code": {
                    "success": True,
                    "provider": "claude",
                    "text": "evidence",
                }
            },
            "waves": [],
            "leaf_calls": 1,
        },
    )

    result = operations.dispatch_tool(
        "agent_hub_run_workflow",
        {"workflow_id": "adaptive", "plan": _plan(), "project_root": root},
    )

    assert result["success"] is False
    assert result["data"]["resumable"] is True
    run_id = result["data"]["run_id"]
    loaded = operations.dispatch_tool("agent_hub_get_run", {"run_id": run_id})
    assert loaded["data"]["status"] == "paused"
    assert loaded["data"]["pause_reason"] == "workflow_timeout_exceeded"
    assert set(loaded["data"]["results"]) == {"analyze_code"}


def test_adaptive_run_fails_closed_when_resume_state_cannot_be_persisted(monkeypatch):
    monkeypatch.setattr(operations.store, "save", lambda _state: None)
    monkeypatch.setattr(operations.store, "load", lambda _run_id: None)

    with pytest.raises(RuntimeError, match="could not be persisted"):
        operations._save_adaptive_state({"run_id": "unwritable", "plan": _plan()})


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
    assert "models" in planned["inputSchema"]["properties"]
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


def test_adaptive_step_uses_explicit_provider_model_map(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)
    captured = {}

    def fake_chat(arguments):
        captured.update(arguments)
        return operations.envelope(
            "chat",
            {"success": True, "text": "ok", "model": arguments.get("model")},
            provider=arguments["provider"],
        )

    monkeypatch.setattr(operations, "_chat", fake_chat)
    result = operations._adaptive_step_call(
        {
            "id": "frontier_review",
            "capability": "chat",
            "provider": "claude",
            "depends_on": [],
            "fallback_providers": [],
            "instruction": "Review the evidence.",
            "final": True,
        },
        "claude",
        {},
        args={
            "project_root": root,
            "models": {"claude": "claude-opus-4-8"},
            "max_tokens": 65536,
        },
        goal="Produce a review.",
    )

    assert result["success"] is True
    assert captured["model"] == "claude-opus-4-8"
    assert captured["max_tokens"] == 65536
    assert captured["reasoning_effort"] == "medium"


def test_adaptive_inspection_uses_bounded_local_code_evidence(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)
    captured = {}

    monkeypatch.setattr(
        operations.gather,
        "gather_code_context",
        lambda _root, depth, focus: {
            "text": "CODE CONTEXT\n===== FILE: src/main.py =====\nVALUE = 1",
            "file_count": 1,
            "candidate_count": 1,
            "files": ["src/main.py"],
            "complete_files": ["src/main.py"],
            "partial_files": [],
            "evidence_segments": [
                {"path": "src/main.py", "mode": "complete", "start_line": 1, "end_line": 1}
            ],
            "focus_applied": bool(focus),
        },
    )
    monkeypatch.setattr(
        operations.gather,
        "gather_durable_facts",
        lambda _root: {"text": "DURABLE FACT PACK"},
    )

    def fake_chat(arguments):
        captured.update(arguments)
        return operations.envelope(
            "chat", {"success": True, "text": "src/main.py defines VALUE."}, provider="claude"
        )

    monkeypatch.setattr(operations, "_chat", fake_chat)
    result = operations._adaptive_step_call(
        {
            "id": "inspect_repo",
            "capability": "inspect_codebase",
            "provider": "claude",
            "depends_on": [],
            "fallback_providers": [],
            "instruction": "Inspect the repository.",
            "reasoning_effort": "high",
            "investigation_depth": "deep",
            "final": False,
        },
        "claude",
        {},
        args={"project_root": root, "models": {"claude": "claude-opus-4-8"}},
        goal="Write complete repository documentation.",
    )

    assert "src/main.py" in captured["prompt"]
    assert captured["reasoning_effort"] == "high"
    assert result["data"]["inspection"]["depth"] == "deep"
    assert result["data"]["inspection"]["complete_files"] == ["src/main.py"]
    assert result["data"]["inspection"]["focus_applied"] is True


def test_adaptive_compare_maps_models_to_participants(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)
    captured = {}

    def fake_compare(arguments):
        captured.update(arguments)
        return operations.envelope(
            "compare_models", {"success": True, "text": "agreed"}, provider="multiple"
        )

    monkeypatch.setattr(operations, "_compare_models", fake_compare)
    operations._adaptive_step_call(
        {
            "id": "frontier_compare",
            "capability": "compare",
            "provider": "multiple",
            "depends_on": [],
            "fallback_providers": [],
            "instruction": "Compare the README findings.",
            "final": True,
            "participants": ["claude", "gemini"],
        },
        "multiple",
        {},
        args={
            "project_root": root,
            "models": {
                "claude": "claude-opus-4-8",
                "gemini": "gemini-3.1-pro-high",
            },
        },
        goal="Compare findings.",
    )

    assert captured["providers"] == ["claude", "gemini"]
    assert captured["models"] == ["claude-opus-4-8", "gemini-3.1-pro-high"]
