"""Deterministic statistical routing layered beneath planner recommendations."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .contracts import ROUTING_MODES, validate_task
from .errors import HubV2Error
from .provider_manifests import builtin_provider_manifests, model_input_limit
from .store import HubStore

QUALITY_WEIGHT = 0.60
RELIABILITY_WEIGHT = 0.20
LATENCY_WEIGHT = 0.10
TOKEN_WEIGHT = 0.10
AUTO_MIN_SAMPLES = 20
AUTO_MAX_QUALITY_REGRESSION = 0.03
AUTO_MAX_FAILURE_REGRESSION = 0.02
AUTO_MIN_QUALITY_GAIN = 0.05
AUTO_MIN_EFFICIENCY_GAIN = 0.10


def routing_context(
    task: Mapping[str, Any],
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    normalized = validate_task(task)
    inline_chars = len(normalized.get("inline_input") or "")
    if inline_chars < 4_000:
        size_bucket = "small"
    elif inline_chars < 50_000:
        size_bucket = "medium"
    else:
        size_bucket = "large"
    return {
        "capability": normalized["capability"],
        "task_family": normalized["capability"],
        "model": model or "",
        "reasoning_effort": reasoning_effort or "",
        "input_size_bucket": size_bucket,
        "language": "ko"
        if any("\uac00" <= char <= "\ud7a3" for char in normalized["intent"])
        else "other",
    }


def _efficiency(value: float | None, *, target: float) -> float:
    if value is None:
        return 0.5
    return max(0.0, min(1.0, target / max(target, float(value))))


def _relative_reduction(candidate: float | None, baseline: float | None) -> float:
    if candidate is None or baseline is None or baseline <= 0:
        return 0.0
    return (baseline - candidate) / baseline


def _auto_gate(
    recommendation: Mapping[str, Any],
    planner: Mapping[str, Any],
) -> tuple[bool, str]:
    if (
        int(recommendation["sample_count"]) < AUTO_MIN_SAMPLES
        or int(planner["sample_count"]) < AUTO_MIN_SAMPLES
    ):
        return False, "cold_start_preserves_planner"
    recommended_components = recommendation["components"]
    planner_components = planner["components"]
    quality_delta = float(recommended_components["quality"]) - float(planner_components["quality"])
    recommended_failure = recommendation.get("failure_rate")
    planner_failure = planner.get("failure_rate")
    if recommended_failure is None or planner_failure is None:
        return False, "insufficient_failure_evidence"
    failure_delta = float(recommended_failure) - float(planner_failure)
    if quality_delta < -AUTO_MAX_QUALITY_REGRESSION:
        return False, "quality_guardrail"
    if failure_delta > AUTO_MAX_FAILURE_REGRESSION:
        return False, "reliability_guardrail"
    latency_gain = _relative_reduction(
        recommendation.get("latency_ms"),
        planner.get("latency_ms"),
    )
    token_gain = _relative_reduction(
        recommendation.get("total_tokens"),
        planner.get("total_tokens"),
    )
    if not (
        quality_delta >= AUTO_MIN_QUALITY_GAIN
        or latency_gain >= AUTO_MIN_EFFICIENCY_GAIN
        or token_gain >= AUTO_MIN_EFFICIENCY_GAIN
    ):
        return False, "no_material_gain"
    return True, "statistical_auto"


def route(
    *,
    store: HubStore,
    task: Mapping[str, Any],
    planner_provider: str,
    routing_mode: str,
    provider_allowlist: Sequence[str],
    readiness: Mapping[str, bool],
    circuit_open: Mapping[str, bool] | None = None,
    models: Mapping[str, str] | None = None,
    model_limits: Mapping[str, Mapping[str, Any]] | None = None,
    estimated_input_tokens: int | None = None,
    run_id: str | None = None,
    step_id: str | None = None,
    policy_revision: int = 0,
) -> dict[str, Any]:
    if routing_mode not in ROUTING_MODES:
        raise HubV2Error(
            "invalid_routing_mode",
            "The routing mode is not supported.",
            scope="routing",
        )
    normalized = validate_task(task)
    capability = normalized["capability"]
    allowlist = set(provider_allowlist)
    manifests = {item["provider_id"]: item for item in builtin_provider_manifests()}
    candidates: list[dict[str, Any]] = []
    for provider, manifest in manifests.items():
        excluded_reason = None
        model = str((models or {}).get(provider) or "")
        input_limit = model_input_limit(
            provider,
            model,
            observed=(model_limits or {}).get(provider),
        )
        if provider not in allowlist:
            excluded_reason = "not_allowed"
        elif capability not in manifest["capabilities"]:
            excluded_reason = "capability_unsupported"
        elif not readiness.get(provider, False):
            excluded_reason = "not_ready"
        elif (circuit_open or {}).get(provider, False):
            excluded_reason = "circuit_open"
        elif (
            estimated_input_tokens is not None
            and estimated_input_tokens > input_limit["max_input_tokens"]
        ):
            excluded_reason = "context_limit"
        context = routing_context(
            normalized,
            model=model,
        )
        stats = store.routing_statistics(context=context, provider=provider)
        score = (
            QUALITY_WEIGHT * stats["quality"]
            + RELIABILITY_WEIGHT * stats["reliability"]
            + LATENCY_WEIGHT * _efficiency(stats["latency_ms"], target=30_000.0)
            + TOKEN_WEIGHT * _efficiency(stats["total_tokens"], target=8_000.0)
        )
        candidates.append(
            {
                "provider": provider,
                "model": model or None,
                "eligible": excluded_reason is None,
                "excluded_reason": excluded_reason,
                "max_input_tokens": input_limit["max_input_tokens"],
                "context_limit_source": input_limit["source"],
                "sample_count": stats["sample_count"],
                "score": round(score, 6),
                "components": {
                    "quality": stats["quality"],
                    "reliability": stats["reliability"],
                    "latency_efficiency": _efficiency(
                        stats["latency_ms"],
                        target=30_000.0,
                    ),
                    "token_efficiency": _efficiency(
                        stats["total_tokens"],
                        target=8_000.0,
                    ),
                },
                "failure_rate": stats["failure_rate"],
                "latency_ms": stats["latency_ms"],
                "total_tokens": stats["total_tokens"],
            }
        )
    eligible = [item for item in candidates if item["eligible"]]
    if not eligible:
        context_blocked = [
            item for item in candidates if item["excluded_reason"] == "context_limit"
        ]
        if context_blocked:
            raise HubV2Error(
                "provider_context_limit",
                "No ready provider can accept the assembled input context.",
                scope="context",
                retryable=False,
                safe_details={
                    "estimated_input_tokens": estimated_input_tokens,
                    "largest_candidate_max_input_tokens": max(
                        int(item["max_input_tokens"]) for item in context_blocked
                    ),
                    "blocked_provider_count": len(context_blocked),
                },
            )
        raise HubV2Error(
            "no_eligible_provider",
            "No ready provider satisfies the task policy.",
            scope="routing",
        )
    eligible.sort(key=lambda item: (-item["score"], item["provider"]))
    recommendation = eligible[0]
    planner_candidate = next(
        (item for item in eligible if item["provider"] == planner_provider),
        None,
    )
    if planner_candidate is None:
        if routing_mode == "pinned":
            pinned_candidate = next(
                (item for item in candidates if item["provider"] == planner_provider),
                None,
            )
            if pinned_candidate and pinned_candidate["excluded_reason"] == "context_limit":
                raise HubV2Error(
                    "provider_context_limit",
                    "The pinned provider cannot accept the assembled input context.",
                    scope="context",
                    retryable=False,
                    safe_details={
                        "provider": planner_provider,
                        "model": pinned_candidate["model"],
                        "estimated_input_tokens": estimated_input_tokens,
                        "max_input_tokens": pinned_candidate["max_input_tokens"],
                    },
                )
            raise HubV2Error(
                "pinned_provider_unavailable",
                "The pinned provider is not eligible for this task.",
                scope="routing",
                retryable=True,
            )
        selected = recommendation
        reason_code = "planner_provider_ineligible"
    elif routing_mode == "auto":
        promoted, reason_code = _auto_gate(recommendation, planner_candidate)
        selected = recommendation if promoted else planner_candidate
    else:
        selected = planner_candidate
        reason_code = (
            "cold_start_preserves_planner"
            if routing_mode == "auto"
            else f"{routing_mode}_preserves_planner"
        )
    decision = store.record_routing_decision(
        run_id=run_id,
        step_id=step_id,
        routing_mode=routing_mode,
        selected_provider=selected["provider"],
        planner_provider=planner_provider,
        candidates=candidates,
        scores={item["provider"]: item["score"] for item in candidates if item["eligible"]},
        sample_count=selected["sample_count"],
        policy_revision=policy_revision,
    )
    decision.update(
        {
            "recommended_provider": recommendation["provider"],
            "reason_code": reason_code,
            "weights": {
                "quality": QUALITY_WEIGHT,
                "reliability": RELIABILITY_WEIGHT,
                "latency": LATENCY_WEIGHT,
                "token_efficiency": TOKEN_WEIGHT,
            },
        }
    )
    return decision
