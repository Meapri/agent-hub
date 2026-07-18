"""LLM-planned, locally validated adaptive orchestration."""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
from hashlib import sha256
import json
import re
import time
from typing import Any, Callable, Dict, List, Mapping, Sequence

from agent_hub import capabilities, consistency
from agent_hub.core import parallel


PLAN_SCHEMA = "agent_hub_plan_v1"
MAX_PLAN_STEPS = 12
MAX_INSTRUCTION_CHARS = 4_000
_STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FENCED_JSON_RE = re.compile(r"\A```(?:json)?\s*\n?(.*?)\n?```\s*\Z", re.I | re.S)

CAPABILITY_PROVIDERS: Dict[str, Sequence[str]] = {
    "chat": ("claude", "grok", "gemini"),
    "inspect_codebase": ("claude", "grok", "gemini"),
    "search": ("claude", "grok", "gemini"),
    "write": ("claude", "grok", "gemini"),
    "review_diff": ("claude", "grok", "gemini"),
    "compare": ("multiple",),
    "verify": ("local",),
    "release_snapshot": ("local",),
    "release_draft": ("claude", "grok", "gemini"),
}
_PROVIDER_CAPABILITY = {
    "chat": "chat",
    "inspect_codebase": "chat",
    "search": "search",
    "write": "write",
    "review_diff": "review_diff",
    "release_draft": "release_draft",
}
_PLAN_KEYS = {"schema", "goal", "steps", "rationale"}
_STEP_KEYS = {
    "id",
    "capability",
    "provider",
    "depends_on",
    "fallback_providers",
    "instruction",
    "final",
    "participants",
    "decision_labels",
    "reasoning_effort",
    "investigation_depth",
}
REASONING_EFFORTS = ("low", "medium", "high")
INVESTIGATION_DEPTHS = ("shallow", "standard", "deep")


def capability_manifest() -> Dict[str, Any]:
    return {
        name: {
            "providers": list(providers),
            "parallel_safe": name not in {"verify"},
        }
        for name, providers in CAPABILITY_PROVIDERS.items()
    }


def planner_prompt(goal: str, *, facts: str = "", max_steps: int = MAX_PLAN_STEPS) -> str:
    manifest = json.dumps(capability_manifest(), ensure_ascii=False, sort_keys=True)
    fact_block = facts.strip()[:8_000] or "[no repository fact pack supplied]"
    return f"""Design an execution DAG for Agent Hub. You decide the useful decomposition,
provider for each step, dependencies, fallbacks, and which single step produces the final answer.
Return exactly one JSON object with no markdown or commentary.

Allowed capabilities and providers (hard constraint):
{manifest}

Contract:
{{
  "schema": "{PLAN_SCHEMA}",
  "goal": "copy the user goal",
  "rationale": "short planning summary",
  "steps": [
    {{
      "id": "lower_snake_case",
      "capability": "one allowed capability",
      "provider": "one allowed provider",
      "depends_on": ["step ids whose outputs are required"],
      "fallback_providers": ["compatible alternatives, if useful"],
      "instruction": "specific task for this step",
      "reasoning_effort": "low | medium | high",
      "investigation_depth": "shallow | standard | deep (inspect_codebase only)",
      "final": false,
      "participants": ["only for compare: 2-3 model providers"],
      "decision_labels": ["only for a closed decision compare: 2-20 labels"]
    }}
  ]
}}

Rules:
- Create at most {max_steps} steps, usually 2-6. Do not add ceremonial steps.
- Do not invent capabilities, providers, tools, files, or facts.
- Use inspect_codebase for local repository understanding. Use search only for external/web facts.
- Choose reasoning_effort per step. Use low for mechanical work, medium for normal analysis, and high
  for ambiguous architecture, broad codebase investigation, difficult review, or final synthesis.
- Choose investigation_depth only for inspect_codebase. Use deep when a durable repository document
  must cover entry points, public schemas, configuration, tests, generated docs, and Git state.
- Make inspect_codebase instructions name the relevant subsystems, paths, commands, or symbols that
  must be proven. The gatherer uses those details for a broad scan followed by focused deep reads.
- Require file:line evidence for repository claims and distinguish complete files from partial excerpts.
- Express true data dependencies only. Independent steps must have the same dependency frontier so
  the scheduler can run them concurrently. Do not encode an arbitrary provider order.
- Every non-final step must feed, directly or transitively, the one final step.
- Use compare only when multiple independent judgments materially help. Use decision_labels only
  when the answer has a real caller-definable closed label set; never fake a semantic score for open text.
- verify is a deterministic local text check, not an LLM judge.

User goal:
{goal.strip()}

Repository fact pack:
{fact_block}
"""


def parse_plan(text: str) -> Dict[str, Any]:
    body = str(text or "").strip()
    fenced = _FENCED_JSON_RE.fullmatch(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid plan JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("adaptive plan must be one JSON object")
    return value


def _unique_strings(values: Any, *, field: str) -> List[str]:
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise ValueError(f"{field} must be an array of strings")
    cleaned = [item.strip() for item in values]
    if any(not item for item in cleaned) or len(set(cleaned)) != len(cleaned):
        raise ValueError(f"{field} must contain unique non-empty strings")
    return cleaned


def _provider_supported(capability: str, provider: str) -> bool:
    allowed = CAPABILITY_PROVIDERS.get(capability, ())
    if provider not in allowed:
        return False
    provider_capability = _PROVIDER_CAPABILITY.get(capability)
    return not provider_capability or capabilities.supports(provider, provider_capability)


def validate_plan(
    plan: Mapping[str, Any],
    *,
    max_steps: int = MAX_PLAN_STEPS,
    max_calls: int = 24,
) -> Dict[str, Any]:
    """Validate an LLM plan as untrusted input and return a normalized copy."""

    if set(plan) - _PLAN_KEYS:
        raise ValueError(f"plan contains unsupported fields: {sorted(set(plan) - _PLAN_KEYS)}")
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"schema must equal {PLAN_SCHEMA}")
    goal = str(plan.get("goal") or "").strip()
    if not goal:
        raise ValueError("plan goal is required")
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("plan steps must be a non-empty array")
    limit = max(1, min(int(max_steps), MAX_PLAN_STEPS))
    if len(raw_steps) > limit:
        raise ValueError(f"plan has too many steps: {len(raw_steps)} > {limit}")

    normalized_steps: List[Dict[str, Any]] = []
    ids: List[str] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise ValueError(f"step {index} must be an object")
        if set(raw) - _STEP_KEYS:
            raise ValueError(
                f"step {index} contains unsupported fields: {sorted(set(raw) - _STEP_KEYS)}"
            )
        step_id = str(raw.get("id") or "").strip()
        if not _STEP_ID_RE.fullmatch(step_id):
            raise ValueError(f"invalid step id: {step_id!r}")
        if step_id in ids:
            raise ValueError(f"duplicate step id: {step_id}")
        capability = str(raw.get("capability") or "").strip()
        provider = str(raw.get("provider") or "").strip()
        if capability not in CAPABILITY_PROVIDERS:
            raise ValueError(f"unsupported capability: {capability}")
        if not _provider_supported(capability, provider):
            raise ValueError(f"provider {provider!r} does not support capability {capability!r}")
        dependencies = _unique_strings(raw.get("depends_on", []), field=f"{step_id}.depends_on")
        fallbacks = _unique_strings(
            raw.get("fallback_providers", []), field=f"{step_id}.fallback_providers"
        )
        if provider in fallbacks:
            raise ValueError(f"{step_id}.fallback_providers repeats the primary provider")
        if any(not _provider_supported(capability, item) for item in fallbacks):
            raise ValueError(f"{step_id} has an incompatible fallback provider")
        instruction = str(raw.get("instruction") or "").strip()
        if not instruction or len(instruction) > MAX_INSTRUCTION_CHARS:
            raise ValueError(f"{step_id}.instruction must be 1..{MAX_INSTRUCTION_CHARS} chars")
        reasoning_effort = str(raw.get("reasoning_effort") or "medium").strip().lower()
        if reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                f"{step_id}.reasoning_effort must be one of: {', '.join(REASONING_EFFORTS)}"
            )
        investigation_depth = str(raw.get("investigation_depth") or "").strip().lower()
        if capability == "inspect_codebase":
            investigation_depth = investigation_depth or "standard"
            if investigation_depth not in INVESTIGATION_DEPTHS:
                raise ValueError(
                    f"{step_id}.investigation_depth must be one of: "
                    f"{', '.join(INVESTIGATION_DEPTHS)}"
                )
        elif investigation_depth:
            raise ValueError(
                f"{step_id}.investigation_depth is only valid for inspect_codebase"
            )

        participants: List[str] = []
        decision_labels: List[str] = []
        if capability == "compare":
            participants = _unique_strings(
                raw.get("participants", ["claude", "grok", "gemini"]),
                field=f"{step_id}.participants",
            )
            if not 2 <= len(participants) <= 3 or any(
                item not in {"claude", "grok", "gemini"} for item in participants
            ):
                raise ValueError(f"{step_id}.participants must contain 2-3 model providers")
            if raw.get("decision_labels"):
                decision_labels = consistency.validate_labels(raw["decision_labels"])
        elif raw.get("participants") or raw.get("decision_labels"):
            raise ValueError(f"{step_id} may use participants/decision_labels only with compare")

        normalized_steps.append(
            {
                "id": step_id,
                "capability": capability,
                "provider": provider,
                "depends_on": dependencies,
                "fallback_providers": fallbacks,
                "instruction": instruction,
                "reasoning_effort": reasoning_effort,
                **(
                    {"investigation_depth": investigation_depth}
                    if investigation_depth
                    else {}
                ),
                "final": bool(raw.get("final", False)),
                **({"participants": participants} if participants else {}),
                **({"decision_labels": decision_labels} if decision_labels else {}),
            }
        )
        ids.append(step_id)

    id_set = set(ids)
    graph: Dict[str, set[str]] = {}
    for step in normalized_steps:
        deps = set(step["depends_on"])
        unknown = deps - id_set
        if unknown:
            raise ValueError(f"{step['id']} depends on unknown steps: {sorted(unknown)}")
        if step["id"] in deps:
            raise ValueError(f"{step['id']} cannot depend on itself")
        graph[step["id"]] = deps
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise ValueError("adaptive plan contains a dependency cycle") from exc

    finals = [step for step in normalized_steps if step["final"]]
    if len(finals) != 1:
        raise ValueError("adaptive plan must contain exactly one final step")
    final_id = finals[0]["id"]
    if any(final_id in step["depends_on"] for step in normalized_steps):
        raise ValueError("the final step must be a DAG sink")
    if finals[0]["capability"] not in {"chat", "write", "compare", "release_draft"}:
        raise ValueError("the final step must produce a user-facing answer")

    downstream: Dict[str, set[str]] = {step_id: set() for step_id in ids}
    for step in normalized_steps:
        for dependency in step["depends_on"]:
            downstream[dependency].add(step["id"])

    def reaches_final(start: str) -> bool:
        frontier = list(downstream[start])
        seen: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current == final_id:
                return True
            if current not in seen:
                seen.add(current)
                frontier.extend(downstream[current])
        return start == final_id

    orphaned = [step_id for step_id in ids if not reaches_final(step_id)]
    if orphaned:
        raise ValueError(f"steps do not feed the final result: {orphaned}")

    expected_calls = sum(1 + len(step["fallback_providers"]) for step in normalized_steps)
    if expected_calls > int(max_calls):
        raise ValueError(f"plan may require {expected_calls} calls, exceeding max_calls={max_calls}")

    normalized = {
        "schema": PLAN_SCHEMA,
        "goal": goal,
        "rationale": str(plan.get("rationale") or "").strip(),
        "steps": normalized_steps,
        "final_step": final_id,
        "expected_max_calls": expected_calls,
    }
    material = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    normalized["plan_sha256"] = sha256(material.encode("utf-8")).hexdigest()
    return normalized


StepInvoker = Callable[[Mapping[str, Any], str, Mapping[str, Dict[str, Any]]], Dict[str, Any]]


def execute_plan(
    plan: Mapping[str, Any],
    *,
    invoke: StepInvoker,
    max_concurrency: int = 3,
    max_calls: int = 24,
    max_elapsed_seconds: float | None = None,
    initial_results: Mapping[str, Mapping[str, Any]] | None = None,
    initial_waves: Sequence[Mapping[str, Any]] | None = None,
    initial_call_count: int = 0,
    max_waves: int | None = None,
) -> Dict[str, Any]:
    """Execute dependency-ready frontiers, optionally resuming a prior slice."""

    normalized = validate_plan(plan, max_calls=max_calls)
    steps = {step["id"]: step for step in normalized["steps"]}
    results: Dict[str, Dict[str, Any]] = {
        str(step_id): dict(result)
        for step_id, result in (initial_results or {}).items()
        if str(step_id) in steps
        and isinstance(result, Mapping)
        and bool(result.get("success"))
    }
    pending = set(steps) - set(results)
    waves: List[Dict[str, Any]] = [dict(wave) for wave in (initial_waves or [])]
    call_count = max(0, int(initial_call_count))
    slice_wave_count = 0
    started = time.monotonic()

    def timed_out() -> bool:
        return bool(
            max_elapsed_seconds is not None
            and time.monotonic() - started >= float(max_elapsed_seconds)
        )

    def run_step(step: Mapping[str, Any]) -> Dict[str, Any]:
        dependency_results = {dep: results[dep] for dep in step["depends_on"]}
        attempts: List[Dict[str, Any]] = []
        for provider in [step["provider"], *step["fallback_providers"]]:
            if timed_out():
                attempts.append(
                    {"provider": provider, "success": False, "error": "workflow_timeout_exceeded"}
                )
                break
            try:
                response = invoke(step, provider, dependency_results)
                ok = bool(response.get("success", not response.get("error")))
                attempts.append(
                    {
                        "provider": provider,
                        "success": ok,
                        "error": response.get("error"),
                    }
                )
                if ok:
                    return {
                        "success": True,
                        "provider": provider,
                        "text": str(response.get("text") or ""),
                        "attempts": attempts,
                        "data": response.get("data") or response,
                    }
            except Exception as exc:  # noqa: BLE001 - fallback is part of the contract
                attempts.append({"provider": provider, "success": False, "error": str(exc)})
        return {
            "success": False,
            "provider": None,
            "text": "",
            "attempts": attempts,
            "error": attempts[-1].get("error") if attempts else "step failed",
        }

    while pending:
        if max_waves is not None and slice_wave_count >= max(1, int(max_waves)):
            return {
                "success": True,
                "status": "paused",
                "text": "Adaptive workflow slice completed; continue the persisted run.",
                "pending_steps": sorted(pending),
                "plan": normalized,
                "results": results,
                "waves": waves,
                "leaf_calls": call_count,
            }
        if timed_out():
            return {
                "success": False,
                "status": "timed_out",
                "text": "Adaptive workflow exhausted its end-to-end time budget.",
                "error": "workflow_timeout_exceeded",
                "blocked_steps": sorted(pending),
                "plan": normalized,
                "results": results,
                "waves": waves,
                "leaf_calls": call_count,
            }
        ready = [
            step_id
            for step_id in pending
            if all(dependency in results for dependency in steps[step_id]["depends_on"])
        ]
        if not ready:
            return {
                "success": False,
                "status": "blocked",
                "text": "No dependency-ready adaptive steps remain.",
                "error": "scheduler_deadlock",
                "plan": normalized,
                "results": results,
                "waves": waves,
                "leaf_calls": call_count,
            }
        outcomes = parallel.run_ordered(
            [lambda sid=step_id: run_step(steps[sid]) for step_id in ready],
            execution="parallel",
            max_workers=max_concurrency,
        )
        wave_results: Dict[str, Any] = {}
        failed: List[str] = []
        for step_id, outcome in zip(ready, outcomes):
            if outcome.error is not None:
                result = {
                    "success": False,
                    "provider": None,
                    "text": "",
                    "attempts": [],
                    "error": str(outcome.error),
                }
            else:
                result = dict(outcome.value or {})
            result["elapsed_ms"] = outcome.elapsed_ms
            results[step_id] = result
            pending.remove(step_id)
            call_count += len(result.get("attempts") or [])
            wave_results[step_id] = {
                "success": bool(result.get("success")),
                "provider": result.get("provider"),
                "elapsed_ms": outcome.elapsed_ms,
            }
            if not result.get("success"):
                failed.append(step_id)
        waves.append({"ready_steps": ready, "results": wave_results})
        slice_wave_count += 1
        if failed:
            blocked = sorted(pending)
            timeout_failure = any(
                any(
                    attempt.get("error") == "workflow_timeout_exceeded"
                    for attempt in results[step_id].get("attempts") or []
                )
                for step_id in failed
            )
            return {
                "success": False,
                "status": "timed_out" if timeout_failure else "failed",
                "text": (
                    "Adaptive workflow exhausted its end-to-end time budget."
                    if timeout_failure
                    else f"Adaptive workflow failed at: {', '.join(failed)}"
                ),
                "error": (
                    "workflow_timeout_exceeded" if timeout_failure else "adaptive_step_failed"
                ),
                "failed_steps": failed,
                "blocked_steps": blocked,
                "plan": normalized,
                "results": results,
                "waves": waves,
                "leaf_calls": call_count,
            }

    final = results[normalized["final_step"]]
    return {
        "success": bool(final.get("success")),
        "status": "completed" if final.get("success") else "failed",
        "text": str(final.get("text") or ""),
        "plan": normalized,
        "results": results,
        "waves": waves,
        "leaf_calls": call_count,
        "final_step": normalized["final_step"],
    }
