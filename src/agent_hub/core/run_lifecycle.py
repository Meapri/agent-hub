"""Revision-fenced cancellation, archival, and explicit run garbage collection."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import time
from typing import Any, Dict

from orchestrate_codex import events, runner, store


ACTIVE_STATUSES = store.ACTIVE_RUN_STATUSES
ARCHIVABLE_STATUSES = store.ARCHIVABLE_RUN_STATUSES
TERMINAL_STATUSES = store.TERMINAL_RUN_STATUSES
CANCEL_REASONS = frozenset({"user_requested", "superseded", "budget", "other"})


def _canonical_project_root(project_root: str) -> str:
    requested = Path(str(project_root)).expanduser()
    if not requested.is_absolute():
        raise ValueError("project_root must be an absolute canonical path")
    canonical = str(requested.resolve())
    if str(requested) != canonical:
        raise ValueError("project_root must be an absolute canonical path")
    return canonical


def _state_project_root(state: Dict[str, Any]) -> str:
    raw = state.get("project_root")
    if not raw and isinstance(state.get("options"), dict):
        raw = state["options"].get("project_root")
    requested = Path(str(raw or "")).expanduser()
    if not requested.is_absolute():
        raise ValueError("persisted run does not have a canonical project_root")
    canonical = str(requested.resolve())
    if str(requested) != canonical:
        raise ValueError("persisted run project_root is not canonical")
    return canonical


def _assert_project(state: Dict[str, Any], project_root: str) -> None:
    if _state_project_root(state) != project_root:
        raise ValueError("run belongs to a different project")


def _validate_expected_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    return value


def _projection(
    state: Dict[str, Any],
    *,
    changed: bool,
    previous_status: str,
    reason_code: str | None = None,
) -> Dict[str, Any]:
    return {
        "schema": "agent_hub_run_lifecycle_v1",
        "run_id": store.validate_run_id(state.get("run_id")),
        "run_kind": str(
            state.get("run_kind") or ("fixed" if state.get("recipe_id") else "")
        ),
        "workflow_id": str(state.get("workflow_id") or state.get("recipe_id") or ""),
        "project_root": _state_project_root(state),
        "status": str(state.get("status") or ""),
        "previous_status": previous_status,
        "store_revision": store._current_revision(state),
        "changed": changed,
        "reason_code": reason_code,
        "updated_at": float(
            state.get("updated_at") or state.get("created_at") or 0.0
        ),
        "event_journal": events.event_summary(state),
    }


def _scoped_state(run_id: str, project_root: str) -> Dict[str, Any]:
    state = store.load_strict(store.validate_run_id(run_id))
    _assert_project(state, project_root)
    return state


def cancel_run(
    run_id: str,
    *,
    project_root: str,
    expected_revision: int,
    reason_code: str = "user_requested",
) -> Dict[str, Any]:
    root = _canonical_project_root(project_root)
    expected = _validate_expected_revision(expected_revision)
    reason = str(reason_code or "user_requested").strip().lower()
    if reason not in CANCEL_REASONS:
        raise ValueError(
            "reason_code must be one of: "
            + ", ".join(sorted(CANCEL_REASONS))
        )
    validated = store.validate_run_id(run_id)
    _scoped_state(validated, root)
    previous_status = ""

    def transition(current: Dict[str, Any], revision: int) -> Dict[str, Any] | None:
        nonlocal previous_status
        _assert_project(current, root)
        status = str(current.get("status") or "")
        previous_status = status
        if status == "cancelled":
            return None
        if status not in ACTIVE_STATUSES:
            raise ValueError(f"run in status {status or 'unknown'} cannot be cancelled")

        changed = deepcopy(current)
        changed_at = time.time()
        changed.update(
            {
                "status": "cancelled",
                "cancel_reason": reason,
                "cancelled_at": changed_at,
                "updated_at": changed_at,
            }
        )
        changed.pop("pause_reason", None)
        events.append_event(
            changed,
            "run_cancelled",
            at=changed_at,
            base_revision=revision,
            resulting_revision=revision + 1,
            run_kind=str(
                changed.get("run_kind")
                or ("fixed" if changed.get("recipe_id") else "")
            ),
            workflow_id=str(
                changed.get("workflow_id") or changed.get("recipe_id") or ""
            ),
            status="cancelled",
            previous_status=status,
            reason_code=reason,
            success=True,
            retryable=False,
        )
        return changed

    committed, changed = store.commit_lifecycle_transition(
        validated,
        expected_revision=expected,
        mutate=transition,
        invalidate_active_lease=True,
    )
    runner.forget_run(validated)
    return _projection(
        committed,
        changed=changed,
        previous_status=previous_status,
        reason_code=(
            reason
            if changed
            else str(committed.get("cancel_reason") or reason)
        )
    )


def archive_run(
    run_id: str,
    *,
    project_root: str,
    expected_revision: int,
) -> Dict[str, Any]:
    root = _canonical_project_root(project_root)
    expected = _validate_expected_revision(expected_revision)
    validated = store.validate_run_id(run_id)
    _scoped_state(validated, root)
    previous_status = ""

    def transition(current: Dict[str, Any], revision: int) -> Dict[str, Any] | None:
        nonlocal previous_status
        _assert_project(current, root)
        status = str(current.get("status") or "")
        previous_status = status
        if status == "archived":
            return None
        if status not in ARCHIVABLE_STATUSES:
            raise ValueError(f"run in status {status or 'unknown'} cannot be archived")

        changed = deepcopy(current)
        changed_at = time.time()
        changed.update(
            {
                "status": "archived",
                "archived_from": status,
                "archived_at": changed_at,
                "updated_at": changed_at,
            }
        )
        events.append_event(
            changed,
            "run_archived",
            at=changed_at,
            base_revision=revision,
            resulting_revision=revision + 1,
            run_kind=str(
                changed.get("run_kind")
                or ("fixed" if changed.get("recipe_id") else "")
            ),
            workflow_id=str(
                changed.get("workflow_id") or changed.get("recipe_id") or ""
            ),
            status="archived",
            previous_status=status,
            success=True,
            retryable=False,
        )
        return changed

    committed, changed = store.commit_lifecycle_transition(
        validated,
        expected_revision=expected,
        mutate=transition,
        invalidate_active_lease=False,
    )
    runner.forget_run(validated)
    return _projection(
        committed,
        changed=changed,
        previous_status=previous_status,
    )


def gc_run(
    run_id: str,
    *,
    project_root: str,
    apply: bool = False,
    expected_revision: int | None = None,
    expected_state_sha256: str | None = None,
) -> Dict[str, Any]:
    root = _canonical_project_root(project_root)
    validated = store.validate_run_id(run_id)
    if not apply:
        state = _scoped_state(validated, root)
        if str(state.get("status") or "") != "archived":
            raise ValueError("only archived runs can be garbage-collected")
        if isinstance(state.get("_lease"), dict):
            raise store.RunPersistenceError("archived run unexpectedly retains a lease")
        return {
            "schema": "agent_hub_run_gc_plan_v1",
            "run_id": validated,
            "project_root": root,
            "status": "archived",
            "store_revision": store._current_revision(state),
            "state_sha256": store.state_sha256(state),
            "apply_required": True,
            "deleted": False,
        }

    if expected_revision is None:
        raise ValueError("expected_revision is required when apply=true")
    if expected_state_sha256 is None:
        raise ValueError("expected_state_sha256 is required when apply=true")
    deleted = store.delete_archived_strict(
        validated,
        project_root=root,
        expected_revision=_validate_expected_revision(expected_revision),
        expected_state_sha256=expected_state_sha256,
    )
    runner.forget_run(validated)
    return {
        "schema": "agent_hub_run_gc_receipt_v1",
        **deleted,
        "deleted": True,
    }
