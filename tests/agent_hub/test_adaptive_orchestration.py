from __future__ import annotations

import json
import subprocess
from threading import Barrier, Event, Lock, Thread
import time

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


def _compare_plan():
    return {
        "schema": "agent_hub_plan_v1",
        "goal": "Compare independent findings",
        "steps": [
            {
                "id": "compare_findings",
                "capability": "compare",
                "provider": "multiple",
                "depends_on": [],
                "fallback_providers": [],
                "instruction": "Compare the findings.",
                "participants": ["claude", "grok", "gemini"],
                "final": True,
            }
        ],
    }


def _single_chat_plan():
    return {
        "schema": "agent_hub_plan_v1",
        "goal": "Answer once",
        "steps": [
            {
                "id": "answer",
                "capability": "chat",
                "provider": "claude",
                "depends_on": [],
                "fallback_providers": [],
                "instruction": "Answer.",
                "final": True,
            }
        ],
    }


def _policy_root(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Rules\n\n- Ground claims.\n", encoding="utf-8")
    return str(tmp_path)


def test_validator_accepts_llm_chosen_dag():
    plan = orchestrator.validate_plan(_plan())
    assert plan["final_step"] == "synthesize"
    assert plan["expected_max_calls"] == 10
    assert plan["expected_max_provider_calls"] == 10
    assert plan["plan_sha256"]
    assert all(step["reasoning_effort"] == "medium" for step in plan["steps"])


def test_validator_counts_compare_participants_as_provider_calls():
    with pytest.raises(ValueError, match="3 calls"):
        orchestrator.validate_plan(_compare_plan(), max_calls=2)

    normalized = orchestrator.validate_plan(_compare_plan(), max_calls=3)

    assert normalized["steps"][0]["estimated_max_provider_calls"] == 3
    assert normalized["expected_max_provider_calls"] == 3
    assert normalized["expected_max_calls"] == 3


def test_validator_counts_bounded_write_rewrites_and_rejects_invalid_limit():
    plan = _plan()
    plan["steps"][2]["quality_rewrite_attempts"] = 0
    normalized = orchestrator.validate_plan(plan)

    assert normalized["steps"][2]["provider_calls_per_attempt"] == 1
    assert normalized["steps"][2]["estimated_max_provider_calls"] == 2
    assert normalized["expected_max_provider_calls"] == 6

    plan["steps"][2]["quality_rewrite_attempts"] = 3
    with pytest.raises(ValueError, match="quality_rewrite_attempts"):
        orchestrator.validate_plan(plan)


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


def test_validator_uses_review_text_for_generated_write_output():
    plan = {
        "schema": "agent_hub_plan_v1",
        "goal": "Draft and review a document",
        "steps": [
            {
                "id": "draft",
                "capability": "write",
                "provider": "claude",
                "depends_on": [],
                "fallback_providers": [],
                "instruction": "Draft the document.",
                "quality_rewrite_attempts": 0,
                "final": False,
            },
            {
                "id": "review",
                "capability": "review_diff",
                "provider": "gpt",
                "depends_on": ["draft"],
                "fallback_providers": [],
                "instruction": "Review the generated draft.",
                "final": True,
            },
        ],
    }

    with pytest.raises(ValueError, match="use review_text"):
        orchestrator.validate_plan(plan)

    plan["steps"][1]["capability"] = "review_text"
    normalized = orchestrator.validate_plan(plan)
    assert normalized["final_step"] == "review"
    assert normalized["steps"][1]["provider_calls_per_attempt"] == 1


def test_validator_requires_dependency_for_review_text():
    plan = _single_chat_plan()
    plan["steps"][0]["capability"] = "review_text"

    with pytest.raises(ValueError, match="requires at least one dependency"):
        orchestrator.validate_plan(plan)


def test_validator_rejects_review_diff_with_transitive_write_ancestor():
    plan = _plan()
    plan["steps"][0].update(
        {
            "id": "draft",
            "capability": "write",
            "provider": "claude",
            "depends_on": [],
        }
    )
    plan["steps"][1].update(
        {
            "id": "summarize",
            "capability": "chat",
            "provider": "grok",
            "depends_on": ["draft"],
        }
    )
    plan["steps"][2].update(
        {
            "id": "review",
            "capability": "review_diff",
            "provider": "gpt",
            "depends_on": ["summarize"],
        }
    )

    with pytest.raises(ValueError, match="use review_text"):
        orchestrator.validate_plan(plan)


def test_planner_prompt_distinguishes_generated_text_from_worktree_diff():
    prompt = orchestrator.planner_prompt("Draft and review a README.")

    assert '"review_text"' in prompt
    assert "review_diff reads only" in prompt
    assert "empty Git diff is not" in prompt


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


def test_scheduler_does_not_call_provider_after_resume_budget_is_exhausted():
    plan = _compare_plan()
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("provider must not be called")

    result = orchestrator.execute_plan(
        plan,
        invoke=forbidden,
        max_calls=3,
        initial_call_count=3,
    )

    assert result["success"] is False
    assert result["status"] == "budget_exhausted"
    assert result["error"] == "provider_call_budget_exhausted"
    assert result["leaf_calls"] == 3
    assert called is False


def test_compare_budget_reservation_starts_no_provider_when_credit_is_insufficient(
    monkeypatch,
):
    called = []
    budget = orchestrator.ProviderCallBudget(2, max_concurrency=2)

    monkeypatch.setattr(
        operations,
        "_chat_raw",
        lambda provider, _arguments: called.append(provider) or {"success": True, "text": provider},
    )

    with pytest.raises(
        orchestrator.ProviderCallBudgetExceeded,
        match="provider_call_budget_exhausted",
    ):
        operations._compare_models(
            {
                "prompt": "Compare.",
                "providers": ["claude", "grok", "gemini"],
                "_provider_call_budget": budget,
            }
        )

    assert called == []
    assert budget.used == 0


def test_provider_budget_is_global_across_compare_and_sibling_call(monkeypatch):
    budget = orchestrator.ProviderCallBudget(4, max_concurrency=2)
    gate = Barrier(2)
    lock = Lock()
    active = 0
    max_active = 0

    def fake_dispatch(_tool, _arguments):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            gate.wait(timeout=2)
            time.sleep(0.01)
            return {"success": True, "text": "ok", "warnings": []}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(operations.claude_mcp, "dispatch_tool", fake_dispatch)
    monkeypatch.setattr(operations.grok_mcp, "dispatch_tool", fake_dispatch)
    monkeypatch.setattr(operations.google_mcp, "dispatch_tool", fake_dispatch)

    outcomes = operations.parallel.run_ordered(
        [
            lambda: operations._compare_models(
                {
                    "prompt": "Compare.",
                    "providers": ["claude", "grok", "gemini"],
                    "_provider_call_budget": budget,
                }
            ),
            lambda: operations._chat_raw(
                "claude",
                {"prompt": "Sibling.", "_provider_call_budget": budget},
            ),
        ],
        execution="parallel",
        max_workers=2,
    )

    assert all(outcome.error is None for outcome in outcomes)
    assert max_active == 2
    assert budget.used == 4


def test_provider_budget_stops_waiters_at_dispatch_deadline():
    budget = orchestrator.ProviderCallBudget(
        2,
        max_concurrency=1,
        deadline_monotonic=time.monotonic() + 0.02,
    )
    reservation = budget.reserve(2)

    with reservation.dispatch():
        time.sleep(0.03)

    with pytest.raises(
        orchestrator.ProviderCallDeadlineExceeded,
        match="workflow_timeout_exceeded",
    ):
        with reservation.dispatch():
            raise AssertionError("provider must not start after the deadline")

    reservation.close()
    assert budget.used == 1


def test_scheduler_rejects_success_that_arrives_after_workflow_deadline():
    plan = _compare_plan()
    plan["steps"][0]["participants"] = ["claude", "gemini"]

    def slow_success(_step, _provider, _dependencies):
        time.sleep(0.02)
        return {"success": True, "text": "late answer"}

    result = orchestrator.execute_plan(
        plan,
        invoke=slow_success,
        max_calls=2,
        max_elapsed_seconds=0.01,
    )

    assert result["success"] is False
    assert result["status"] == "timed_out"
    assert result["error"] == "workflow_timeout_exceeded"


@pytest.mark.parametrize(
    "provider_error",
    [
        {"type": "TimeoutError", "message": "provider exceeded its limit"},
        {"type": "codex_timeout", "message": "official Codex timed out"},
        "request_timeout",
    ],
)
def test_scheduler_turns_provider_timeouts_into_resumable_timeout(provider_error):
    result = orchestrator.execute_plan(
        _single_chat_plan(),
        invoke=lambda *_args, **_kwargs: {
            "success": False,
            "error": provider_error,
            "text": "provider failed",
        },
    )

    assert result["success"] is False
    assert result["status"] == "timed_out"
    assert result["error"] == "provider_call_timeout"
    attempts = result["results"]["answer"]["attempts"]
    assert attempts == [
        {
                "provider": "claude",
                "success": False,
                "error": "provider_call_timeout",
                "provider_calls": 1,
        }
    ]
    assert "official Codex timed out" not in json.dumps(result)


def test_adaptive_compare_does_not_dispatch_queued_participants_after_deadline(
    tmp_path, monkeypatch
):
    root = _policy_root(tmp_path)
    budget = orchestrator.ProviderCallBudget(
        3,
        max_concurrency=1,
        deadline_monotonic=time.monotonic() + 0.02,
    )
    called = []

    def fake_dispatch(_tool, _arguments):
        called.append(True)
        time.sleep(0.03)
        return {"success": True, "text": "evidence", "warnings": []}

    monkeypatch.setattr(operations.claude_mcp, "dispatch_tool", fake_dispatch)
    monkeypatch.setattr(operations.grok_mcp, "dispatch_tool", fake_dispatch)
    monkeypatch.setattr(operations.google_mcp, "dispatch_tool", fake_dispatch)

    result = orchestrator.execute_plan(
        _compare_plan(),
        invoke=lambda step, provider, dependencies: operations._adaptive_step_call(
            dict(step),
            provider,
            dict(dependencies),
            args={"project_root": root, "_provider_call_budget": budget},
            goal="Compare independent findings",
        ),
        max_calls=3,
        max_elapsed_seconds=0.2,
        call_budget=budget,
    )

    assert result["success"] is False
    assert result["status"] == "timed_out"
    assert result["error"] == "workflow_timeout_exceeded"
    assert result["leaf_calls"] == 1
    assert len(called) == 1


def test_adaptive_compare_reports_three_actual_provider_calls(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)
    budget = orchestrator.ProviderCallBudget(3, max_concurrency=3)

    def fake_dispatch(_tool, _arguments):
        return {"success": True, "text": "evidence", "warnings": []}

    monkeypatch.setattr(operations.claude_mcp, "dispatch_tool", fake_dispatch)
    monkeypatch.setattr(operations.grok_mcp, "dispatch_tool", fake_dispatch)
    monkeypatch.setattr(operations.google_mcp, "dispatch_tool", fake_dispatch)

    result = orchestrator.execute_plan(
        _compare_plan(),
        invoke=lambda step, provider, dependencies: operations._adaptive_step_call(
            dict(step),
            provider,
            dict(dependencies),
            args={"project_root": root, "_provider_call_budget": budget},
            goal="Compare independent findings",
        ),
        max_calls=3,
        call_budget=budget,
    )

    assert result["success"] is True
    assert result["leaf_calls"] == 3
    compare = result["results"]["compare_findings"]["data"]
    assert compare["schema"] == "compare_result_v1"
    assert compare["call_usage"]["provider_calls"] == 3


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


def test_adaptive_context_renders_each_compare_participant_answer():
    context = operations._adaptive_context(
        "Synthesize the review.",
        {"instruction": "Write the final recommendation."},
        {
            "compare_findings": {
                "success": True,
                "text": "Compared 3 provider/model targets (2 succeeded).",
                "data": {
                    "schema": "compare_result_v1",
                    "status": "partial",
                    "requested": 3,
                    "succeeded": 2,
                    "min_successes": 2,
                    "participants": [
                        {
                            "provider": "claude",
                            "model": "claude-test",
                            "success": True,
                            "text": "Claude evidence",
                        },
                        {
                            "provider": "grok",
                            "model": "grok-test",
                            "success": False,
                            "text": "",
                            "error": "provider unavailable",
                        },
                        {
                            "provider": "gemini",
                            "model": "gemini-test",
                            "success": True,
                            "text": "Gemini evidence",
                        },
                    ],
                },
            }
        },
    )

    assert "claude / claude-test" in context
    assert "Claude evidence" in context
    assert "gemini / gemini-test" in context
    assert "Gemini evidence" in context
    assert "grok / grok-test" in context
    assert "provider unavailable" in context


def test_adaptive_write_source_preserves_compare_participant_answers(tmp_path, monkeypatch):
    captured = {}
    root = _policy_root(tmp_path)
    dependency = {
        "success": True,
        "text": "aggregate only",
        "data": {
            "schema": "compare_result_v1",
            "status": "partial",
            "requested": 2,
            "succeeded": 1,
            "min_successes": 1,
            "participants": [
                {
                    "provider": "claude",
                    "model": "claude-test",
                    "success": True,
                    "text": "Claude evidence",
                },
                {
                    "provider": "gemini",
                    "model": "gemini-test",
                    "success": False,
                    "error": "provider unavailable",
                },
            ],
        },
    }

    def fake_write(arguments):
        captured.update(arguments)
        return operations.envelope("write", {"success": True, "text": "final"}, provider="claude")

    monkeypatch.setattr(operations, "_write", fake_write)
    operations._adaptive_step_call(
        {
            "id": "write_final",
            "capability": "write",
            "provider": "claude",
            "depends_on": ["compare"],
            "fallback_providers": [],
            "instruction": "Write the final answer.",
            "final": True,
        },
        "claude",
        {"compare": dependency},
        args={"project_root": root},
        goal="Synthesize.",
    )

    assert "claude / claude-test" in captured["source_text"]
    assert "Claude evidence" in captured["source_text"]
    assert "gemini / gemini-test" in captured["source_text"]
    assert "provider unavailable" in captured["source_text"]


def test_dependency_context_has_one_total_character_limit(monkeypatch):
    monkeypatch.setattr(operations, "ADAPTIVE_DEPENDENCY_ITEM_MAX_CHARS", 100)
    monkeypatch.setattr(operations, "ADAPTIVE_DEPENDENCY_CONTEXT_MAX_CHARS", 200)
    rendered = operations._render_dependency_outputs(
        {
            "first": {"text": "a" * 150},
            "second": {"text": "b" * 150},
            "third": {"text": "c" * 150},
        },
        max_chars=200,
    )

    assert rendered.endswith("[dependency context truncated]")
    assert len(rendered) <= operations.ADAPTIVE_DEPENDENCY_CONTEXT_MAX_CHARS + 31


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
    specs = {item["name"]: item for item in operations.tool_definitions()}
    timeout = specs["agent_hub_run_workflow"]["inputSchema"]["properties"]["workflow_timeout"]
    per_call = specs["agent_hub_run_workflow"]["inputSchema"]["properties"][
        "per_call_timeout"
    ]
    repairs = specs["agent_hub_plan_workflow"]["inputSchema"]["properties"][
        "planner_repair_attempts"
    ]
    assert timeout["default"] == 1790
    assert timeout["maximum"] == 1790
    assert per_call["default"] == 1790
    assert per_call["maximum"] == 1790
    assert repairs["default"] == 5
    assert repairs["maximum"] == 5
    assert (
        specs["agent_hub_plan_workflow"]["inputSchema"]["properties"]["max_leaf_calls"]["default"]
        == 100
    )


def test_adaptive_supervised_run_persists_and_resumes_one_wave_per_call(tmp_path, monkeypatch):
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

    loaded_before_continue = operations.dispatch_tool("agent_hub_get_run", {"run_id": run_id})
    first = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {"state": loaded_before_continue["data"], "max_waves_per_call": 1},
    )
    assert first["success"] is True
    assert first["text"] == ("Adaptive workflow paused safely; call continue for the next wave.")
    assert first["data"]["status"] == "paused"
    assert set(first["data"]["results"]) == {
        "analyze_code",
        "challenge_assumptions",
    }
    assert first["data"]["pending_steps"] == ["synthesize"]

    second = operations.dispatch_tool("agent_hub_continue_workflow", {"run_id": run_id})
    assert second["success"] is True
    assert second["data"]["status"] == "completed"
    assert second["text"] == "final"
    loaded = operations.dispatch_tool("agent_hub_get_run", {"run_id": run_id})
    assert loaded["data"]["done"] is True
    assert loaded["data"]["status"] == "completed"


def test_background_adaptive_continue_returns_before_provider_finishes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = _policy_root(repo)
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    provider_started = Event()
    release_provider = Event()

    def slow_step(_step, provider, _dependencies, **_kwargs):
        provider_started.set()
        assert release_provider.wait(timeout=2)
        return {"success": True, "provider": provider, "text": "done", "data": {}}

    monkeypatch.setattr(operations, "_adaptive_step_call", slow_step)
    started = operations.dispatch_tool(
        "agent_hub_start_workflow",
        {
            "workflow_id": "adaptive",
            "plan": _single_chat_plan(),
            "project_root": root,
        },
    )
    run_id = started["data"]["run_id"]

    accepted = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {
            "run_id": run_id,
            "expected_revision": 0,
            "background": True,
            "max_waves_per_call": 1,
        },
    )

    assert accepted["success"] is True
    assert accepted["data"]["accepted"] is True
    assert accepted["data"]["execution"] == "background"
    assert accepted["data"]["status"] == "running"
    assert accepted["data"]["next_action"]["tool"] == "agent_hub_get_run"
    assert provider_started.wait(timeout=1)
    observed = operations.dispatch_tool("agent_hub_get_run", {"run_id": run_id})
    assert observed["success"] is True
    assert observed["data"]["lease_active"] is True
    assert observed["data"]["continuation_status"] == "running"
    assert observed["data"]["next_action"]["tool"] == "agent_hub_get_run"

    release_provider.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        completed = operations.dispatch_tool("agent_hub_get_run", {"run_id": run_id})
        if completed["data"]["store_revision"] == 1:
            break
        time.sleep(0.01)
    else:
        pytest.fail("background continuation did not commit")

    assert completed["data"]["status"] == "completed"
    assert completed["data"]["lease_active"] is False
    assert completed["data"]["continuation_status"] == "idle"


def test_background_adaptive_continue_redacts_worker_crash(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = _policy_root(repo)
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))

    def crash(*_args, **_kwargs):
        raise RuntimeError("secret provider response")

    monkeypatch.setattr(operations, "_continue_adaptive_workflow", crash)
    started = operations.dispatch_tool(
        "agent_hub_start_workflow",
        {
            "workflow_id": "adaptive",
            "plan": _single_chat_plan(),
            "project_root": root,
        },
    )
    run_id = started["data"]["run_id"]
    accepted = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {
            "run_id": run_id,
            "expected_revision": 0,
            "background": True,
        },
    )
    assert accepted["success"] is True

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        observed = operations.dispatch_tool("agent_hub_get_run", {"run_id": run_id})
        if observed["data"]["store_revision"] == 1:
            break
        time.sleep(0.01)
    else:
        pytest.fail("background failure was not committed")

    assert observed["data"]["status"] == "paused"
    assert observed["data"]["pause_reason"] == "background_worker_failed"
    assert observed["data"]["error"] == "background_worker_failed"
    assert "secret provider response" not in json.dumps(observed, ensure_ascii=False)


def test_concurrent_adaptive_continue_calls_only_one_provider(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = _policy_root(repo)
    monkeypatch.setenv(
        "ORCHESTRATE_CODEX_STATE_DIR",
        str(tmp_path / "runs"),
    )
    provider_started = Event()
    release_provider = Event()
    second_done = Event()
    calls = 0
    call_lock = Lock()
    responses = {}

    def fake_step(_step, provider, _dependencies, **_kwargs):
        nonlocal calls
        with call_lock:
            calls += 1
        provider_started.set()
        assert release_provider.wait(timeout=2)
        return {"success": True, "provider": provider, "text": "done", "data": {}}

    monkeypatch.setattr(operations, "_adaptive_step_call", fake_step)
    started = operations.dispatch_tool(
        "agent_hub_start_workflow",
        {
            "workflow_id": "adaptive",
            "plan": _single_chat_plan(),
            "project_root": root,
        },
    )
    run_id = started["data"]["run_id"]
    stale_state = dict(started["data"])
    assert started["data"]["state_schema_version"] == 2
    assert started["data"]["call_accounting_version"] == 2
    assert started["data"]["store_revision"] == 0
    assert started["data"]["next_action"]["arguments"]["expected_revision"] == 0

    first_thread = Thread(
        target=lambda: responses.setdefault(
            "first",
            operations.dispatch_tool(
                "agent_hub_continue_workflow",
                {"run_id": run_id},
            ),
        )
    )

    def run_second():
        responses["second"] = operations.dispatch_tool(
            "agent_hub_continue_workflow",
            {"run_id": run_id},
        )
        second_done.set()

    second_thread = Thread(target=run_second)
    first_thread.start()
    assert provider_started.wait(timeout=1)
    second_thread.start()
    try:
        assert second_done.wait(timeout=0.5)
        assert responses["second"]["success"] is False
        assert responses["second"]["error"]["type"] == "run_lease_active"
        assert calls == 1
        observed = operations.dispatch_tool("agent_hub_get_run", {"run_id": run_id})
        assert observed["data"]["lease_active"] is True
        assert "_lease" not in json.dumps(observed)
    finally:
        release_provider.set()
        first_thread.join(timeout=2)
        second_thread.join(timeout=2)

    assert responses["first"]["success"] is True
    assert responses["first"]["data"]["store_revision"] == 1
    assert "_lease" not in responses["first"]["data"]

    stale = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {"run_id": run_id, "expected_revision": 0},
    )
    assert stale["success"] is False
    assert stale["error"]["type"] == "run_revision_conflict"
    assert calls == 1

    stale_supplied = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {"state": stale_state},
    )
    assert stale_supplied["success"] is False
    assert stale_supplied["error"]["type"] == "run_revision_conflict"
    assert calls == 1


def test_adaptive_continue_lease_always_covers_maximum_runtime(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = _policy_root(repo)
    monkeypatch.setenv(
        "ORCHESTRATE_CODEX_STATE_DIR",
        str(tmp_path / "runs"),
    )
    observed_lease_seconds = []
    original_claim = operations.store.claim

    def recording_claim(*args, **kwargs):
        observed_lease_seconds.append(kwargs["lease_seconds"])
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(operations.store, "claim", recording_claim)
    monkeypatch.setattr(
        operations,
        "_adaptive_step_call",
        lambda _step, provider, _dependencies, **_kwargs: {
            "success": True,
            "provider": provider,
            "text": "done",
            "data": {},
        },
    )
    started = operations.dispatch_tool(
        "agent_hub_start_workflow",
        {
            "workflow_id": "adaptive",
            "plan": _single_chat_plan(),
            "project_root": root,
            "workflow_timeout": operations.ADAPTIVE_WORKFLOW_TIMEOUT_MIN,
        },
    )

    resumed = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {"run_id": started["data"]["run_id"]},
    )

    assert resumed["success"] is True
    assert observed_lease_seconds == [
        operations.ADAPTIVE_WORKFLOW_TIMEOUT_MAX + operations.ADAPTIVE_LEASE_GRACE_SECONDS
    ]


def test_legacy_adaptive_call_accounting_pauses_before_provider_call(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = _policy_root(repo)
    monkeypatch.setenv(
        "ORCHESTRATE_CODEX_STATE_DIR",
        str(tmp_path / "runs"),
    )
    called = False
    run_id = "4" * 12
    operations.store.create(
        {
            "run_id": run_id,
            "workflow_id": "adaptive",
            "run_kind": "adaptive",
            "status": "paused",
            "plan": _single_chat_plan(),
            "options": {"project_root": root},
            "results": {},
            "waves": [],
            "leaf_calls": 1,
        }
    )

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("legacy state must not call a provider")

    monkeypatch.setattr(operations, "_adaptive_step_call", forbidden)
    result = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {"run_id": run_id},
    )

    assert result["success"] is False
    assert result["error"]["type"] == "legacy_call_accounting"
    assert result["data"]["pause_reason"] == "call_accounting_upgrade_required"
    assert called is False
    assert "_lease" not in operations.store.load(run_id)


def test_adaptive_continue_releases_lease_after_unexpected_exception(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = _policy_root(repo)
    monkeypatch.setenv(
        "ORCHESTRATE_CODEX_STATE_DIR",
        str(tmp_path / "runs"),
    )
    started = operations.dispatch_tool(
        "agent_hub_start_workflow",
        {
            "workflow_id": "adaptive",
            "plan": _single_chat_plan(),
            "project_root": root,
        },
    )
    run_id = started["data"]["run_id"]
    real_execute_plan = orchestrator.execute_plan

    def fail_execute(*_args, **_kwargs):
        raise RuntimeError("unexpected scheduler failure")

    monkeypatch.setattr(
        orchestrator,
        "execute_plan",
        fail_execute,
    )

    failed = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {"run_id": run_id},
    )

    assert failed["success"] is False
    assert operations.store.load(run_id).get("_lease") is None

    monkeypatch.setattr(orchestrator, "execute_plan", real_execute_plan)
    monkeypatch.setattr(
        operations,
        "_adaptive_step_call",
        lambda _step, provider, _dependencies, **_kwargs: {
            "success": True,
            "provider": provider,
            "text": "done",
            "data": {},
        },
    )
    resumed = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {"run_id": run_id},
    )

    assert resumed["success"] is True
    assert resumed["data"]["status"] == "completed"


def test_adaptive_start_and_continue_schemas_expose_resumable_controls():
    specs = {item["name"]: item for item in operations.tool_definitions()}
    start_props = specs["agent_hub_start_workflow"]["inputSchema"]["properties"]
    claim_props = specs["agent_hub_claim_run_action"]["inputSchema"]["properties"]
    continue_props = specs["agent_hub_continue_workflow"]["inputSchema"]["properties"]
    get_props = specs["agent_hub_get_run"]["inputSchema"]["properties"]

    assert start_props["workflow_timeout"]["default"] == 1790
    assert continue_props["workflow_timeout"]["maximum"] == 1790
    assert continue_props["max_waves_per_call"]["default"] == 8
    assert continue_props["background"]["default"] is False
    assert continue_props["expected_revision"]["minimum"] == 0
    assert claim_props["action_id"]["pattern"] == "^[0-9a-f]{64}$"
    assert continue_props["claim_token"]["pattern"] == "^[0-9a-f]{32}$"
    assert continue_props["run_id"]["pattern"] == "^[0-9a-f]{12}$"
    assert get_props["run_id"]["pattern"] == "^[0-9a-f]{12}$"


def test_canonical_run_tools_reject_path_traversal_without_reading_outside(tmp_path, monkeypatch):
    state_dir = tmp_path / "runs"
    outside = tmp_path / "outside.json"
    outside.write_text(
        '{"run_id": "../../outside", "run_kind": "adaptive", "secret": "do-not-leak"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(state_dir))

    result = operations.dispatch_tool(
        "agent_hub_get_run",
        {"run_id": "../../outside"},
    )

    assert result["success"] is False
    assert result["data"]["error_type"] == "ValueError"
    assert "do-not-leak" not in json.dumps(result)

    wrong_type = operations.dispatch_tool(
        "agent_hub_get_run",
        {"run_id": 123456789012},
    )
    assert wrong_type["success"] is False
    assert wrong_type["data"]["error_type"] == "ValueError"


def test_supplied_adaptive_state_rejects_invalid_run_id_before_provider_call(monkeypatch):
    called = False

    def forbidden_call(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(operations, "_adaptive_step_call", forbidden_call)
    state = {
        "run_id": "../../outside",
        "run_kind": "adaptive",
        "status": "paused",
        "plan": _plan(),
        "options": {},
        "results": {},
        "waves": [],
        "leaf_calls": 0,
    }

    result = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {"state": state},
    )

    assert result["success"] is False
    assert result["data"]["error_type"] == "ValueError"
    assert called is False


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


def test_provider_timeout_returns_a_persisted_resume_run(tmp_path, monkeypatch):
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
            "error": "provider_call_timeout",
            "text": "provider call timed out",
            "results": {},
            "waves": [],
            "leaf_calls": 1,
        },
    )

    result = operations.dispatch_tool(
        "agent_hub_run_workflow",
        {"workflow_id": "adaptive", "plan": _single_chat_plan(), "project_root": root},
    )

    assert result["success"] is False
    assert result["data"]["resumable"] is True
    run_id = result["data"]["run_id"]
    loaded = operations.dispatch_tool("agent_hub_get_run", {"run_id": run_id})
    assert loaded["data"]["status"] == "paused"
    assert loaded["data"]["pause_reason"] == "provider_call_timeout"
    assert loaded["data"]["results"] == {}


def test_adaptive_run_fails_closed_when_resume_state_cannot_be_persisted(monkeypatch):
    def fail_create(_state):
        raise operations.store.RunPersistenceError("could not persist run state")

    monkeypatch.setattr(operations.store, "create", fail_create)

    with pytest.raises(
        operations.store.RunPersistenceError,
        match="could not persist",
    ):
        operations._save_adaptive_state({"run_id": "0" * 12, "plan": _plan()})


def test_workflow_catalog_and_schema_expose_adaptive_mode():
    listed = operations.dispatch_tool("agent_hub_list_workflows", {})
    adaptive = next(item for item in listed["data"]["workflows"] if item["id"] == "adaptive")
    assert adaptive["dynamic"] is True
    explained = operations.dispatch_tool("agent_hub_get_workflow", {"workflow_id": "adaptive"})
    assert explained["data"]["schema"] == "agent_hub_plan_v1"
    planned = next(
        item for item in operations.tool_definitions() if item["name"] == "agent_hub_plan_workflow"
    )
    assert planned["annotations"]["readOnlyHint"] is False
    assert "planner_provider" in planned["inputSchema"]["properties"]
    assert "models" in planned["inputSchema"]["properties"]
    assert len(operations.tool_definitions()) == 37


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
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
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


def test_adaptive_review_text_receives_dependency_output(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)
    captured = {}

    def fake_chat(arguments):
        captured.update(arguments)
        return operations.envelope(
            "chat",
            {"success": True, "text": "review complete"},
            provider=arguments["provider"],
        )

    monkeypatch.setattr(operations, "_chat", fake_chat)
    result = operations._adaptive_step_call(
        {
            "id": "review_draft",
            "capability": "review_text",
            "provider": "gpt",
            "depends_on": ["draft"],
            "fallback_providers": [],
            "instruction": "Review the generated draft for accuracy.",
            "reasoning_effort": "high",
            "final": True,
        },
        "gpt",
        {"draft": {"success": True, "text": "generated README body"}},
        args={"project_root": root, "models": {"gpt": "gpt-test"}},
        goal="Write and review a README.",
    )

    assert result["success"] is True
    assert captured["model"] == "gpt-test"
    assert captured["reasoning_effort"] == "high"
    assert "generated README body" in captured["prompt"]


def test_adaptive_review_diff_fails_closed_on_empty_worktree(tmp_path):
    root = _policy_root(tmp_path)
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "AGENTS.md"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)

    result = operations._adaptive_step_call(
        {
            "id": "review_changes",
            "capability": "review_diff",
            "provider": "gpt",
            "depends_on": [],
            "fallback_providers": [],
            "instruction": "Review the working tree.",
            "reasoning_effort": "high",
            "final": True,
        },
        "gpt",
        {},
        args={"project_root": root},
        goal="Review repository changes.",
    )

    assert result["success"] is False
    assert result["error_type"] == "adaptive_review_diff_empty"
    assert "empty_diff" in result["warnings"]


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
            "candidate_limit": 10_000,
            "candidate_truncated": True,
            "focus_scan_truncated": True,
            "read_bytes": 1_024,
            "read_byte_limit": 2_048,
            "focus_scan_byte_limit": 1_536,
            "skipped_file_counts": {"sensitive": 1},
            "source_truncated_files": [],
            "text_chars": 1_900,
            "text_char_limit": 2_000,
            "text_truncated": True,
            "git": {"output_truncated": True},
        },
    )
    monkeypatch.setattr(
        operations.gather,
        "gather_durable_facts",
        lambda _root: {
            "text": "DURABLE FACT PACK",
            "durable_read_bytes": 512,
            "durable_read_byte_limit": 1_024,
            "text_truncated": False,
        },
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
    assert result["data"]["inspection"]["candidate_truncated"] is True
    assert result["data"]["inspection"]["skipped_file_counts"] == {"sensitive": 1}
    assert result["data"]["inspection"]["text_char_limit"] == 2_000
    assert result["data"]["inspection"]["git_output_truncated"] is True
    assert result["data"]["inspection"]["durable_read_bytes"] == 512


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


def test_adaptive_compare_preserves_partial_provider_model_map(tmp_path, monkeypatch):
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
            "models": {"claude": "claude-opus-4-8"},
        },
        goal="Compare findings.",
    )

    assert captured["providers"] == ["claude", "gemini"]
    assert captured["models"] == ["claude-opus-4-8", ""]


def test_adaptive_state_snapshots_all_effective_models(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)
    monkeypatch.setattr(
        operations,
        "_effective_provider_models",
        lambda: {
            "claude": "claude-snapshot",
            "grok": "grok-snapshot",
            "gemini": "gemini-snapshot",
            "gpt": "gpt-snapshot",
        },
    )

    state = operations._new_adaptive_state(
        _single_chat_plan(),
        {
            "project_root": root,
            "models": {"claude": "claude-explicit"},
        },
        {},
    )

    assert state["options"]["models"] == {
        "claude": "claude-explicit",
        "grok": "grok-snapshot",
        "gemini": "gemini-snapshot",
        "gpt": "gpt-snapshot",
    }


def test_adaptive_resume_keeps_persisted_model_snapshot(monkeypatch, tmp_path):
    root = _policy_root(tmp_path)
    persisted = {
        "project_root": root,
        "models": {
            "claude": "claude-original",
            "grok": "grok-original",
            "gemini": "gemini-original",
            "gpt": "gpt-original",
        },
    }
    monkeypatch.setattr(
        operations,
        "_effective_provider_models",
        lambda: pytest.fail("complete persisted snapshots must not reread live settings"),
    )

    options = operations._adaptive_run_options(
        {"models": {"claude": "claude-override"}},
        persisted,
    )

    assert options["models"] == persisted["models"]


def test_adaptive_executor_surfaces_successful_step_warnings():
    result = orchestrator.execute_plan(
        _single_chat_plan(),
        invoke=lambda *_args, **_kwargs: {
            "success": True,
            "text": "done",
            "warnings": [
                "automatic_reasoning_effort_omitted:claude:claude-haiku-test"
            ],
        },
        max_calls=4,
    )

    assert result["warnings"] == [
        "automatic_reasoning_effort_omitted:claude:claude-haiku-test"
    ]
