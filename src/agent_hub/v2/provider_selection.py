"""Which provider runs a step, decided without predicting which one is better.

This replaces a scoring router. That router blended observed statistics with a
user-editable prior, ranked every provider, and could promote its own pick over
the planner's. A pre-registered experiment ran ten durable runs with the gate
wide open and the planner alternating between two providers: the selected
provider changed zero times, and every decision recorded the same cold-start
reason. The apparatus had never once changed an outcome, so its cost was all
downside -- two modules, three tables, a scoring model, and a promotion gate
that could quietly send work somewhere the caller did not ask for.

What survived is the part that was doing real work all along, and none of it
needs statistics:

  eligibility  a provider that is not allowlisted, cannot serve the capability,
               is not ready, has an open circuit, or whose context window is too
               small for the assembled input, cannot run this step.
  order        eligible providers are tried in the caller's allowlist order.
               The caller stated a preference; honouring it is the whole answer.
  model        the model string the caller resolved for that provider, and the
               input limit that goes with it.

The planner's provider is always used when it is eligible. When it is not, an
explicitly pinned provider is an error -- the caller named it, so silently
substituting would be a lie -- and an unpinned one falls through to the next
eligible provider in allowlist order.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .contracts import validate_task
from .errors import HubV2Error
from .provider_manifests import builtin_provider_manifests, model_input_limit

# Ordered: the first reason that applies is the one reported, and the order runs
# from "the caller excluded it" to "it is momentarily unusable" to "this
# specific input does not fit", which is the order a reader asks the questions in.
EXCLUSION_REASONS = (
    "not_allowed",
    "capability_unsupported",
    "not_ready",
    "circuit_open",
    "context_limit",
)


def _exclusion_reason(
    provider: str,
    manifest: Mapping[str, Any],
    *,
    capability: str,
    allowlist: set[str],
    readiness: Mapping[str, bool],
    circuit_open: Mapping[str, bool],
    estimated_input_tokens: int | None,
    max_input_tokens: int,
) -> str | None:
    if provider not in allowlist:
        return "not_allowed"
    if capability not in manifest["capabilities"]:
        return "capability_unsupported"
    if not readiness.get(provider, False):
        return "not_ready"
    if circuit_open.get(provider, False):
        return "circuit_open"
    if estimated_input_tokens is not None and estimated_input_tokens > max_input_tokens:
        return "context_limit"
    return None


def select_provider(
    *,
    task: Mapping[str, Any],
    planner_provider: str,
    provider_allowlist: Sequence[str],
    readiness: Mapping[str, bool],
    pinned: bool = False,
    circuit_open: Mapping[str, bool] | None = None,
    models: Mapping[str, str] | None = None,
    model_limits: Mapping[str, Mapping[str, Any]] | None = None,
    estimated_input_tokens: int | None = None,
    # Which excluded providers are only excluded because their owner is signed
    # out, and the command that fixes each. Supplied by the caller so this stays
    # a pure function of its arguments.
    login_commands: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Pick the provider and the fallback order for one step.

    Pure: it reads no stored history and writes nothing. The same arguments
    always give the same answer, which is what makes a failed run reproducible.
    """

    normalized = validate_task(task)
    capability = normalized["capability"]
    allowlist = set(provider_allowlist)
    manifests = {item["provider_id"]: item for item in builtin_provider_manifests()}

    candidates: list[dict[str, Any]] = []
    for provider, manifest in manifests.items():
        model = str((models or {}).get(provider) or "")
        input_limit = model_input_limit(
            provider,
            model,
            observed=(model_limits or {}).get(provider),
        )
        reason = _exclusion_reason(
            provider,
            manifest,
            capability=capability,
            allowlist=allowlist,
            readiness=readiness,
            circuit_open=dict(circuit_open or {}),
            estimated_input_tokens=estimated_input_tokens,
            max_input_tokens=int(input_limit["max_input_tokens"]),
        )
        candidates.append(
            {
                "provider": provider,
                "model": model or None,
                "eligible": reason is None,
                "excluded_reason": reason,
                "max_input_tokens": input_limit["max_input_tokens"],
                "context_limit_source": input_limit["source"],
                "login_command": (login_commands or {}).get(provider),
            }
        )

    # Allowlist order is the caller's stated preference. Providers outside it are
    # never eligible, so their position does not matter.
    preference = {provider: index for index, provider in enumerate(provider_allowlist)}
    eligible = sorted(
        (item for item in candidates if item["eligible"]),
        key=lambda item: (preference.get(item["provider"], len(preference)), item["provider"]),
    )

    if not eligible:
        _raise_nothing_eligible(candidates, estimated_input_tokens=estimated_input_tokens)

    planner_candidate = next(
        (item for item in eligible if item["provider"] == planner_provider),
        None,
    )
    if planner_candidate is not None:
        selected, reason_code = planner_candidate, "planner_provider_eligible"
    elif pinned:
        _raise_pinned_unavailable(
            candidates,
            planner_provider=planner_provider,
            estimated_input_tokens=estimated_input_tokens,
        )
    else:
        selected, reason_code = eligible[0], "planner_provider_ineligible"

    return {
        "schema": "agent_hub_provider_selection_v1",
        "selected_provider": selected["provider"],
        "model": selected["model"],
        "max_input_tokens": selected["max_input_tokens"],
        "context_limit_source": selected["context_limit_source"],
        "planner_provider": planner_provider,
        "pinned": bool(pinned),
        "reason_code": reason_code,
        "candidates": candidates,
        # Everything eligible after the selection, in the order to try it.
        "fallbacks": [
            item["provider"] for item in eligible if item["provider"] != selected["provider"]
        ],
    }


def _raise_nothing_eligible(
    candidates: Sequence[Mapping[str, Any]],
    *,
    estimated_input_tokens: int | None,
) -> None:
    context_blocked = [item for item in candidates if item["excluded_reason"] == "context_limit"]
    if context_blocked:
        # Distinguish "too big for anyone" from "nobody is available", because
        # the caller's remedy differs: shrink the input versus wait or reconfigure.
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
    _raise_signed_out(candidates)
    raise HubV2Error(
        "no_eligible_provider",
        "No ready provider satisfies the task policy.",
        scope="routing",
    )


def _raise_signed_out(candidates: Sequence[Mapping[str, Any]]) -> None:
    """Say which sign-in is missing, when that is the only thing wrong.

    "No ready provider satisfies the task policy" is true and useless: it reads
    like a policy problem when the actual state is that Codex is logged out and
    one command fixes it. Only raised when every excluded candidate is excluded
    for that reason, so a genuine policy or capability exclusion still reports
    itself.
    """

    # Providers outside the allowlist were never in the running, so they say
    # nothing about why the ones that were are unavailable.
    considered = [item for item in candidates if item["excluded_reason"] != "not_allowed"]
    blocked = [item for item in considered if item["excluded_reason"] == "not_ready"]
    if not blocked or len(blocked) != len(considered):
        return
    signed_out = [item for item in blocked if item.get("login_command")]
    if len(signed_out) != len(blocked):
        return
    raise HubV2Error(
        "provider_login_required",
        "; ".join(
            f"{item['provider']} is signed out -- run: {item['login_command']}"
            for item in signed_out
        ),
        scope="provider",
        retryable=False,
        safe_details={
            "providers": [str(item["provider"]) for item in signed_out],
            "commands": [str(item["login_command"]) for item in signed_out],
        },
    )


def _raise_pinned_unavailable(
    candidates: Sequence[Mapping[str, Any]],
    *,
    planner_provider: str,
    estimated_input_tokens: int | None,
) -> None:
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
