"""Typed, state-verified takeover capsules for fixed and adaptive runs."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import hmac
import json
import math
from pathlib import Path
from typing import Any, Dict, Tuple

from orchestrate_codex import runner, store

CAPSULE_SCHEMA = "agent_hub_takeover_v1"
MAX_CAPSULE_BYTES = 64 * 1024
MAX_CAPSULE_DEPTH = 5
MAX_CAPSULE_NODES = 256
MAX_CAPSULE_COLLECTION_ITEMS = 128
MAX_CAPSULE_STRING_CHARS = 8 * 1024
_CAPSULE_KEYS = {
    "schema",
    "run_id",
    "run_kind",
    "project_root",
    "workflow_id",
    "status",
    "expected_revision",
    "action_id",
    "plan_sha256",
    "handoff_source",
    "handoff_sha256",
    "next_action",
    "state_updated_at",
    "capsule_sha256",
}
_SHA256_PATTERN = "^[0-9a-f]{64}$"

CAPSULE_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema": {"type": "string", "const": CAPSULE_SCHEMA},
        "run_id": {"type": "string", "pattern": store.RUN_ID_PATTERN},
        "run_kind": {"type": "string", "enum": ["fixed", "adaptive"]},
        "project_root": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CAPSULE_STRING_CHARS,
        },
        "workflow_id": {"type": "string", "maxLength": 128},
        "status": {"type": "string", "maxLength": 64},
        "expected_revision": {"type": "integer", "minimum": 0},
        "action_id": {
            "oneOf": [
                {"type": "string", "pattern": _SHA256_PATTERN},
                {"type": "null"},
            ]
        },
        "plan_sha256": {
            "oneOf": [
                {"type": "string", "pattern": _SHA256_PATTERN},
                {"type": "null"},
            ]
        },
        "handoff_source": {
            "oneOf": [
                {
                    "type": "string",
                    "maxLength": MAX_CAPSULE_STRING_CHARS,
                },
                {"type": "null"},
            ]
        },
        "handoff_sha256": {
            "oneOf": [
                {"type": "string", "pattern": _SHA256_PATTERN},
                {"type": "null"},
            ]
        },
        "next_action": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "call_tool"},
                        "stage_id": {"type": ["string", "null"]},
                        "tool": {"type": ["string", "null"]},
                        "action_id": {
                            "type": "string",
                            "pattern": _SHA256_PATTERN,
                        },
                    },
                    "required": ["type", "stage_id", "tool", "action_id"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "continue"},
                        "tool": {
                            "type": "string",
                            "const": "agent_hub_continue_workflow",
                        },
                        "pending_steps": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 128},
                            "maxItems": 100,
                        },
                    },
                    "required": ["type", "tool", "pending_steps"],
                    "additionalProperties": False,
                },
            ]
        },
        "state_updated_at": {"type": "number", "minimum": 0},
        "capsule_sha256": {"type": "string", "pattern": _SHA256_PATTERN},
    },
    "required": sorted(_CAPSULE_KEYS),
    "additionalProperties": False,
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _project_root(state: Dict[str, Any]) -> str:
    value = state.get("project_root")
    if not value and isinstance(state.get("options"), dict):
        value = state["options"].get("project_root")
    root = Path(str(value or "")).expanduser()
    if not root.is_absolute():
        raise ValueError("persisted run does not have a canonical project_root")
    canonical = str(root.resolve())
    if str(root) != canonical:
        raise ValueError("persisted run project_root is not canonical")
    return canonical


def _handoff_identity(state: Dict[str, Any]) -> Tuple[str | None, str | None]:
    snapshot = (
        state.get("_handoff_snapshot")
        if isinstance(state.get("_handoff_snapshot"), dict)
        else {}
    )
    source = str(snapshot.get("source") or "") or None
    digest = str(snapshot.get("file_sha256") or "") or None
    return source, digest


def _adaptive_plan_sha(state: Dict[str, Any]) -> str:
    plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
    executable = {
        key: plan.get(key)
        for key in ("schema", "goal", "rationale", "steps")
    }
    return _digest(executable)


def _fixed_action(state: Dict[str, Any]) -> Dict[str, Any]:
    action = runner._next_action(deepcopy(state))
    if action.get("type") != "call_tool":
        raise ValueError("fixed run does not have a provider action to take over")
    return action


def _preflight_capsule(capsule: Any) -> None:
    if not isinstance(capsule, dict):
        raise ValueError("capsule must be an object")
    stack = [(capsule, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_CAPSULE_NODES:
            raise ValueError("takeover capsule has too many values")
        if depth > MAX_CAPSULE_DEPTH:
            raise ValueError("takeover capsule is nested too deeply")
        if isinstance(value, dict):
            if len(value) > MAX_CAPSULE_COLLECTION_ITEMS:
                raise ValueError("takeover capsule object has too many fields")
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("takeover capsule field names must be strings")
                if len(key) > 128:
                    raise ValueError("takeover capsule field name is too long")
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            if len(value) > MAX_CAPSULE_COLLECTION_ITEMS:
                raise ValueError("takeover capsule array has too many items")
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            if len(value) > MAX_CAPSULE_STRING_CHARS:
                raise ValueError("takeover capsule string is too long")
        elif isinstance(value, bool) or value is None:
            continue
        elif isinstance(value, int):
            if value.bit_length() > 256:
                raise ValueError("takeover capsule integer is too large")
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("takeover capsule number must be finite")
        else:
            raise ValueError("takeover capsule contains an unsupported value")


def _capsule_body(state: Dict[str, Any]) -> Dict[str, Any]:
    status = str(state.get("status") or "")
    if status in store.TERMINAL_RUN_STATUSES:
        raise ValueError("terminal run cannot be prepared for takeover")
    run_kind = str(
        state.get("run_kind") or ("fixed" if state.get("recipe_id") else "")
    )
    if run_kind not in {"fixed", "adaptive"}:
        raise ValueError("unsupported run kind for takeover")
    handoff_source, handoff_sha = _handoff_identity(state)
    action_id = None
    plan_sha = None
    if run_kind == "fixed":
        action = _fixed_action(state)
        action_id = action["action_id"]
        next_action = {
            "type": "call_tool",
            "stage_id": action.get("stage_id"),
            "tool": action.get("tool"),
            "action_id": action_id,
        }
    else:
        plan_sha = _adaptive_plan_sha(state)
        plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
        completed = set(
            (state.get("results") or {}).keys()
            if isinstance(state.get("results"), dict)
            else ()
        )
        pending = [
            str(step.get("id") or "")
            for step in plan.get("steps") or []
            if isinstance(step, dict) and str(step.get("id") or "") not in completed
        ]
        next_action = {
            "type": "continue",
            "tool": "agent_hub_continue_workflow",
            "pending_steps": pending,
        }
    try:
        state_updated_at = float(state.get("updated_at") or state.get("created_at") or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("persisted run timestamp is invalid") from exc
    if not math.isfinite(state_updated_at) or state_updated_at < 0:
        raise ValueError("persisted run timestamp is invalid")
    return {
        "schema": CAPSULE_SCHEMA,
        "run_id": state["run_id"],
        "run_kind": run_kind,
        "project_root": _project_root(state),
        "workflow_id": str(
            state.get("workflow_id") or state.get("recipe_id") or ""
        ),
        "status": status,
        "expected_revision": store._current_revision(state),
        "action_id": action_id,
        "plan_sha256": plan_sha,
        "handoff_source": handoff_source,
        "handoff_sha256": handoff_sha,
        "next_action": next_action,
        "state_updated_at": state_updated_at,
    }


def prepare(run_id: str, *, project_root: str) -> Dict[str, Any]:
    state = store.load_strict(store.validate_run_id(run_id))
    requested_root = str(Path(project_root).expanduser().resolve())
    if requested_root != _project_root(state):
        raise ValueError("run belongs to a different project")
    capsule = _capsule_body(state)
    capsule["capsule_sha256"] = _digest(capsule)
    return {"capsule": capsule}


def validate(
    capsule: Dict[str, Any],
    *,
    project_root: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    _preflight_capsule(capsule)
    if len(_canonical_json(capsule).encode("utf-8")) > MAX_CAPSULE_BYTES:
        raise ValueError("takeover capsule is too large")
    if set(capsule) != _CAPSULE_KEYS:
        raise ValueError("takeover capsule fields do not match the supported schema")
    if capsule.get("schema") != CAPSULE_SCHEMA:
        raise ValueError("unsupported takeover capsule schema")
    supplied_digest = str(capsule.get("capsule_sha256") or "")
    unsigned = {key: value for key, value in capsule.items() if key != "capsule_sha256"}
    if not hmac.compare_digest(supplied_digest, _digest(unsigned)):
        raise ValueError("takeover capsule digest mismatch")
    requested_root = Path(project_root).expanduser().resolve()
    if str(requested_root) != str(capsule.get("project_root") or ""):
        raise ValueError("takeover capsule belongs to a different project")

    state = store.load_strict(store.validate_run_id(capsule.get("run_id")))
    current_status = str(state.get("status") or "")
    if current_status in store.TERMINAL_RUN_STATUSES:
        if unsigned.get("status") != current_status:
            raise ValueError("stale takeover capsule: status changed")
        raise ValueError("terminal run cannot be resumed from takeover")
    expected = _capsule_body(state)
    for key, current in expected.items():
        if unsigned.get(key) != current:
            raise ValueError(f"stale takeover capsule: {key} changed")
    return state, capsule


def resume(
    capsule: Dict[str, Any],
    *,
    project_root: str,
    lease_seconds: float = 320.0,
    handoff_drift_policy: str | None = None,
) -> Dict[str, Any]:
    state, verified = validate(capsule, project_root=project_root)
    if verified["run_kind"] == "fixed":
        claimed = runner.claim_next_action(
            run_id=verified["run_id"],
            expected_revision=verified["expected_revision"],
            action_id=str(verified["action_id"] or ""),
            lease_seconds=lease_seconds,
            handoff_drift_policy=handoff_drift_policy,
        )
        return {
            "resume_mode": "claimed_fixed_action",
            "state_mutated": True,
            "capsule_sha256": verified["capsule_sha256"],
            **claimed.public(),
        }
    return {
        "schema": "adaptive_takeover_resume_v1",
        "resume_mode": "validated_adaptive_continue",
        "state_mutated": False,
        "capsule_sha256": verified["capsule_sha256"],
        "run_id": verified["run_id"],
        "base_revision": verified["expected_revision"],
        "action": {
            "tool": "agent_hub_continue_workflow",
            "arguments": {
                "run_id": verified["run_id"],
                "expected_revision": verified["expected_revision"],
            },
        },
    }
