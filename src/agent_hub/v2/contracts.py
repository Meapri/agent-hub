"""Strict dependency-free contracts shared by daemon, bridge, and workers."""

from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .errors import HubV2Error

TASK_SCHEMA = "task_v2"
PLAN_SCHEMA = "plan_v2"
RUN_SCHEMA = "run_v3"
EVENT_SCHEMA = "event_v2"
ARTIFACT_SCHEMA = "artifact_v2"
PROVIDER_MANIFEST_SCHEMA = "agent_hub_provider_v2"
ROUTING_DECISION_SCHEMA = "routing_decision_v1"
EGRESS_MANIFEST_SCHEMA = "egress_manifest_v2"
RECONCILIATION_SCHEMA = "agent_hub_run_reconciliation_v1"
MAX_RECONCILED_RESULT_CHARS = 200_000
MAX_INLINE_INPUT_CHARS = 2_000_000
DEFAULT_PROVIDER_MAX_INPUT_TOKENS = 131_072
MAX_PROVIDER_INPUT_TOKENS = 10_000_000

CAPABILITIES = frozenset(
    {
        "chat",
        "search",
        "vision",
        "write",
        "image",
        "inspect",
        "review",
        "decide",
    }
)
RUN_STATUSES = frozenset(
    {
        "prepared",
        "queued",
        "running",
        "waiting_approval",
        "paused",
        "completed",
        "failed",
        "cancelled",
        "archived",
        "outcome_unknown",
    }
)
ROUTING_MODES = frozenset({"pinned", "shadow", "advisory", "auto"})
ROUTING_PROFILES = frozenset({"quality_balanced", "latency_first", "cost_first"})
# Only not_delivered can cause an external request to be sent again.
RECONCILIATION_VERDICTS = frozenset({"not_delivered", "delivered_discarded", "delivered_recovered"})
RECONCILIATION_DISPOSITIONS = frozenset({"resume", "fail"})
RETENTION_MODES = frozenset({"ephemeral", "durable_private", "exportable"})
SENSITIVITY_LEVELS = frozenset({"public", "project", "sensitive", "secret"})

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def digest_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def require_object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HubV2Error(
            "invalid_request",
            f"{field} must be an object.",
            scope="contract",
        )
    return dict(value)


def require_string(
    value: Any,
    *,
    field: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise HubV2Error("invalid_request", f"{field} must be a string.", scope="contract")
    if not allow_empty and not value:
        raise HubV2Error("invalid_request", f"{field} must not be empty.", scope="contract")
    if len(value) > maximum:
        raise HubV2Error(
            "request_too_large",
            f"{field} exceeds the allowed size.",
            scope="contract",
            safe_details={"field": field, "maximum": maximum},
        )
    return value


def require_identifier(value: Any, *, field: str) -> str:
    text = require_string(value, field=field, maximum=128)
    if _ID_RE.fullmatch(text) is None:
        raise HubV2Error(
            "invalid_request",
            f"{field} has an invalid identifier.",
            scope="contract",
        )
    return text


def require_digest(value: Any, *, field: str) -> str:
    text = require_string(value, field=field, maximum=64)
    if _DIGEST_RE.fullmatch(text) is None:
        raise HubV2Error(
            "invalid_request",
            f"{field} must be a lowercase SHA-256 digest.",
            scope="contract",
        )
    return text


def require_non_negative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HubV2Error(
            "invalid_request",
            f"{field} must be a non-negative integer.",
            scope="contract",
        )
    return value


def require_finite_number(value: Any, *, field: str, minimum: float = 0.0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise HubV2Error(
            "invalid_request",
            f"{field} must be a finite number of at least {minimum}.",
            scope="contract",
        )
    return float(value)


def canonical_project_root(value: Any) -> str:
    text = require_string(value, field="project_root", maximum=4096)
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise HubV2Error(
            "invalid_project_root",
            "project_root must be an absolute path.",
            scope="project",
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise HubV2Error(
            "invalid_project_root",
            "project_root does not exist.",
            scope="project",
        ) from exc
    if not resolved.is_dir():
        raise HubV2Error(
            "invalid_project_root",
            "project_root must be a directory.",
            scope="project",
        )
    return str(resolved)


def _string_list(
    value: Any,
    *,
    field: str,
    maximum_items: int,
    item_maximum: int = 128,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum_items:
        raise HubV2Error(
            "invalid_request",
            f"{field} must be an array with at most {maximum_items} items.",
            scope="contract",
        )
    return [require_string(item, field=f"{field}[]", maximum=item_maximum) for item in value]


def _reject_unknown_fields(
    value: Mapping[str, Any],
    *,
    allowed: set[str],
    field: str,
    code: str = "invalid_request",
    scope: str = "contract",
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise HubV2Error(
            code,
            f"{field} contains unsupported fields.",
            scope=scope,
            safe_details={"fields": unknown},
        )


def validate_task(raw: Any) -> dict[str, Any]:
    value = require_object(raw, field="task")
    _reject_unknown_fields(
        value,
        allowed={
            "schema",
            "intent",
            "capability",
            "inline_input",
            "input_artifacts",
            "constraints",
            "output_contract",
            "retention",
        },
        field="task",
    )
    schema = value.get("schema", TASK_SCHEMA)
    if schema != TASK_SCHEMA:
        raise HubV2Error("unsupported_schema", "task schema is not supported.", scope="contract")
    capability = require_string(value.get("capability"), field="capability", maximum=32)
    if capability not in CAPABILITIES:
        raise HubV2Error(
            "unsupported_capability",
            "The requested capability is not supported.",
            scope="contract",
            safe_details={"capability": capability},
        )
    intent = require_string(value.get("intent"), field="intent", maximum=16_000)
    inline_input = value.get("inline_input")
    if inline_input is not None:
        inline_input = require_string(
            inline_input,
            field="inline_input",
            maximum=MAX_INLINE_INPUT_CHARS,
            allow_empty=True,
        )
    input_artifacts = _string_list(
        value.get("input_artifacts"),
        field="input_artifacts",
        maximum_items=100,
    )
    if inline_input is None and not input_artifacts:
        inline_input = ""
    constraints = require_object(value.get("constraints", {}), field="constraints")
    _reject_unknown_fields(
        constraints,
        allowed={
            "provider_allowlist",
            "max_tokens",
            "max_input_tokens",
            "max_output_tokens",
            "max_total_tokens",
            "max_leaf_calls",
            "timeout_seconds",
        },
        field="task.constraints",
    )
    provider_allowlist = _string_list(
        constraints.get("provider_allowlist"),
        field="constraints.provider_allowlist",
        maximum_items=32,
    )
    budgets: dict[str, int | float] = {}
    for key in (
        "max_tokens",
        "max_input_tokens",
        "max_output_tokens",
        "max_total_tokens",
        "max_leaf_calls",
    ):
        if key in constraints:
            budgets[key] = require_non_negative_int(constraints[key], field=f"constraints.{key}")
    legacy_max_tokens = budgets.get("max_tokens")
    if legacy_max_tokens is not None:
        # max_tokens was historically overloaded as both a per-call output
        # limit and an aggregate run limit. Keep those two meanings for old
        # callers, but never use it to shrink the input context window.
        budgets.setdefault("max_output_tokens", legacy_max_tokens)
        budgets.setdefault("max_total_tokens", legacy_max_tokens)
    if "timeout_seconds" in constraints:
        budgets["timeout_seconds"] = require_finite_number(
            constraints["timeout_seconds"],
            field="constraints.timeout_seconds",
            minimum=0.1,
        )
    retention = value.get("retention", "ephemeral")
    if retention not in RETENTION_MODES:
        raise HubV2Error("invalid_request", "retention is not supported.", scope="contract")
    output_contract = require_object(value.get("output_contract", {}), field="output_contract")
    return {
        "schema": TASK_SCHEMA,
        "intent": intent,
        "capability": capability,
        "inline_input": inline_input,
        "input_artifacts": input_artifacts,
        "constraints": {
            "provider_allowlist": provider_allowlist,
            **budgets,
        },
        "output_contract": output_contract,
        "retention": retention,
    }


def input_token_limit(constraints: Mapping[str, Any]) -> int | None:
    value = constraints.get("max_input_tokens")
    return int(value) if value is not None else None


def output_token_limit(
    constraints: Mapping[str, Any],
    *,
    default: int = 131_072,
) -> int:
    value = constraints.get("max_output_tokens", constraints.get("max_tokens", default))
    return int(value)


def total_token_limit(
    constraints: Mapping[str, Any],
    *,
    default: int = 131_072,
) -> int:
    value = constraints.get("max_total_tokens", constraints.get("max_tokens", default))
    return int(value)


def validate_reconciliation_resolutions(
    raw: Any,
    *,
    run_disposition: Any,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Split a reconciliation request into a digest-safe body and its plaintext.

    The normalized body carries only digests, so user-supplied result text can
    never end up inside the proposal digest or the stored proposal row.
    """

    disposition = str(run_disposition or "")
    if disposition not in RECONCILIATION_DISPOSITIONS:
        raise HubV2Error(
            "invalid_request",
            "run_disposition must be resume or fail.",
            scope="run",
        )
    if not isinstance(raw, list) or not 1 <= len(raw) <= 64:
        raise HubV2Error(
            "invalid_request",
            "resolutions must contain 1..64 entries.",
            scope="run",
        )
    resolutions: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    seen: set[str] = set()
    for item in raw:
        value = require_object(item, field="resolutions[]")
        _reject_unknown_fields(
            value,
            allowed={"step_id", "verdict", "result_text"},
            field="resolutions[]",
        )
        step_id = require_identifier(value.get("step_id"), field="resolutions[].step_id")
        if step_id in seen:
            raise HubV2Error(
                "invalid_request",
                "resolutions must not repeat a step id.",
                scope="run",
            )
        seen.add(step_id)
        verdict = str(value.get("verdict") or "")
        if verdict not in RECONCILIATION_VERDICTS:
            raise HubV2Error(
                "invalid_request",
                "The reconciliation verdict is not supported.",
                scope="run",
            )
        entry: dict[str, Any] = {"step_id": step_id, "verdict": verdict}
        if verdict == "delivered_recovered":
            text = require_string(
                value.get("result_text"),
                field="resolutions[].result_text",
                maximum=MAX_RECONCILED_RESULT_CHARS,
            )
            if not text.strip():
                raise HubV2Error(
                    "invalid_request",
                    "delivered_recovered needs the recovered result text.",
                    scope="run",
                )
            texts[step_id] = text
            entry["result_sha256"] = sha256(text.encode("utf-8")).hexdigest()
        elif value.get("result_text") is not None:
            raise HubV2Error(
                "invalid_request",
                "result_text is only valid for delivered_recovered.",
                scope="run",
            )
        resolutions.append(entry)
    resolutions.sort(key=lambda item: item["step_id"])
    return (
        {"run_disposition": disposition, "resolutions": resolutions},
        texts,
    )


def validate_plan(raw: Any) -> dict[str, Any]:
    value = require_object(raw, field="plan")
    _reject_unknown_fields(
        value,
        allowed={
            "schema",
            "task",
            "steps",
            "routing_mode",
            "policy_revision",
            "egress_manifest_sha256",
            "inline_consent_artifacts",
            "request_plan_sha256",
            "plan_sha256",
        },
        field="plan",
        code="invalid_plan",
        scope="planner",
    )
    if value.get("schema") != PLAN_SCHEMA:
        raise HubV2Error("unsupported_schema", "plan schema is not supported.", scope="contract")
    steps_raw = value.get("steps")
    if not isinstance(steps_raw, list) or not 1 <= len(steps_raw) <= 64:
        raise HubV2Error(
            "invalid_plan",
            "plan.steps must contain between 1 and 64 steps.",
            scope="planner",
        )
    steps: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw_step in enumerate(steps_raw):
        step = require_object(raw_step, field=f"steps[{index}]")
        _reject_unknown_fields(
            step,
            allowed={
                "id",
                "capability",
                "depends_on",
                "instruction",
                "routing_requirements",
                "output_contract",
                "verifier",
            },
            field=f"plan.steps[{index}]",
            code="invalid_plan",
            scope="planner",
        )
        step_id = require_identifier(step.get("id"), field=f"steps[{index}].id")
        if step_id in ids:
            raise HubV2Error("invalid_plan", "plan step ids must be unique.", scope="planner")
        ids.add(step_id)
        capability = require_string(
            step.get("capability"),
            field=f"steps[{index}].capability",
            maximum=32,
        )
        if capability not in CAPABILITIES:
            raise HubV2Error(
                "invalid_plan",
                "plan step has an unsupported capability.",
                scope="planner",
            )
        depends_on = _string_list(
            step.get("depends_on"),
            field=f"steps[{index}].depends_on",
            maximum_items=64,
        )
        instruction = require_string(
            step.get("instruction"),
            field=f"steps[{index}].instruction",
            maximum=100_000,
        )
        steps.append(
            {
                "id": step_id,
                "capability": capability,
                "depends_on": depends_on,
                "instruction": instruction,
                "routing_requirements": require_object(
                    step.get("routing_requirements", {}),
                    field=f"steps[{index}].routing_requirements",
                ),
                "output_contract": require_object(
                    step.get("output_contract", {}),
                    field=f"steps[{index}].output_contract",
                ),
                "verifier": require_object(
                    step.get("verifier", {}),
                    field=f"steps[{index}].verifier",
                ),
            }
        )
    for step in steps:
        if step["id"] in step["depends_on"] or any(dep not in ids for dep in step["depends_on"]):
            raise HubV2Error(
                "invalid_plan",
                "plan dependencies must reference other existing steps.",
                scope="planner",
            )
    pending = {step["id"]: set(step["depends_on"]) for step in steps}
    ready = [step_id for step_id, deps in pending.items() if not deps]
    visited: set[str] = set()
    while ready:
        current = ready.pop()
        if current in visited:
            continue
        visited.add(current)
        for step_id, deps in pending.items():
            if current in deps:
                deps.remove(current)
                if not deps:
                    ready.append(step_id)
    if len(visited) != len(steps):
        raise HubV2Error("invalid_plan", "plan dependencies contain a cycle.", scope="planner")
    normalized = {
        "schema": PLAN_SCHEMA,
        "task": validate_task(value.get("task")),
        "steps": steps,
        "routing_mode": value.get("routing_mode", "shadow"),
        "policy_revision": require_non_negative_int(
            value.get("policy_revision", 0),
            field="policy_revision",
        ),
        "egress_manifest_sha256": value.get("egress_manifest_sha256"),
        "inline_consent_artifacts": _string_list(
            value.get("inline_consent_artifacts"),
            field="inline_consent_artifacts",
            maximum_items=100,
        ),
        "request_plan_sha256": value.get("request_plan_sha256"),
    }
    if normalized["routing_mode"] not in ROUTING_MODES:
        raise HubV2Error("invalid_plan", "routing_mode is not supported.", scope="planner")
    if normalized["egress_manifest_sha256"] is not None:
        normalized["egress_manifest_sha256"] = require_digest(
            normalized["egress_manifest_sha256"],
            field="egress_manifest_sha256",
        )
    if normalized["request_plan_sha256"] is not None:
        normalized["request_plan_sha256"] = require_digest(
            normalized["request_plan_sha256"],
            field="request_plan_sha256",
        )
    calculated = digest_json(normalized)
    supplied = value.get("plan_sha256")
    if supplied is not None and require_digest(supplied, field="plan_sha256") != calculated:
        raise HubV2Error(
            "plan_digest_conflict",
            "The plan changed after its digest was prepared.",
            scope="planner",
        )
    normalized["plan_sha256"] = calculated
    return normalized


def validate_provider_manifest(raw: Any) -> dict[str, Any]:
    value = require_object(raw, field="provider_manifest")
    _reject_unknown_fields(
        value,
        allowed={
            "schema",
            "provider_id",
            "adapter_version",
            "protocol_version",
            "capabilities",
            "reasoning_effort",
            "auth_owner",
            "auth_mode",
            "allowed_domains",
            "supports_cancel",
            "supports_streaming",
            "supports_idempotency",
            "context_limits",
            "settings_schema",
        },
        field="provider_manifest",
        code="invalid_provider_manifest",
        scope="provider",
    )
    if value.get("schema") != PROVIDER_MANIFEST_SCHEMA:
        raise HubV2Error(
            "unsupported_schema",
            "provider manifest schema is not supported.",
            scope="provider",
        )
    provider_id = require_identifier(value.get("provider_id"), field="provider_id")
    adapter_version = require_string(
        value.get("adapter_version"),
        field="adapter_version",
        maximum=64,
    )
    protocol_version = require_string(
        value.get("protocol_version"),
        field="protocol_version",
        maximum=32,
    )
    if protocol_version != "2.0":
        raise HubV2Error(
            "unsupported_protocol_version",
            "provider worker protocol version is not supported.",
            scope="provider",
            safe_details={"supported": ["2.0"]},
        )
    capabilities = _string_list(
        value.get("capabilities"),
        field="capabilities",
        maximum_items=32,
    )
    unknown = sorted(set(capabilities) - CAPABILITIES)
    if unknown:
        raise HubV2Error(
            "invalid_provider_manifest",
            "provider manifest contains unknown capabilities.",
            scope="provider",
            safe_details={"capabilities": unknown},
        )
    domains = _string_list(
        value.get("allowed_domains"),
        field="allowed_domains",
        maximum_items=64,
        item_maximum=253,
    )
    for domain in domains:
        if "/" in domain or ":" in domain or domain.startswith("."):
            raise HubV2Error(
                "invalid_provider_manifest",
                "allowed_domains must contain host names only.",
                scope="provider",
            )
    context_limits = require_object(
        value.get(
            "context_limits",
            {
                "default_max_input_tokens": DEFAULT_PROVIDER_MAX_INPUT_TOKENS,
                "model_overrides": [],
            },
        ),
        field="context_limits",
    )
    _reject_unknown_fields(
        context_limits,
        allowed={"default_max_input_tokens", "model_overrides"},
        field="context_limits",
        code="invalid_provider_manifest",
        scope="provider",
    )
    default_max_input_tokens = require_non_negative_int(
        context_limits.get(
            "default_max_input_tokens",
            DEFAULT_PROVIDER_MAX_INPUT_TOKENS,
        ),
        field="context_limits.default_max_input_tokens",
    )
    if not 1 <= default_max_input_tokens <= MAX_PROVIDER_INPUT_TOKENS:
        raise HubV2Error(
            "invalid_provider_manifest",
            "default_max_input_tokens is outside the supported range.",
            scope="provider",
        )
    raw_overrides = context_limits.get("model_overrides", [])
    if not isinstance(raw_overrides, list) or len(raw_overrides) > 64:
        raise HubV2Error(
            "invalid_provider_manifest",
            "model_overrides must be an array with at most 64 items.",
            scope="provider",
        )
    model_overrides: list[dict[str, Any]] = []
    seen_prefixes: set[str] = set()
    for index, raw_override in enumerate(raw_overrides):
        override = require_object(
            raw_override,
            field=f"context_limits.model_overrides[{index}]",
        )
        _reject_unknown_fields(
            override,
            allowed={"model_prefix", "max_input_tokens"},
            field=f"context_limits.model_overrides[{index}]",
            code="invalid_provider_manifest",
            scope="provider",
        )
        model_prefix = require_string(
            override.get("model_prefix"),
            field=f"context_limits.model_overrides[{index}].model_prefix",
            maximum=128,
        )
        max_input_tokens = require_non_negative_int(
            override.get("max_input_tokens"),
            field=f"context_limits.model_overrides[{index}].max_input_tokens",
        )
        if model_prefix in seen_prefixes or not 1 <= max_input_tokens <= MAX_PROVIDER_INPUT_TOKENS:
            raise HubV2Error(
                "invalid_provider_manifest",
                "model_overrides contain a duplicate prefix or invalid token limit.",
                scope="provider",
            )
        seen_prefixes.add(model_prefix)
        model_overrides.append(
            {
                "model_prefix": model_prefix,
                "max_input_tokens": max_input_tokens,
            }
        )
    return {
        "schema": PROVIDER_MANIFEST_SCHEMA,
        "provider_id": provider_id,
        "adapter_version": adapter_version,
        "protocol_version": protocol_version,
        "capabilities": sorted(set(capabilities)),
        "reasoning_effort": _string_list(
            value.get("reasoning_effort"),
            field="reasoning_effort",
            maximum_items=16,
        ),
        "auth_owner": require_string(
            value.get("auth_owner"),
            field="auth_owner",
            maximum=128,
        ),
        "auth_mode": require_string(
            value.get("auth_mode"),
            field="auth_mode",
            maximum=128,
        ),
        "allowed_domains": sorted(set(domains)),
        "supports_cancel": bool(value.get("supports_cancel", False)),
        "supports_streaming": bool(value.get("supports_streaming", False)),
        "supports_idempotency": bool(value.get("supports_idempotency", False)),
        "context_limits": {
            "default_max_input_tokens": default_max_input_tokens,
            "model_overrides": sorted(
                model_overrides,
                key=lambda item: (-len(item["model_prefix"]), item["model_prefix"]),
            ),
        },
        "settings_schema": require_object(
            value.get("settings_schema", {}),
            field="settings_schema",
        ),
    }


def ensure_public_model_id(model_id: Any) -> str:
    value = require_string(model_id, field="model_id", maximum=256)
    uppercase = value.upper()
    if "PLACEHOLDER" in uppercase or uppercase.startswith("MODEL_"):
        raise HubV2Error(
            "invalid_model_id",
            "Internal or placeholder model ids cannot be used for generation.",
            scope="provider",
        )
    return value


def safe_usage(value: Any) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int | float] = {}
    for raw_key, raw_value in list(value.items())[:32]:
        key = str(raw_key)[:64]
        if (
            _ID_RE.fullmatch(key)
            and isinstance(raw_value, (int, float))
            and not isinstance(raw_value, bool)
            and math.isfinite(float(raw_value))
            and raw_value >= 0
        ):
            result[key] = raw_value
    return result


MAX_STEP_TOKENS = 1_000_000_000
STEP_TOKEN_SOURCES = frozenset({"unset", "reported", "estimated", "mixed", "local"})
_USAGE_INPUT_KEYS = ("input_tokens", "prompt_tokens")
_USAGE_OUTPUT_KEYS = ("output_tokens", "completion_tokens")


def estimate_tokens_from_text(text: str) -> int:
    """Rough byte-based token estimate used when a provider reports no usage."""

    return (len(text.encode("utf-8")) + 3) // 4


def _usage_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < 0:
        return None
    return min(int(value), MAX_STEP_TOKENS)


def _first_usage_value(usage: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        parsed = _usage_int(usage.get(key))
        if parsed is not None:
            return parsed
    return None


def normalize_token_usage(
    usage: Any,
    *,
    estimated_input_tokens: int = 0,
    estimated_output_tokens: int = 0,
) -> dict[str, Any]:
    """Normalize provider-specific usage payloads into one accounting shape.

    Providers disagree on key names and some omit usage entirely, so the caller
    supplies estimates. ``source`` records whether the result was reported by the
    provider, estimated locally, or a mix, so that purely estimated numbers can be
    kept out of routing statistics.
    """

    estimate_input = max(0, min(int(estimated_input_tokens), MAX_STEP_TOKENS))
    estimate_output = max(0, min(int(estimated_output_tokens), MAX_STEP_TOKENS))
    mapping = usage if isinstance(usage, Mapping) else {}
    reported_input = _first_usage_value(mapping, _USAGE_INPUT_KEYS)
    reported_output = _first_usage_value(mapping, _USAGE_OUTPUT_KEYS)
    reported_total = _usage_int(mapping.get("total_tokens"))

    if reported_input is not None and reported_output is not None:
        input_tokens, output_tokens, source = reported_input, reported_output, "reported"
    elif reported_total is not None and reported_total > 0:
        # Only a total is available: attribute it without inventing extra tokens.
        input_tokens = min(estimate_input, reported_total)
        output_tokens = reported_total - input_tokens
        source = "mixed"
    elif reported_input is not None:
        input_tokens, output_tokens, source = reported_input, estimate_output, "mixed"
    elif reported_output is not None:
        input_tokens, output_tokens, source = estimate_input, reported_output, "mixed"
    else:
        input_tokens, output_tokens, source = estimate_input, estimate_output, "estimated"

    total = min(input_tokens + output_tokens, MAX_STEP_TOKENS)
    if source == "reported" and reported_total is not None:
        total = min(max(reported_total, 0), MAX_STEP_TOKENS)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total,
        "source": source,
    }


def bounded_unique(values: Iterable[str], *, maximum: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
        if len(result) >= maximum:
            break
    return result
