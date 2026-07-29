"""LLM-planned, locally validated adaptive orchestration."""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
from hashlib import sha256
import json
import re
from typing import Any, Dict, List, Mapping, Sequence

from agent_hub import capabilities, consistency, provider_registry


PLAN_SCHEMA = "agent_hub_plan_v1"
MAX_PLAN_STEPS = 12
MAX_INSTRUCTION_CHARS = 4_000
_STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FENCED_JSON_RE = re.compile(r"\A```(?:json)?\s*\n?(.*?)\n?```\s*\Z", re.I | re.S)

CAPABILITY_PROVIDERS: Dict[str, Sequence[str]] = {
    "chat": provider_registry.providers_supporting("chat", planner_only=True),
    "inspect_codebase": provider_registry.providers_supporting("chat", planner_only=True),
    "search": provider_registry.providers_supporting("search", planner_only=True),
    "write": provider_registry.providers_supporting("write", planner_only=True),
    "review_text": provider_registry.providers_supporting("chat", planner_only=True),
    "review_diff": provider_registry.providers_supporting("review_diff", planner_only=True),
    "compare": ("multiple",),
    "verify": ("local",),
    "release_snapshot": ("local",),
    "release_draft": provider_registry.providers_supporting("release_draft", planner_only=True),
}
_PROVIDER_CAPABILITY = {
    "chat": "chat",
    "inspect_codebase": "chat",
    "search": "search",
    "write": "write",
    "review_text": "chat",
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
    "min_successes",
    "quality_rewrite_attempts",
    "provider_calls_per_attempt",
    "estimated_max_provider_calls",
}
REASONING_EFFORTS = ("low", "medium", "high")
INVESTIGATION_DEPTHS = ("shallow", "standard", "deep")


def capability_manifest(
    *,
    allowed_capabilities: Sequence[str] | None = None,
    allowed_providers: Sequence[str] | None = None,
) -> Dict[str, Any]:
    """What the planner may choose from.

    `allowed_providers` is the run's approved egress destinations. The static
    table says which providers *can* serve a capability; the caller's approval
    says which ones this run *may* reach. Telling the planner the wider set and
    then judging it against the narrower one is how a plan gets thrown away for
    obeying its own instructions.
    """

    allowed = set(allowed_capabilities or CAPABILITY_PROVIDERS)
    approved = {str(item) for item in allowed_providers} if allowed_providers else None
    manifest: Dict[str, Any] = {}
    for name, providers in CAPABILITY_PROVIDERS.items():
        if name not in allowed:
            continue
        usable = [item for item in providers if approved is None or item in approved]
        if not usable:
            # No approved provider can serve this capability, so offering it
            # would only invite a step the runtime has to reject.
            continue
        manifest[name] = {"providers": usable, "parallel_safe": name not in {"verify"}}
    return manifest


def planner_prompt(
    goal: str,
    *,
    facts: str = "",
    max_steps: int = MAX_PLAN_STEPS,
    allowed_capabilities: Sequence[str] | None = None,
    allowed_providers: Sequence[str] | None = None,
) -> str:
    allowed = set(allowed_capabilities or CAPABILITY_PROVIDERS)
    manifest = json.dumps(
        capability_manifest(
            allowed_capabilities=tuple(allowed),
            allowed_providers=allowed_providers,
        ),
        ensure_ascii=False,
        sort_keys=True,
    )
    fact_block = facts.strip()[:8_000] or "[no repository fact pack supplied]"
    review_diff_rule = (
        "- review_diff reads only the repository working-tree diff; it never reviews dependency "
        "text.\n"
        if "review_diff" in allowed
        else ""
    )
    compare_rules = (
        "- Use compare only when multiple independent judgments materially help.\n"
        "- A compare normally requires at least two successful participants.\n"
        if "compare" in allowed
        else ""
    )
    verify_rule = (
        "- verify is a deterministic local text check, not an LLM judge.\n"
        if "verify" in allowed
        else ""
    )
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
      "final": false
    }}
  ]
}}

Rules:
- Create at most {max_steps} steps, usually 2-6. Do not add ceremonial steps.
- Do not invent capabilities, providers, tools, files, or facts.
- Every provider and every fallback_providers entry must appear under that step's capability in
  the manifest above. No other provider is reachable for this run, and one unreachable name makes
  the whole plan invalid. When a capability lists a single provider, leave fallback_providers empty.
- Use inspect_codebase for local repository understanding. Use search only for external/web facts.
- Use review_text to review a generated draft or another dependency output.
{review_diff_rule.rstrip()}
- Choose reasoning_effort per step. Use low for mechanical work, medium for normal analysis, and high
  for ambiguous architecture, broad codebase investigation, difficult review, or final synthesis.
- Omit every capability-specific field unless the step has that exact capability:
  investigation_depth is only for inspect_codebase; quality_rewrite_attempts is only for write;
  participants, min_successes, and decision_labels are only for compare when compare is allowed.
- For inspect_codebase, choose investigation_depth from shallow, standard, or deep. Use deep when a
  durable repository document must cover entry points, public schemas, configuration, tests,
  generated docs, and Git state.
- For write, quality_rewrite_attempts must be an integer from 0 through 2.
- Make inspect_codebase instructions name the relevant subsystems, paths, commands, or symbols that
  must be proven. The gatherer uses those details for a broad scan followed by focused deep reads.
- Require file:line evidence for repository claims and distinguish complete files from partial excerpts.
- Express true data dependencies only. Independent steps must have the same dependency frontier so
  the scheduler can run them concurrently. Do not encode an arbitrary provider order.
- Every non-final step must feed, directly or transitively, the one final step.
{compare_rules.rstrip()}
{verify_rule.rstrip()}

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
    allowed_capabilities: Sequence[str] | None = None,
    allowed_providers: Sequence[str] | None = None,
) -> Dict[str, Any]:
    """Validate an LLM plan as untrusted input and return a normalized copy.

    `allowed_providers` is checked here rather than only at the runtime fence so
    that a plan naming an unapproved destination is a local validation failure,
    which the planner repair loop can feed back and correct. The fence in the
    service stays as the authority; this only stops the plan earlier.
    """

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

    allowed = set(allowed_capabilities or CAPABILITY_PROVIDERS)
    approved_providers = {str(item) for item in allowed_providers} if allowed_providers else None
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
        if capability not in CAPABILITY_PROVIDERS or capability not in allowed:
            raise ValueError(f"unsupported capability: {capability}")
        if not _provider_supported(capability, provider):
            raise ValueError(f"provider {provider!r} does not support capability {capability!r}")
        if approved_providers is not None and provider not in approved_providers:
            raise ValueError(
                f"{step_id}.provider {provider!r} is not an approved destination for this run; "
                f"choose one of: {', '.join(sorted(approved_providers))}"
            )
        dependencies = _unique_strings(raw.get("depends_on", []), field=f"{step_id}.depends_on")
        fallbacks = _unique_strings(
            raw.get("fallback_providers", []), field=f"{step_id}.fallback_providers"
        )
        if provider in fallbacks:
            raise ValueError(f"{step_id}.fallback_providers repeats the primary provider")
        if any(not _provider_supported(capability, item) for item in fallbacks):
            raise ValueError(f"{step_id} has an incompatible fallback provider")
        if approved_providers is not None and not set(fallbacks).issubset(approved_providers):
            raise ValueError(
                f"{step_id}.fallback_providers names a destination this run has not approved; "
                f"approved destinations are: {', '.join(sorted(approved_providers))}"
            )
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
            raise ValueError(f"{step_id}.investigation_depth is only valid for inspect_codebase")

        participants: List[str] = []
        decision_labels: List[str] = []
        min_successes: int | None = None
        if capability == "compare":
            participants = _unique_strings(
                raw.get("participants", list(provider_registry.DEFAULT_COMPARE_PROVIDERS)),
                field=f"{step_id}.participants",
            )
            compare_providers = set(
                provider_registry.providers_supporting("compare", planner_only=True)
            )
            if not 2 <= len(participants) <= len(compare_providers) or any(
                item not in compare_providers for item in participants
            ):
                raise ValueError(
                    f"{step_id}.participants must contain 2-{len(compare_providers)} "
                    "model providers"
                )
            raw_min_successes = raw.get("min_successes", min(2, len(participants)))
            if isinstance(raw_min_successes, bool) or not isinstance(raw_min_successes, int):
                raise ValueError(f"{step_id}.min_successes must be an integer")
            min_successes = raw_min_successes
            if not 1 <= min_successes <= len(participants):
                raise ValueError(f"{step_id}.min_successes must be within 1..{len(participants)}")
            if raw.get("decision_labels"):
                decision_labels = consistency.validate_labels(raw["decision_labels"])
        elif (
            raw.get("participants")
            or raw.get("decision_labels")
            or raw.get("min_successes") is not None
        ):
            raise ValueError(
                f"{step_id} may use participants/min_successes/decision_labels only with compare"
            )

        quality_rewrite_attempts: int | None = None
        if capability == "write":
            raw_rewrites = raw.get("quality_rewrite_attempts", 2)
            if isinstance(raw_rewrites, bool) or not isinstance(raw_rewrites, int):
                raise ValueError(f"{step_id}.quality_rewrite_attempts must be an integer")
            quality_rewrite_attempts = raw_rewrites
            if not 0 <= quality_rewrite_attempts <= 2:
                raise ValueError(f"{step_id}.quality_rewrite_attempts must be within 0..2")
        elif raw.get("quality_rewrite_attempts") is not None:
            raise ValueError(f"{step_id}.quality_rewrite_attempts is only valid for write")

        if capability in {"verify", "release_snapshot"}:
            provider_calls_per_attempt = 0
        elif capability == "compare":
            provider_calls_per_attempt = len(participants)
        elif capability == "write":
            provider_calls_per_attempt = 1 + int(quality_rewrite_attempts or 0)
        else:
            provider_calls_per_attempt = 1
        estimated_max_provider_calls = provider_calls_per_attempt * (1 + len(fallbacks))

        normalized_steps.append(
            {
                "id": step_id,
                "capability": capability,
                "provider": provider,
                "depends_on": dependencies,
                "fallback_providers": fallbacks,
                "instruction": instruction,
                "reasoning_effort": reasoning_effort,
                **({"investigation_depth": investigation_depth} if investigation_depth else {}),
                "final": bool(raw.get("final", False)),
                **({"participants": participants} if participants else {}),
                **({"min_successes": min_successes} if min_successes is not None else {}),
                **({"decision_labels": decision_labels} if decision_labels else {}),
                **(
                    {"quality_rewrite_attempts": quality_rewrite_attempts}
                    if quality_rewrite_attempts is not None
                    else {}
                ),
                "provider_calls_per_attempt": provider_calls_per_attempt,
                "estimated_max_provider_calls": estimated_max_provider_calls,
            }
        )
        ids.append(step_id)

    id_set = set(ids)
    steps_by_id = {step["id"]: step for step in normalized_steps}
    graph: Dict[str, set[str]] = {}
    for step in normalized_steps:
        deps = set(step["depends_on"])
        unknown = deps - id_set
        if unknown:
            raise ValueError(f"{step['id']} depends on unknown steps: {sorted(unknown)}")
        if step["id"] in deps:
            raise ValueError(f"{step['id']} cannot depend on itself")
        if step["capability"] == "review_text" and not deps:
            raise ValueError(f"{step['id']}.review_text requires at least one dependency")
        graph[step["id"]] = deps
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise ValueError("adaptive plan contains a dependency cycle") from exc
    for step in normalized_steps:
        if step["capability"] != "review_diff":
            continue
        ancestors: set[str] = set()
        frontier = list(graph[step["id"]])
        while frontier:
            dependency = frontier.pop()
            if dependency in ancestors:
                continue
            ancestors.add(dependency)
            frontier.extend(graph[dependency])
        if any(steps_by_id[dependency]["capability"] == "write" for dependency in ancestors):
            raise ValueError(
                f"{step['id']}.review_diff cannot review generated write output; use review_text"
            )

    finals = [step for step in normalized_steps if step["final"]]
    if len(finals) != 1:
        raise ValueError("adaptive plan must contain exactly one final step")
    final_id = finals[0]["id"]
    if any(final_id in step["depends_on"] for step in normalized_steps):
        raise ValueError("the final step must be a DAG sink")
    if finals[0]["capability"] not in {
        "chat",
        "write",
        "review_text",
        "compare",
        "release_draft",
    }:
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

    expected_calls = sum(int(step["estimated_max_provider_calls"]) for step in normalized_steps)
    if expected_calls > int(max_calls):
        raise ValueError(
            f"plan may require {expected_calls} calls, exceeding max_calls={max_calls}"
        )

    normalized = {
        "schema": PLAN_SCHEMA,
        "goal": goal,
        "rationale": str(plan.get("rationale") or "").strip(),
        "steps": normalized_steps,
        "final_step": final_id,
        "expected_max_calls": expected_calls,
        "expected_max_provider_calls": expected_calls,
    }
    material = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    normalized["plan_sha256"] = sha256(material.encode("utf-8")).hexdigest()
    return normalized
