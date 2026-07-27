"""One table deciding what a failed step may do next.

This used to be five independent decisions -- a default error type in the
provider runtime, a hardcoded retryable set in the worker, a hardcoded ambiguous
set in the fallback loop, a derived boolean in the wave, and a special case for
one code in the store. They disagreed, and the disagreement is what left runs
permanently stuck: a provider failure that fell through to the runtime's default
was marked non-retryable, so the wave recorded retry_safe=False and the store
then refused to requeue it.

The question that decides the class is not "did it fail" but "did we get an
answer":

  retry_safe  Nothing was sent, or the provider answered by refusing to do the
              work (rate limit, capacity, a request we can shrink). Re-sending
              is harmless, so the runtime may requeue on its own.
  ambiguous   A request went out and no answer came back -- timeout, dead
              worker, unreadable response. The provider may have run it. Never
              retried automatically; an operator adjudicates through
              agent_hub_cancel's reconciliation.
  terminal    We got an answer and the answer is final for this attempt: the
              request was rejected on its merits, a local check failed, or a
              budget ran out. Replanning may still replace the step.

An unrecognized code is ambiguous, never retry_safe. Provider payloads carry
error strings this repository does not control, so unknown codes are expected
rather than exceptional -- and guessing "safe" about a request that may already
have run is the mistake this table exists to prevent. The cost of the
conservative default is that the run parks in outcome_unknown awaiting
reconciliation, which is a strictly better resting place than the failed,
non-retryable, non-reconcilable state that stranded runs before.
"""

from __future__ import annotations

from typing import Literal

FailureClass = Literal["retry_safe", "ambiguous", "terminal"]

UNCLASSIFIED_PROVIDER_FAILURE = "provider_unclassified_failure"

FAILURE_CLASSES: dict[str, FailureClass] = {
    # -- Nothing was dispatched: refused before the request left this machine.
    "provider_worker_unavailable": "retry_safe",
    "provider_egress_unavailable": "retry_safe",
    "no_eligible_provider": "retry_safe",
    "pinned_provider_unavailable": "retry_safe",
    "circuit_open": "retry_safe",
    # -- The provider answered by declining the work rather than doing it.
    "rate_limit": "retry_safe",
    "temporary_unavailable": "retry_safe",
    "provider_context_limit": "retry_safe",
    "request_too_large": "retry_safe",
    "provider_response_too_large": "retry_safe",
    # Every candidate was tried and each one was retry_safe: the fallback loop
    # re-raises instead of advancing the moment an attempt is anything else, so
    # this summary can only be reached over requests that were never delivered
    # or were declined. If that loop changes, this entry changes with it.
    "fallback_exhausted": "retry_safe",
    # -- Dispatched and no answer came back.
    "provider_timeout": "ambiguous",
    "provider_worker_failed": "ambiguous",
    "provider_protocol_error": "ambiguous",
    "codex_timeout": "ambiguous",
    "codex_process_error": "ambiguous",
    "unsupported_protocol_version": "ambiguous",
    # The provider reported a failure without naming it. We cannot tell from
    # this whether the work ran, which is exactly the state that stranded runs.
    UNCLASSIFIED_PROVIDER_FAILURE: "ambiguous",
    # -- Answered, and the answer is final for this attempt.
    # The provider ran and rejected the request on its merits.
    "provider_operation_failed": "terminal",
    "provider_request_failed": "terminal",
    "model_list_failed": "terminal",
    "unknown_provider": "terminal",
    "unknown_worker_method": "terminal",
    "unsupported_worker_capability": "terminal",
    "unsupported_capability": "terminal",
    "capability_changed": "terminal",
    "invalid_model_id": "terminal",
    "invalid_provider_manifest": "terminal",
    "provider_sandbox_invalid_path": "terminal",
    # A local check rejected the request or its result. Re-running changes
    # nothing until the plan or the policy changes.
    "invalid_request": "terminal",
    "invalid_plan": "terminal",
    "unsupported_schema": "terminal",
    "deterministic_verification_failed": "terminal",
    "invalid_verifier": "terminal",
    "inspection_incomplete": "terminal",
    "planner_execution_failed": "terminal",
    "planner_protocol_error": "terminal",
    "planner_capability_violation": "terminal",
    "planner_egress_violation": "terminal",
    "planner_scope_violation": "terminal",
    # Policy and egress: denied until a human changes the decision.
    "egress_denied": "terminal",
    "egress_policy_denied": "terminal",
    "egress_approval_required": "terminal",
    "egress_approval_conflict": "terminal",
    "sensitive_source_denied": "terminal",
    "provider_policy_denied": "terminal",
    "model_policy_denied": "terminal",
    # Budgets: the resource is spent, so a retry would only overspend.
    "run_token_budget_exhausted": "terminal",
    "run_time_budget_exhausted": "terminal",
    "leaf_call_budget_exceeded": "terminal",
    "replan_budget_exhausted": "terminal",
    # The provider answered; handling that answer failed locally.
    "artifact_integrity_failed": "terminal",
    "artifact_content_unavailable": "terminal",
    "artifact_not_text": "terminal",
    "run_internal_error": "terminal",
}


def classify(code: str | None) -> FailureClass:
    return FAILURE_CLASSES.get(str(code or ""), "ambiguous")


def is_known(code: str | None) -> bool:
    return str(code or "") in FAILURE_CLASSES


def is_retryable(code: str | None) -> bool:
    """Whether the runtime may re-dispatch this on its own."""

    return classify(code) == "retry_safe"
