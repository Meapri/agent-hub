"""Deterministic statistical routing layered beneath planner recommendations."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .contracts import ROUTING_MODES, validate_task
from .errors import HubV2Error
from .provider_manifests import builtin_provider_manifests, model_input_limit
from .routing_prior import RoutingPriorSnapshot
from .store import HubStore

QUALITY_WEIGHT = 0.60
RELIABILITY_WEIGHT = 0.20
LATENCY_WEIGHT = 0.10
TOKEN_WEIGHT = 0.10
# quality_balanced reuses the module constants so the documented default and the
# profile table can never drift apart.
ROUTING_PROFILE_WEIGHTS: dict[str, dict[str, float]] = {
    "quality_balanced": {
        "quality": QUALITY_WEIGHT,
        "reliability": RELIABILITY_WEIGHT,
        "latency": LATENCY_WEIGHT,
        "token_efficiency": TOKEN_WEIGHT,
    },
    "latency_first": {
        "quality": 0.35,
        "reliability": 0.25,
        "latency": 0.30,
        "token_efficiency": 0.10,
    },
    "cost_first": {
        "quality": 0.35,
        "reliability": 0.20,
        "latency": 0.05,
        "token_efficiency": 0.40,
    },
}
AUTO_MIN_OBSERVED_SAMPLES = 5
# One runtime routing sample contributes this much weight, so a weight divided
# by it is an effective sample count.
RUNTIME_SAMPLE_WEIGHT = 3.0
MIN_PROPORTION_VARIANCE = 0.01
# A prior may inform a promotion but must never be the bulk of the evidence.
# At the default prior weight this needs roughly six observed samples, which is
# above AUTO_MIN_OBSERVED_SAMPLES rather than below it.
AUTO_MAX_PRIOR_FRACTION = 0.25
# One-sided ~98%. z=1 promoted indistinguishable providers far too often, and
# every wave compares several candidates without any multiple-comparison
# correction, so the bar has to absorb that too.
AUTO_SEPARATION_Z = 2.0
AUTO_MAX_QUALITY_REGRESSION = 0.03
AUTO_MAX_FAILURE_REGRESSION = 0.02
AUTO_MIN_QUALITY_GAIN = 0.05
AUTO_MIN_EFFICIENCY_GAIN = 0.10


def profile_weights(routing_profile: str) -> dict[str, float]:
    weights = ROUTING_PROFILE_WEIGHTS.get(str(routing_profile))
    if weights is None:
        raise HubV2Error(
            "invalid_routing_profile",
            "The routing profile is not supported.",
            scope="routing",
            safe_details={"routing_profile": str(routing_profile)[:64]},
        )
    return dict(weights)


def _binomial_sd(proportion: float, weight: float) -> float:
    """Standard error from an *effective sample count*, not a raw weight.

    Routing weights are not counts: one runtime sample carries
    RUNTIME_SAMPLE_WEIGHT. Feeding the weight straight into sqrt(p(1-p)/n)
    understates the error by sqrt(RUNTIME_SAMPLE_WEIGHT). A floor on the
    variance also keeps a run of identical outcomes (p exactly 0 or 1) from
    reporting zero uncertainty.
    """

    effective_n = max(0.0, float(weight)) / RUNTIME_SAMPLE_WEIGHT
    spread = max(proportion * (1.0 - proportion), MIN_PROPORTION_VARIANCE)
    return (spread / (effective_n + 1.0)) ** 0.5


def _blend_positive(
    prior_value: float | None,
    prior_weight: float,
    observed_value: float | None,
    observed_weight: float,
) -> float | None:
    if prior_value is not None and prior_weight > 0.0:
        if observed_value is None or observed_weight <= 0.0:
            return prior_value
        return (prior_weight * prior_value + observed_weight * observed_value) / (
            prior_weight + observed_weight
        )
    return observed_value


def blend_statistics(
    stats: Mapping[str, Any],
    prior_entry: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Shrink observed statistics toward a prior with a pseudo-weight.

    The prior share is ``w / (w + n)``: it decays automatically as observations
    accumulate, and a handful of bad real outcomes overrides it immediately.
    """

    weight = float(prior_entry["effective_weight"]) if prior_entry else 0.0
    success = float(stats.get("success_weight") or 0.0)
    failure = float(stats.get("failure_weight") or 0.0)
    quality_weight = float(stats.get("quality_weight") or 0.0)
    latency_weight = float(stats.get("latency_weight") or 0.0)
    tokens_weight = float(stats.get("tokens_weight") or 0.0)

    prior_reliability = prior_entry.get("reliability") if prior_entry else None
    if prior_reliability is not None and weight > 0.0:
        reliability = (weight * float(prior_reliability) + success) / (weight + success + failure)
        reliability_n = weight + success + failure
    else:
        reliability = float(stats["reliability"])
        reliability_n = 2.0 + success + failure
    reliability_sd = _binomial_sd(reliability, reliability_n)

    prior_quality = prior_entry.get("quality") if prior_entry else None
    if prior_quality is not None and weight > 0.0:
        if quality_weight <= 0.0:
            quality = float(prior_quality)
        else:
            quality = (weight * float(prior_quality) + quality_weight * float(stats["quality"])) / (
                weight + quality_weight
            )
        quality_n = weight + quality_weight
    else:
        quality = float(stats["quality"])
        quality_n = quality_weight
    # With no quality evidence the value is a placeholder shared by every
    # candidate, so it must contribute no uncertainty either.
    quality_sd = 0.0 if quality_n <= 0.0 else _binomial_sd(quality, quality_n)

    latency_ms = _blend_positive(
        prior_entry.get("latency_ms") if prior_entry else None,
        weight,
        stats["latency_ms"],
        latency_weight,
    )
    total_tokens = _blend_positive(
        prior_entry.get("total_tokens") if prior_entry else None,
        weight,
        stats["total_tokens"],
        tokens_weight,
    )
    if prior_reliability is not None and weight > 0.0:
        failure_rate: float | None = 1.0 - reliability
    else:
        failure_rate = stats["failure_rate"]
    observed_total = success + failure
    prior_fraction = weight / (weight + observed_total) if weight + observed_total > 0 else 0.0
    if weight <= 0.0:
        kind = "observed" if observed_total > 0 else "default"
    else:
        kind = "prior" if observed_total <= 0 else "blended"
    return {
        "quality": quality,
        "reliability": reliability,
        "latency_ms": latency_ms,
        "total_tokens": total_tokens,
        "failure_rate": failure_rate,
        "evidence": {
            "kind": kind,
            "prior_weight": weight,
            "prior_fraction": round(prior_fraction, 6),
            "observed_weight": observed_total,
            "quality_sd": quality_sd,
            "reliability_sd": reliability_sd,
            "quality_weight": quality_weight,
            "latency_weight": latency_weight,
            "tokens_weight": tokens_weight,
        },
    }


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
    *,
    weights: Mapping[str, float],
) -> tuple[bool, str]:
    if (
        int(recommendation["sample_count"]) < AUTO_MIN_OBSERVED_SAMPLES
        or int(planner["sample_count"]) < AUTO_MIN_OBSERVED_SAMPLES
    ):
        return False, "cold_start_preserves_planner"
    recommended_evidence = recommendation["evidence"]
    planner_evidence = planner["evidence"]
    # A heavier prior demands proportionally more real evidence before it can move
    # a decision, so an assumption alone never promotes a provider.
    if (
        max(
            float(recommended_evidence["prior_fraction"]),
            float(planner_evidence["prior_fraction"]),
        )
        > AUTO_MAX_PRIOR_FRACTION
    ):
        return False, "prior_evidence_insufficient"
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
    # An efficiency gain only counts when both sides actually measured it.
    latency_measured = (
        float(recommended_evidence["latency_weight"]) > 0.0
        and float(planner_evidence["latency_weight"]) > 0.0
    )
    tokens_measured = (
        float(recommended_evidence["tokens_weight"]) > 0.0
        and float(planner_evidence["tokens_weight"]) > 0.0
    )
    latency_gain = (
        _relative_reduction(recommendation.get("latency_ms"), planner.get("latency_ms"))
        if latency_measured
        else 0.0
    )
    token_gain = (
        _relative_reduction(recommendation.get("total_tokens"), planner.get("total_tokens"))
        if tokens_measured
        else 0.0
    )
    if not (
        quality_delta >= AUTO_MIN_QUALITY_GAIN
        or latency_gain >= AUTO_MIN_EFFICIENCY_GAIN
        or token_gain >= AUTO_MIN_EFFICIENCY_GAIN
    ):
        return False, "no_material_gain"
    sigma_recommended = (
        (float(weights["quality"]) * float(recommended_evidence["quality_sd"])) ** 2
        + (float(weights["reliability"]) * float(recommended_evidence["reliability_sd"])) ** 2
    ) ** 0.5
    sigma_planner = (
        (float(weights["quality"]) * float(planner_evidence["quality_sd"])) ** 2
        + (float(weights["reliability"]) * float(planner_evidence["reliability_sd"])) ** 2
    ) ** 0.5
    separation = float(recommendation["score"]) - float(planner["score"])
    combined = (sigma_recommended**2 + sigma_planner**2) ** 0.5
    if separation - AUTO_SEPARATION_Z * combined < 0.0:
        return False, "scores_not_separated"
    if (
        max(
            float(recommended_evidence["prior_weight"]),
            float(planner_evidence["prior_weight"]),
        )
        > 0.0
    ):
        return True, "prior_assisted_auto"
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
    routing_profile: str = "quality_balanced",
    prior: RoutingPriorSnapshot | None = None,
) -> dict[str, Any]:
    if routing_mode not in ROUTING_MODES:
        raise HubV2Error(
            "invalid_routing_mode",
            "The routing mode is not supported.",
            scope="routing",
        )
    weights = profile_weights(routing_profile)
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
        prior_entry = (
            prior.lookup(capability=capability, provider=provider, model=model or None)
            if prior is not None
            else None
        )
        blended = blend_statistics(stats, prior_entry)
        score = (
            weights["quality"] * blended["quality"]
            + weights["reliability"] * blended["reliability"]
            + weights["latency"] * _efficiency(blended["latency_ms"], target=30_000.0)
            + weights["token_efficiency"] * _efficiency(blended["total_tokens"], target=8_000.0)
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
                    "quality": blended["quality"],
                    "reliability": blended["reliability"],
                    "latency_efficiency": _efficiency(
                        blended["latency_ms"],
                        target=30_000.0,
                    ),
                    "token_efficiency": _efficiency(
                        blended["total_tokens"],
                        target=8_000.0,
                    ),
                },
                "failure_rate": blended["failure_rate"],
                "latency_ms": blended["latency_ms"],
                "total_tokens": blended["total_tokens"],
                "evidence": blended["evidence"],
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
        promoted, reason_code = _auto_gate(recommendation, planner_candidate, weights=weights)
        selected = recommendation if promoted else planner_candidate
    else:
        selected = planner_candidate
        reason_code = (
            "cold_start_preserves_planner"
            if routing_mode == "auto"
            else f"{routing_mode}_preserves_planner"
        )
    evidence = selected["evidence"]
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
        reason_code=reason_code,
        routing_profile=routing_profile,
        evidence_kind=str(evidence["kind"]),
        prior_sha256=prior.file_sha256 if prior is not None else None,
        prior_revision=prior.revision if prior is not None else None,
        prior_weight_fraction=float(evidence["prior_fraction"]),
    )
    decision.update(
        {
            "recommended_provider": recommendation["provider"],
            "reason_code": reason_code,
            "routing_profile": routing_profile,
            "weights": weights,
            "prior": prior.public() if prior is not None else None,
        }
    )
    if run_id is not None and prior is not None:
        _record_prior_event(
            store,
            run_id=run_id,
            step_id=step_id,
            prior=prior,
            selected=selected,
            capability=capability,
            routing_mode=routing_mode,
            routing_profile=routing_profile,
            reason_code=reason_code,
        )
    return decision


def _record_prior_event(
    store: HubStore,
    *,
    run_id: str,
    step_id: str | None,
    prior: RoutingPriorSnapshot,
    selected: Mapping[str, Any],
    capability: str,
    routing_mode: str,
    routing_profile: str,
    reason_code: str,
) -> None:
    """Audit trail for prior-influenced routing. Never breaks the run."""

    if prior.state == "invalid":
        details: dict[str, Any] = {
            "step_id": step_id or "",
            "reason_code": prior.reason_code,
            "routing_mode": routing_mode,
            "routing_profile": routing_profile,
        }
        event_type = "routing_prior_unavailable"
    elif reason_code == "prior_assisted_auto":
        details = {
            "step_id": step_id or "",
            "provider": selected["provider"],
            "model": selected["model"] or "",
            "capability": capability,
            "reason_code": reason_code,
            "routing_mode": routing_mode,
            "routing_profile": routing_profile,
            "evidence_kind": selected["evidence"]["kind"],
            "prior_sha256": prior.file_sha256,
            "prior_revision": prior.revision,
            "prior_weight_fraction": selected["evidence"]["prior_fraction"],
        }
        event_type = "routing_prior_applied"
    else:
        return
    try:
        store.record_runtime_event(run_id, event_type=event_type, details=details)
    except HubV2Error:
        pass
