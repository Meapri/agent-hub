"""Application service behind both the v2 daemon protocol and tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from typing import Any, Callable, Mapping

from agent_hub import __version__
from agent_hub.core import handoff as handoff_state
from agent_hub.doctor import run_doctor

from .context import collect_scoped_fact_pack, index_project, search_fact_pack
from .contracts import (
    PLAN_SCHEMA,
    TASK_SCHEMA,
    canonical_json,
    require_non_negative_int,
    safe_usage,
    validate_plan,
    validate_task,
)
from .crypto import ArtifactCipher, MacOSKeychainKeyProvider
from .egress import prepare_egress, redact_secret_lines, verify_egress_approval
from .errors import HubV2Error, public_failure, safe_unexpected_error
from .policy import (
    apply_policy_update,
    load_policy,
    prepare_policy_update,
)
from .provider_client import ProviderWorkerClient
from .provider_manifests import builtin_provider_manifests, manifest_for
from .routing import route, routing_context
from .store import HubStore
from .tools import TOOL_NAMES, tool_definitions
from .verifier import verify_output

MAX_PLANNER_MANIFEST_CHARS = 32_000
RUN_LEASE_GRACE_SECONDS = 60.0
MAX_RUN_LEASE_SECONDS = 3600.0


def _structured_text(result: Mapping[str, Any]) -> str:
    text = result.get("text")
    if isinstance(text, str):
        return text
    data = result.get("data")
    if isinstance(data, Mapping) and isinstance(data.get("text"), str):
        return data["text"]
    return json.dumps(result, ensure_ascii=False)


def _planner_manifest_summary(manifest: Mapping[str, Any]) -> str:
    """Describe approved sources without duplicating their contents into planning."""

    entries = manifest.get("entries")
    safe_entries = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            safe_entries.append(
                {
                    "path": str(entry.get("path_alias") or ""),
                    "classification": str(entry.get("classification") or ""),
                    "chars": int(entry.get("chars") or 0),
                    "sha256": str(entry.get("sha256") or ""),
                }
            )
    summary: dict[str, Any] = {
        "schema": "agent_hub_planner_manifest_summary_v1",
        "manifest_sha256": str(manifest.get("manifest_sha256") or ""),
        "fact_pack_sha256": str(manifest.get("fact_pack_sha256") or ""),
        "entry_count": len(safe_entries),
        "total_chars": int(manifest.get("total_chars") or 0),
        "destinations": list(manifest.get("destinations") or []),
        "entries": [],
        "entries_truncated": False,
    }
    for entry in safe_entries:
        candidate = {**summary, "entries": [*summary["entries"], entry]}
        encoded = canonical_json(candidate)
        if len(encoded) > MAX_PLANNER_MANIFEST_CHARS:
            summary["entries_truncated"] = True
            break
        summary["entries"].append(entry)
    return canonical_json(summary)


class HubService:
    def __init__(
        self,
        store: HubStore,
        *,
        worker_factory: Callable[[str], ProviderWorkerClient] = ProviderWorkerClient,
        cipher: ArtifactCipher | None = None,
    ) -> None:
        self.store = store
        self._worker_factory = worker_factory
        self.cipher = cipher or ArtifactCipher(MacOSKeychainKeyProvider())
        self._active: dict[str, list[ProviderWorkerClient]] = {}
        self._active_lock = threading.Lock()
        self._status_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._status_cache_lock = threading.Lock()

    def dispatch(self, name: str, arguments: Any) -> dict[str, Any]:
        if name not in TOOL_NAMES:
            return public_failure(
                HubV2Error(
                    "unknown_tool",
                    "The Agent Hub v2 tool is not supported.",
                    scope="mcp",
                ),
                operation=name or "tools/call",
            )
        if not isinstance(arguments, Mapping):
            return public_failure(
                HubV2Error(
                    "invalid_request",
                    "Tool arguments must be an object.",
                    scope="mcp",
                ),
                operation=name,
            )
        handler = getattr(self, f"_tool_{name.removeprefix('agent_hub_')}")
        started = time.monotonic()
        try:
            data = handler(dict(arguments))
            response = {"success": True, "operation": name, "error": None, "data": data}
        except HubV2Error as exc:
            response = public_failure(exc, operation=name)
        except Exception:  # noqa: BLE001
            response = safe_unexpected_error(operation=name)
        try:
            self.store.record_operation_metric(
                operation=name,
                success=response.get("success") is True,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:  # noqa: BLE001
            pass
        return response

    def _worker(self, provider: str) -> ProviderWorkerClient:
        manifest_for(provider)
        return self._worker_factory(provider)

    @staticmethod
    def _safe_provider_error(
        error: HubV2Error,
        *,
        provider: str,
    ) -> HubV2Error:
        return HubV2Error(
            error.code,
            "The provider could not complete the requested operation.",
            scope="provider",
            retryable=error.retryable,
            safe_details={
                "provider": provider,
                "reason_code": error.code,
            },
        )

    @staticmethod
    def _enforce_task_policy(
        task: dict[str, Any],
        *,
        project_root: str,
        provider: str | None,
        model: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        snapshot = load_policy(project_root)
        policy = snapshot.policy
        if (
            task.get("inline_input")
            and policy["egress"].get("inline_prompt") == "denied"
            and task["capability"] != "inspect"
        ):
            raise HubV2Error(
                "egress_policy_denied",
                "Project policy denies inline prompt egress.",
                scope="policy",
            )
        constraints = dict(task["constraints"])
        requested = constraints["provider_allowlist"]
        allowed = list(policy["provider_allowlist"])
        if requested and not set(requested).issubset(allowed):
            raise HubV2Error(
                "provider_policy_denied",
                "The task requests a provider denied by project policy.",
                scope="policy",
            )
        if provider and provider not in allowed:
            raise HubV2Error(
                "provider_policy_denied",
                "The selected provider is denied by project policy.",
                scope="policy",
            )
        model_allowlist = set(policy["model_allowlist"])
        if model and model_allowlist and model not in model_allowlist:
            raise HubV2Error(
                "model_policy_denied",
                "The selected model is denied by project policy.",
                scope="policy",
            )
        constraints["provider_allowlist"] = requested or allowed
        for field in ("timeout_seconds", "max_tokens"):
            project_limit = policy["budgets"].get(field)
            requested_limit = constraints.get(field)
            if project_limit is not None:
                constraints[field] = (
                    min(requested_limit, project_limit)
                    if requested_limit is not None
                    else project_limit
                )
        normalized = validate_task({**task, "constraints": constraints})
        return normalized, policy

    @staticmethod
    def _provider_state(result: Mapping[str, Any], provider: str) -> dict[str, Any]:
        data = result.get("data")
        providers = data.get("providers") if isinstance(data, Mapping) else None
        state = providers.get(provider) if isinstance(providers, Mapping) else None
        return dict(state) if isinstance(state, Mapping) else {}

    @staticmethod
    def _auth_state(state: Mapping[str, Any]) -> str:
        if state.get("ready") or state.get("auth_ready"):
            return "callable"
        if state.get("logged_in") and state.get("refreshable"):
            return "refreshable"
        if state.get("relogin_required") or not state.get("logged_in", False):
            return "login_required"
        return "unavailable"

    def _readiness(
        self,
        providers: list[str],
        *,
        probe: bool = False,
    ) -> tuple[dict[str, bool], dict[str, Any]]:
        readiness: dict[str, bool] = {}
        states: dict[str, Any] = {}
        now = time.monotonic()
        pending: list[str] = []
        with self._status_cache_lock:
            for provider in providers:
                cached = self._status_cache.get(provider)
                if not probe and cached and now - cached[0] <= 5.0:
                    states[provider] = dict(cached[1])
                    readiness[provider] = bool(cached[1].get("ready"))
                else:
                    pending.append(provider)

        def fetch(provider: str) -> tuple[str, dict[str, Any]]:
            try:
                result = self._worker(provider).request(
                    "status",
                    {"probe": probe},
                    timeout=10.0,
                )
                state = self._provider_state(result, provider)
                return provider, state
            except HubV2Error as exc:
                return provider, {
                    "ready": False,
                    "error_code": exc.code,
                    "retryable": exc.retryable,
                }

        if pending:
            with ThreadPoolExecutor(max_workers=min(4, len(pending))) as executor:
                futures = [executor.submit(fetch, provider) for provider in pending]
                for future in as_completed(futures):
                    provider, state = future.result()
                    states[provider] = state
                    readiness[provider] = bool(state.get("ready"))
                    with self._status_cache_lock:
                        self._status_cache[provider] = (time.monotonic(), dict(state))
        return readiness, states

    def _tool_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        manifests = builtin_provider_manifests()
        providers = [item["provider_id"] for item in manifests]
        readiness, states = self._readiness(
            providers,
            probe=bool(arguments.get("probe", False)),
        )
        return {
            "schema": "agent_hub_status_v2",
            "version": __version__,
            "protocol_version": "2.0",
            "store": self.store.health(),
            "observability": self.store.operation_metrics(),
            "providers": {
                provider: {
                    "manifest": next(item for item in manifests if item["provider_id"] == provider),
                    "state": states[provider],
                    "ready": readiness[provider],
                }
                for provider in providers
            },
            "authentication_mutation": {
                "allowed_via_mcp": False,
                "next_action": {"type": "local_gui", "command": "agent-hub-connect"},
            },
        }

    def _tool_catalog(self, arguments: dict[str, Any]) -> dict[str, Any]:
        provider = str(arguments.get("provider") or "")
        providers = (
            [provider]
            if provider
            else [item["provider_id"] for item in builtin_provider_manifests()]
        )
        capability = str(arguments.get("capability") or "")
        catalog: dict[str, Any] = {}
        for provider_id in providers:
            manifest = manifest_for(provider_id)
            if capability and capability not in manifest["capabilities"]:
                continue
            try:
                result = self._worker(provider_id).request(
                    "catalog",
                    {"refresh": bool(arguments.get("refresh", False))},
                    timeout=30.0,
                )
                state = self._provider_state(
                    self._worker(provider_id).request(
                        "status",
                        {"probe": False},
                        timeout=10.0,
                    ),
                    provider_id,
                )
                requested_model = str(arguments.get("model") or "") or None
                verification = self.store.generation_verification(
                    provider=provider_id,
                    model=requested_model,
                )
                result_data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
                model_payload = (
                    (result_data.get("models") or {}).get(provider_id)
                    if isinstance(result_data.get("models"), Mapping)
                    else None
                )
                source = (
                    str(model_payload.get("source") or "")
                    if isinstance(model_payload, Mapping)
                    else ""
                )
                if not isinstance(model_payload, Mapping) or model_payload.get("success") is False:
                    catalog_state = "unavailable"
                elif source in {"static", "static_fallback", "curated", "built_in"}:
                    catalog_state = "static_fallback"
                elif source == "cached":
                    catalog_state = "cached"
                else:
                    catalog_state = "live"
                catalog[provider_id] = {
                    "manifest": manifest,
                    "auth_state": self._auth_state(state),
                    "catalog_state": catalog_state,
                    "generation_state": (
                        verification["generation_state"] if verification else "unknown"
                    ),
                    "generation_verification": verification,
                    "result": result,
                }
            except HubV2Error as exc:
                safe_error = self._safe_provider_error(exc, provider=provider_id)
                catalog[provider_id] = {
                    "manifest": manifest,
                    "auth_state": "unavailable",
                    "catalog_state": "unavailable",
                    "generation_state": "unknown",
                    "error": safe_error.public(),
                }
        return {"schema": "agent_hub_catalog_v2", "providers": catalog}

    def _tool_execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task = validate_task(arguments.get("task"))
        if task["retention"] != "ephemeral" or arguments.get("record") is True:
            raise HubV2Error(
                "durable_run_required",
                "Recorded work must use agent_hub_start.",
                scope="run",
                next_action={"type": "call_tool", "tool": "agent_hub_start"},
            )
        project_root = arguments.get("project_root")
        if not isinstance(project_root, str) or not project_root:
            raise HubV2Error(
                "invalid_project_root",
                "project_root is required so project policy can be enforced.",
                scope="project",
            )
        task, _ = self._enforce_task_policy(
            task,
            project_root=project_root,
            provider=str(arguments.get("provider") or "") or None,
            model=str(arguments.get("model") or "") or None,
        )
        if task["capability"] == "inspect":
            inspection = task.get("output_contract") or {}
            source_paths = list(inspection.get("source_paths") or [])
            if source_paths:
                indexed = None
                facts = collect_scoped_fact_pack(
                    project_root=project_root,
                    source_paths=source_paths,
                )
            else:
                indexed = index_project(self.store, project_root=project_root)
                facts = search_fact_pack(
                    self.store,
                    project_root=project_root,
                    query=task["inline_input"] or task["intent"],
                )
            return {
                "schema": "agent_hub_execution_v2",
                "provider": "local",
                "routing_decision": None,
                "result": {"index": indexed, "fact_pack": facts},
            }
        if task.get("input_artifacts"):
            raise HubV2Error(
                "egress_approval_required",
                "Stored artifact input must use agent_hub_plan prepare/apply before external execution.",
                scope="egress",
                next_action={"type": "call_tool", "tool": "agent_hub_plan"},
            )
        allowed = task["constraints"]["provider_allowlist"] or [
            item["provider_id"] for item in builtin_provider_manifests()
        ]
        explicit = str(arguments.get("provider") or "")
        planner_provider = explicit or allowed[0]
        readiness, _ = self._readiness(allowed)
        decision = route(
            store=self.store,
            task=task,
            planner_provider=planner_provider,
            routing_mode="pinned" if explicit else "shadow",
            provider_allowlist=allowed,
            readiness=readiness,
            circuit_open={
                provider: self.store.provider_health(provider)["circuit_open"]
                for provider in allowed
            },
            models={planner_provider: str(arguments.get("model") or "")},
        )
        provider = decision["selected_provider"]
        try:
            result = self._worker(provider).request(
                "invoke",
                {"task": task, "model": arguments.get("model")},
                timeout=float(task["constraints"].get("timeout_seconds") or 1790),
            )
        except HubV2Error as exc:
            self.store.record_provider_outcome(
                provider=provider,
                success=False,
                error_code=exc.code,
            )
            if arguments.get("model"):
                self.store.record_generation_verification(
                    provider=provider,
                    model=str(arguments["model"]),
                    generation_state="failed",
                    reason_code=exc.code,
                )
            raise self._safe_provider_error(exc, provider=provider) from None
        self.store.record_provider_outcome(provider=provider, success=True)
        resolved_model = str(result.get("model") or arguments.get("model") or "")
        if resolved_model:
            result = {**result, "model": resolved_model}
            self.store.record_generation_verification(
                provider=provider,
                model=resolved_model,
                generation_state="verified",
                reason_code="generation_succeeded",
            )
        return {
            "schema": "agent_hub_execution_v2",
            "provider": provider,
            "routing_decision": decision,
            "result": result,
        }

    def _tool_plan(self, arguments: dict[str, Any]) -> dict[str, Any]:
        mode = str(arguments.get("mode") or "")
        task = validate_task(arguments.get("task"))
        project_root = str(arguments.get("project_root") or "")
        policy = load_policy(project_root)
        provider = str(arguments.get("provider") or "claude")
        model = arguments.get("model")
        task, _ = self._enforce_task_policy(
            task,
            project_root=project_root,
            provider=provider,
            model=str(model) if model else None,
        )
        destination_providers = list(
            dict.fromkeys(
                [
                    provider,
                    *task["constraints"]["provider_allowlist"],
                ]
            )
        )
        if mode == "prepare":
            source_paths = list(arguments.get("source_paths") or [])
            if source_paths and policy.policy["egress"].get("repository_content") == "denied":
                raise HubV2Error(
                    "egress_policy_denied",
                    "Project policy denies repository content egress.",
                    scope="policy",
                )
            artifact_ids = list(task.get("input_artifacts") or [])
            if artifact_ids and policy.policy["egress"].get("artifact_content") == "denied":
                raise HubV2Error(
                    "egress_policy_denied",
                    "Project policy denies stored artifact egress.",
                    scope="policy",
                )
            artifact_sources = []
            for artifact_id in artifact_ids:
                artifact = self.store.get_artifact(artifact_id, include_content=False)
                artifact_sources.append(
                    {
                        "artifact_id": artifact_id,
                        "content": self._artifact_text(artifact_id),
                        "content_sha256": artifact["content_sha256"],
                        "sensitivity": artifact["sensitivity"],
                    }
                )
            proposal = prepare_egress(
                project_root=project_root,
                provider=provider,
                model=str(model) if model else None,
                destination_providers=destination_providers,
                source_paths=source_paths,
                policy_revision=policy.policy["revision"],
                estimated_max_tokens=int(
                    task["constraints"].get("max_tokens") or policy.policy["budgets"]["max_tokens"]
                ),
                artifact_sources=artifact_sources,
            )
            proposal["task"] = task
            proposal["provider"] = provider
            proposal["model"] = model
            proposal["proposal_sha256"] = sha256(
                canonical_json(
                    {
                        "task": task,
                        "provider": provider,
                        "model": model,
                        "manifest_sha256": proposal["manifest"]["manifest_sha256"],
                    }
                ).encode("utf-8")
            ).hexdigest()
            return proposal
        if mode != "apply":
            raise HubV2Error(
                "invalid_request",
                "plan mode must be prepare or apply.",
                scope="planner",
            )
        proposal = arguments.get("proposal")
        if not isinstance(proposal, Mapping):
            raise HubV2Error(
                "invalid_egress_proposal",
                "An egress proposal is required.",
                scope="planner",
            )
        proposal_sha = str(arguments.get("proposal_sha256") or "")
        if proposal.get("proposal_sha256") != proposal_sha:
            raise HubV2Error(
                "proposal_digest_conflict",
                "The planner proposal digest does not match.",
                scope="planner",
            )
        expected_proposal_sha = sha256(
            canonical_json(
                {
                    "task": task,
                    "provider": provider,
                    "model": model,
                    "manifest_sha256": proposal.get("manifest", {}).get("manifest_sha256"),
                }
            ).encode("utf-8")
        ).hexdigest()
        if expected_proposal_sha != proposal_sha:
            raise HubV2Error(
                "proposal_digest_conflict",
                "The planner inputs changed after preparation.",
                scope="planner",
            )
        expected_policy_revision = require_non_negative_int(
            arguments.get("expected_policy_revision"),
            field="expected_policy_revision",
        )
        verified = verify_egress_approval(
            proposal,
            approved_manifest_sha256=str(proposal.get("manifest", {}).get("manifest_sha256") or ""),
            expected_policy_revision=expected_policy_revision,
        )
        self.store.record_egress_approval(
            project_root=project_root,
            manifest=verified["manifest"],
        )
        approved_paths = {
            str(entry.get("path_alias") or "")
            for entry in verified["manifest"].get("entries") or []
            if isinstance(entry, Mapping) and str(entry.get("path_alias") or "")
        }
        approved_destinations = {
            str(item) for item in verified["manifest"].get("destinations") or []
        }
        planner_prompt = (
            task["intent"] + "\n\nApproved project manifest (source contents are intentionally "
            "excluded from this planning call):\n"
            + _planner_manifest_summary(verified["manifest"])
            + "\n\nAgent Hub runtime planner constraints: use only chat, "
            "inspect_codebase, search, write, and review_text steps. Do not use "
            "compare, verify, review_diff, release_snapshot, or release_draft. "
            "Use review_text or a final write step to incorporate review findings."
        )
        raw = self._worker(provider).request(
            "plan",
            {
                "task": task,
                "planner_prompt": planner_prompt,
                "model": model,
            },
            timeout=1790.0,
        )
        data = raw.get("data") if isinstance(raw.get("data"), Mapping) else {}
        raw_plan = data.get("plan") or raw.get("plan")
        if not isinstance(raw_plan, Mapping):
            raise HubV2Error(
                "planner_protocol_error",
                "The planner did not return a plan.",
                scope="planner",
                retryable=True,
            )
        raw_steps = raw_plan.get("steps")
        if not isinstance(raw_steps, list):
            raise HubV2Error(
                "planner_protocol_error",
                "The planner steps are invalid.",
                scope="planner",
            )
        steps = []
        for index, step in enumerate(raw_steps):
            if not isinstance(step, Mapping):
                continue
            capability = str(step.get("capability") or "chat")
            if capability == "review_text":
                capability = "review"
            elif capability == "inspect_codebase":
                capability = "inspect"
            output_contract = dict(step.get("output_contract") or {})
            planned_provider = str(step.get("provider") or provider)
            planned_fallbacks = [
                str(item) for item in step.get("fallback_providers") or step.get("fallbacks") or []
            ]
            if not {planned_provider, *planned_fallbacks}.issubset(approved_destinations):
                raise HubV2Error(
                    "planner_egress_violation",
                    "The planner selected a provider outside the approved destinations.",
                    scope="planner",
                )
            if capability == "inspect":
                requested_sources = list(output_contract.get("source_paths") or [])
                if requested_sources and not set(requested_sources).issubset(approved_paths):
                    raise HubV2Error(
                        "planner_scope_violation",
                        "The planner requested inspection sources outside the approved manifest.",
                        scope="planner",
                    )
                output_contract.update(
                    {
                        "type": "fact_pack_v2",
                        "source_paths": requested_sources or sorted(approved_paths),
                        "require_complete": bool(requested_sources or approved_paths),
                    }
                )
            steps.append(
                {
                    "id": str(step.get("id") or f"step_{index + 1}"),
                    "capability": capability,
                    "depends_on": list(step.get("depends_on") or []),
                    "instruction": str(
                        step.get("instruction") or step.get("prompt") or task["intent"]
                    ),
                    "routing_requirements": {
                        "planner_provider": planned_provider,
                        "fallbacks": planned_fallbacks,
                    },
                    "output_contract": output_contract,
                    "verifier": dict(step.get("verifier") or {}),
                }
            )
        plan = validate_plan(
            {
                "schema": PLAN_SCHEMA,
                "task": task,
                "steps": steps,
                "routing_mode": policy.policy["routing_mode"],
                "policy_revision": policy.policy["revision"],
                "egress_manifest_sha256": verified["manifest"]["manifest_sha256"],
            }
        )
        return {
            "schema": "agent_hub_plan_result_v2",
            "plan": plan,
            "planner_provider": provider,
            "planner_model": model,
            "egress_manifest": verified["manifest"],
        }

    def _seal_plan(
        self,
        plan: Mapping[str, Any],
        *,
        request_plan_sha256: str,
    ) -> dict[str, Any]:
        sealed = deepcopy(dict(plan))
        sealed["inline_consent_artifacts"] = []
        sealed["request_plan_sha256"] = request_plan_sha256
        task = deepcopy(dict(sealed["task"]))
        inline = str(task.get("inline_input") or "")
        if inline:
            encrypted = self.cipher.encrypt(
                inline.encode("utf-8"),
                aad=b"agent-hub-v2-task-input",
            )
            artifact = self.store.put_artifact(
                content=encrypted["payload"],
                media_type="text/plain; charset=utf-8",
                sensitivity="project",
                encrypted=True,
                source_refs=[],
                retention=task["retention"],
                delete_after=(time.time() + 86400.0 if task["retention"] == "ephemeral" else None),
                content_sha256=str(encrypted["content_sha256"]),
            )
            task["inline_input"] = ""
            task["input_artifacts"] = [
                *task.get("input_artifacts", []),
                artifact["artifact_id"],
            ]
            sealed["inline_consent_artifacts"] = [artifact["artifact_id"]]
        sealed["task"] = task
        sealed.pop("plan_sha256", None)
        return validate_plan(sealed)

    def _tool_start(self, arguments: dict[str, Any]) -> dict[str, Any]:
        idempotency_key = str(arguments.get("idempotency_key") or "")
        plan = validate_plan(arguments.get("plan"))
        project_root = str(arguments.get("project_root") or "")
        request_plan_sha256 = str(plan["plan_sha256"])
        existing = self.store.get_run_by_idempotency_key(
            idempotency_key,
            expected_project_root=project_root,
            expected_request_plan_sha256=request_plan_sha256,
        )
        if existing is not None:
            return existing
        policy = load_policy(project_root).policy
        if plan["policy_revision"] != policy["revision"]:
            raise HubV2Error(
                "policy_revision_conflict",
                "The plan was prepared under a different project policy revision.",
                scope="policy",
                retryable=True,
            )
        if len(plan["steps"]) > int(policy["budgets"]["max_leaf_calls"]):
            raise HubV2Error(
                "leaf_call_budget_exceeded",
                "The plan exceeds the project leaf-call budget.",
                scope="policy",
            )
        self._validate_inspection_approval(plan, project_root=project_root)
        model_allowlist = set(policy["model_allowlist"])
        for step in plan["steps"]:
            requested_model = str((step.get("routing_requirements") or {}).get("model") or "")
            if requested_model and model_allowlist and requested_model not in model_allowlist:
                raise HubV2Error(
                    "model_policy_denied",
                    "A plan step requests a model denied by project policy.",
                    scope="policy",
                )
        enforced_task, _ = self._enforce_task_policy(
            dict(plan["task"]),
            project_root=project_root,
            provider=None,
            model=None,
        )
        enforced_task = validate_task({**enforced_task, "retention": policy["artifact_retention"]})
        effective_plan = {**plan, "task": enforced_task}
        effective_plan.pop("plan_sha256", None)
        plan = validate_plan(effective_plan)
        sealed = self._seal_plan(
            plan,
            request_plan_sha256=request_plan_sha256,
        )
        return self.store.create_run(
            plan=sealed,
            project_root=project_root,
            idempotency_key=idempotency_key,
        )

    def _validate_inspection_approval(
        self,
        plan: Mapping[str, Any],
        *,
        project_root: str,
    ) -> None:
        steps = list(plan.get("steps") or [])
        dependents: dict[str, list[Mapping[str, Any]]] = {str(step["id"]): [] for step in steps}
        for step in steps:
            for dependency in step.get("depends_on") or []:
                dependents.setdefault(str(dependency), []).append(step)

        def reaches_external(step_id: str) -> bool:
            pending = list(dependents.get(step_id, []))
            seen: set[str] = set()
            while pending:
                candidate = pending.pop()
                candidate_id = str(candidate["id"])
                if candidate_id in seen:
                    continue
                seen.add(candidate_id)
                if candidate["capability"] != "inspect":
                    return True
                pending.extend(dependents.get(candidate_id, []))
            return False

        scoped_steps = []
        for step in steps:
            if step["capability"] != "inspect":
                continue
            contract = step.get("output_contract") or {}
            paths = list(contract.get("source_paths") or [])
            if reaches_external(str(step["id"])) and not paths:
                raise HubV2Error(
                    "egress_approval_required",
                    "An inspection feeding an external step requires approved source paths.",
                    scope="egress",
                )
            if paths:
                scoped_steps.append((str(step["id"]), paths))
        external_steps = [step for step in steps if step["capability"] != "inspect"]
        input_artifacts = set(plan.get("task", {}).get("input_artifacts") or [])
        inline_consent = set(plan.get("inline_consent_artifacts") or [])
        stored_input_artifacts = input_artifacts - inline_consent
        if not scoped_steps and not (external_steps and stored_input_artifacts):
            return
        manifest_sha = str(plan.get("egress_manifest_sha256") or "")
        approval = self.store.get_egress_approval(manifest_sha)
        if approval is None:
            raise HubV2Error(
                "egress_approval_required",
                "The plan does not reference a recorded egress approval.",
                scope="egress",
            )
        root = str(Path(project_root).expanduser().resolve(strict=True))
        if approval["project_root"] != root or approval["policy_revision"] != int(
            plan["policy_revision"]
        ):
            raise HubV2Error(
                "egress_approval_conflict",
                "The recorded egress approval belongs to a different project or policy.",
                scope="egress",
            )
        approved_paths = {
            str(entry["path_alias"])
            for entry in approval["entries"]
            if entry.get("kind") == "repository"
        }
        approved_artifacts = {
            str(entry["artifact_id"])
            for entry in approval["entries"]
            if entry.get("kind") == "artifact"
        }
        approved_destinations = set(approval["destinations"])
        for step in steps:
            if step["capability"] == "inspect":
                continue
            requirements = step.get("routing_requirements") or {}
            destinations = {
                str(requirements.get("planner_provider") or ""),
                *(str(item) for item in requirements.get("fallbacks") or []),
            }
            destinations.discard("")
            if not destinations.issubset(approved_destinations):
                raise HubV2Error(
                    "egress_approval_conflict",
                    "A provider step exceeds the approved egress destinations.",
                    scope="egress",
                    safe_details={"step_id": str(step["id"])},
                )
        for step_id, paths in scoped_steps:
            if not set(paths).issubset(approved_paths):
                raise HubV2Error(
                    "egress_approval_conflict",
                    "An inspection step exceeds the approved source scope.",
                    scope="egress",
                    safe_details={"step_id": step_id},
                )
        if external_steps and not stored_input_artifacts.issubset(approved_artifacts):
            raise HubV2Error(
                "egress_approval_conflict",
                "A stored input artifact is outside the approved egress manifest.",
                scope="egress",
            )

    def _artifact_text(self, artifact_id: str) -> str:
        artifact = self.store.get_artifact(artifact_id, include_content=True)
        content = artifact.get("content")
        if not isinstance(content, bytes):
            raise HubV2Error(
                "artifact_content_unavailable",
                "The artifact does not contain durable content.",
                scope="artifact",
            )
        plaintext = self.cipher.decrypt(
            content,
            aad=(
                b"agent-hub-v2-task-input"
                if artifact["producer_step_id"] is None
                else f"agent-hub-v2-step:{artifact['producer_step_id']}".encode("utf-8")
            ),
            expected_sha256=artifact["content_sha256"],
        )
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HubV2Error(
                "artifact_not_text",
                "The artifact is not UTF-8 text.",
                scope="artifact",
            ) from exc

    def _ready_steps(
        self,
        plan: Mapping[str, Any],
        run: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        statuses = {step["step_id"]: step["status"] for step in run["steps"]}
        return [
            dict(step)
            for step in plan["steps"]
            if statuses.get(step["id"]) == "queued"
            and all(statuses.get(dep) == "completed" for dep in step["depends_on"])
        ]

    def _execute_ready_step(
        self,
        *,
        run_id: str,
        plan: Mapping[str, Any],
        run: Mapping[str, Any],
        step: Mapping[str, Any],
    ) -> dict[str, Any]:
        started = time.monotonic()
        source_refs = list(
            dict.fromkeys(
                [
                    *plan["task"].get("input_artifacts", []),
                    *[
                        artifact_id
                        for state in run["steps"]
                        if state["step_id"] in step["depends_on"]
                        for artifact_id in state["output_artifact_ids"]
                    ],
                ]
            )
        )
        if step["capability"] == "inspect":
            inspection = step.get("output_contract") or {}
            source_paths = list(inspection.get("source_paths") or [])
            if source_paths:
                approval = self.store.get_egress_approval(
                    str(plan.get("egress_manifest_sha256") or "")
                )
                expected_sources = {
                    str(entry["path_alias"]): str(entry["sha256"])
                    for entry in (approval or {}).get("entries", [])
                }
                facts = collect_scoped_fact_pack(
                    project_root=run["project_root"],
                    source_paths=source_paths,
                    expected_sources=expected_sources,
                )
            else:
                index_project(self.store, project_root=run["project_root"])
                facts = search_fact_pack(
                    self.store,
                    project_root=run["project_root"],
                    query=step["instruction"],
                )
            if inspection.get("require_complete") is True and not facts.get("coverage", {}).get(
                "complete"
            ):
                raise HubV2Error(
                    "inspection_incomplete",
                    "The inspection could not collect every approved source completely.",
                    scope="context",
                    retryable=True,
                )
            text = json.dumps(facts, ensure_ascii=False)
            provider = "local"
            model = None
            media_type = "application/json"
            checkpoint: dict[str, Any] = {"retrieval": str(facts.get("retrieval") or "unknown")}
        else:
            input_artifacts = set(plan["task"].get("input_artifacts") or [])
            inline_consent = set(plan.get("inline_consent_artifacts") or [])
            approval = self.store.get_egress_approval(str(plan.get("egress_manifest_sha256") or ""))
            artifact_entries = {
                str(entry.get("artifact_id") or ""): entry
                for entry in (approval or {}).get("entries", [])
                if entry.get("kind") == "artifact"
            }
            dependency_parts = []
            for artifact_id in source_refs:
                artifact_text = self._artifact_text(artifact_id)
                if artifact_id in input_artifacts and artifact_id not in inline_consent:
                    entry = artifact_entries.get(artifact_id)
                    artifact = self.store.get_artifact(artifact_id, include_content=False)
                    if entry is None or entry.get("source_sha256") != artifact["content_sha256"]:
                        raise HubV2Error(
                            "egress_approval_conflict",
                            "A stored input artifact no longer matches its egress approval.",
                            scope="egress",
                        )
                    artifact_text, _ = redact_secret_lines(artifact_text)
                    if sha256(artifact_text.encode("utf-8")).hexdigest() != entry.get("sha256"):
                        raise HubV2Error(
                            "egress_approval_conflict",
                            "A stored input artifact changed after egress approval.",
                            scope="egress",
                        )
                dependency_parts.append(artifact_text)
            dependency_text = "\n\n".join(dependency_parts)
            task = validate_task(
                {
                    "schema": TASK_SCHEMA,
                    "intent": step["instruction"],
                    "capability": step["capability"],
                    "inline_input": dependency_text,
                    "constraints": plan["task"]["constraints"],
                    "retention": "ephemeral",
                }
            )
            allowed = task["constraints"]["provider_allowlist"] or [
                item["provider_id"] for item in builtin_provider_manifests()
            ]
            routing_requirements = step.get("routing_requirements") or {}
            requested_model = str(routing_requirements.get("model") or "") or None
            planner_provider = str(routing_requirements.get("planner_provider") or allowed[0])
            declared_fallbacks = [
                str(item)
                for item in routing_requirements.get("fallbacks") or []
                if str(item) in allowed and str(item) != planner_provider
            ]
            declared_chain = list(
                dict.fromkeys(
                    [
                        planner_provider,
                        *declared_fallbacks,
                    ]
                )
            )
            routing_allowlist = (
                [item for item in declared_chain if item in allowed]
                if routing_requirements.get("fallbacks") is not None
                else allowed
            )
            if planner_provider not in routing_allowlist:
                raise HubV2Error(
                    "provider_policy_denied",
                    "The planned primary provider is outside the task provider allowlist.",
                    scope="routing",
                )
            readiness, _ = self._readiness(routing_allowlist)
            decision = route(
                store=self.store,
                task=task,
                planner_provider=planner_provider,
                routing_mode=plan["routing_mode"],
                provider_allowlist=routing_allowlist,
                readiness=readiness,
                circuit_open={
                    provider_id: self.store.provider_health(provider_id)["circuit_open"]
                    for provider_id in routing_allowlist
                },
                run_id=run_id,
                step_id=step["id"],
                policy_revision=plan["policy_revision"],
                models={planner_provider: requested_model or ""},
            )
            eligible = {
                str(item["provider"]) for item in decision["candidates"] if item["eligible"]
            }
            if routing_requirements.get("fallbacks") is not None:
                provider_order = [
                    item
                    for item in dict.fromkeys([decision["selected_provider"], *declared_chain])
                    if item in eligible
                ]
            else:
                provider_order = [decision["selected_provider"]] + [
                    item["provider"]
                    for item in sorted(
                        (
                            item
                            for item in decision["candidates"]
                            if item["eligible"]
                            and item["provider"] != decision["selected_provider"]
                        ),
                        key=lambda item: (-item["score"], item["provider"]),
                    )
                ]
            result = None
            provider = provider_order[0]
            last_error: HubV2Error | None = None
            for candidate in provider_order:
                provider = candidate
                worker = self._worker(provider)
                attempt_started = time.monotonic()
                correlation_id = f"{run_id}.{step['id']}.{provider}"
                self.store.record_runtime_event(
                    run_id,
                    event_type="provider_attempt_started",
                    details={
                        "step_id": str(step["id"]),
                        "provider": provider,
                        "capability": str(step["capability"]),
                        "correlation_id": correlation_id,
                    },
                )
                with self._active_lock:
                    self._active.setdefault(run_id, []).append(worker)
                try:
                    result = worker.request(
                        "invoke",
                        {"task": task, "model": requested_model},
                        timeout=float(task["constraints"].get("timeout_seconds") or 1790),
                        request_id=correlation_id,
                    )
                except HubV2Error as exc:
                    exc.safe_details = {
                        **dict(exc.safe_details or {}),
                        "provider": provider,
                    }
                    self.store.record_runtime_event(
                        run_id,
                        event_type="provider_attempt_failed",
                        details={
                            "step_id": str(step["id"]),
                            "provider": provider,
                            "error_code": exc.code,
                            "retryable": exc.retryable,
                            "correlation_id": correlation_id,
                            "elapsed_ms": int((time.monotonic() - attempt_started) * 1000),
                        },
                    )
                    self.store.record_provider_outcome(
                        provider=provider,
                        success=False,
                        error_code=exc.code,
                    )
                    last_error = exc
                    if exc.code in {
                        "provider_timeout",
                        "provider_worker_failed",
                        "provider_protocol_error",
                    }:
                        raise
                    if not exc.retryable:
                        raise
                    continue
                self.store.record_runtime_event(
                    run_id,
                    event_type="provider_attempt_completed",
                    details={
                        "step_id": str(step["id"]),
                        "provider": provider,
                        "correlation_id": correlation_id,
                        "elapsed_ms": int((time.monotonic() - attempt_started) * 1000),
                    },
                )
                self.store.record_provider_outcome(provider=provider, success=True)
                break
            if result is None:
                raise HubV2Error(
                    "fallback_exhausted",
                    "All eligible provider fallbacks failed.",
                    scope="routing",
                    retryable=True,
                    safe_details={"last_error_code": last_error.code if last_error else "unknown"},
                )
            text = _structured_text(result)
            model = result.get("model") if isinstance(result.get("model"), str) else requested_model
            media_type = "text/plain; charset=utf-8"
            checkpoint = {"routing_decision_id": decision["decision_id"]}
        aad = f"agent-hub-v2-step:{step['id']}".encode("utf-8")
        try:
            verification = verify_output(text, step.get("verifier"))
        except HubV2Error:
            if provider != "local":
                self.store.record_routing_sample(
                    context=routing_context(task, model=model),
                    provider=provider,
                    model=model,
                    capability=step["capability"],
                    success=False,
                    quality=0.0,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    total_tokens=None,
                    signal_weight=3.0,
                )
            raise
        encrypted = self.cipher.encrypt(text.encode("utf-8"), aad=aad)
        artifact = self.store.put_artifact(
            content=encrypted["payload"],
            media_type=media_type,
            sensitivity="project",
            encrypted=True,
            run_id=run_id,
            producer_step_id=str(step["id"]),
            source_refs=source_refs,
            verification=verification,
            retention=plan["task"]["retention"],
            delete_after=(
                time.time() + 86400.0 if plan["task"]["retention"] == "ephemeral" else None
            ),
            content_sha256=str(encrypted["content_sha256"]),
        )
        checkpoint.update(
            {
                "result_sha256": artifact["content_sha256"],
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        )
        if provider != "local" and isinstance(result, Mapping):
            usage = safe_usage(result.get("usage"))
            if usage:
                checkpoint["usage"] = usage
        if provider != "local":
            usage = result.get("usage") if isinstance(result, Mapping) else None
            total_tokens = (
                usage.get("total_tokens")
                if isinstance(usage, Mapping) and isinstance(usage.get("total_tokens"), int)
                else None
            )
            self.store.record_routing_sample(
                context=routing_context(task, model=model),
                provider=provider,
                model=model,
                capability=step["capability"],
                success=True,
                quality=(
                    1.0
                    if (step.get("verifier") or {}).get("type") in {"json", "contains", "sha256"}
                    else None
                ),
                latency_ms=checkpoint["elapsed_ms"],
                total_tokens=total_tokens,
                signal_weight=3.0,
            )
        return {
            "step_id": step["id"],
            "provider": provider,
            "model": model,
            "artifact_id": artifact["artifact_id"],
            "input_artifact_ids": source_refs,
            "checkpoint": checkpoint,
        }

    @staticmethod
    def _critical_path_elapsed_ms(
        plan: Mapping[str, Any],
        run: Mapping[str, Any],
    ) -> int:
        durations = {
            str(step["step_id"]): int(step["checkpoint"].get("elapsed_ms") or 0)
            for step in run["steps"]
        }
        dependencies = {
            str(step["id"]): [str(item) for item in step["depends_on"]] for step in plan["steps"]
        }
        totals: dict[str, int] = {}

        def cumulative(step_id: str) -> int:
            if step_id in totals:
                return totals[step_id]
            upstream = max(
                (cumulative(dependency) for dependency in dependencies.get(step_id, [])),
                default=0,
            )
            totals[step_id] = upstream + durations.get(step_id, 0)
            return totals[step_id]

        return max((cumulative(step_id) for step_id in dependencies), default=0)

    def _run_wave(
        self,
        *,
        run_id: str,
        claim_token: str,
        claim_revision: int,
        max_waves: int,
        lease_seconds: float,
    ) -> None:
        current_revision = claim_revision
        try:
            plan = self.store.get_plan(run_id)
            for _ in range(max_waves):
                self.store.renew_claim(
                    run_id,
                    claim_token=claim_token,
                    expected_revision=current_revision,
                    lease_seconds=lease_seconds,
                )
                run = self.store.get_run(run_id)
                ready = self._ready_steps(plan, run)
                if not ready:
                    break
                for step in ready:
                    external = step["capability"] != "inspect"
                    updated = self.store.update_step(
                        run_id,
                        step_id=step["id"],
                        expected_run_revision=current_revision,
                        status="running",
                        checkpoint={
                            "phase": (
                                "provider_request_pending" if external else "local_step_pending"
                            ),
                            "retry_safe": not external,
                            "request_sha256": sha256(
                                canonical_json(
                                    {
                                        "plan_sha256": plan["plan_sha256"],
                                        "step_id": step["id"],
                                        "capability": step["capability"],
                                    }
                                ).encode("utf-8")
                            ).hexdigest(),
                        },
                    )
                    current_revision = updated["revision"]
                execution_run = self.store.get_run(run_id)
                errors: dict[str, HubV2Error] = {}
                with ThreadPoolExecutor(max_workers=min(4, len(ready))) as executor:
                    futures = {
                        executor.submit(
                            self._execute_ready_step,
                            run_id=run_id,
                            plan=plan,
                            run=execution_run,
                            step=step,
                        ): step["id"]
                        for step in ready
                    }
                    for future in as_completed(futures):
                        step_id = futures[future]
                        try:
                            outcome = future.result()
                        except HubV2Error as exc:
                            errors[step_id] = exc
                            ambiguous = exc.code in {
                                "provider_timeout",
                                "provider_worker_failed",
                                "provider_protocol_error",
                            }
                            updated = self.store.update_step(
                                run_id,
                                step_id=step_id,
                                expected_run_revision=current_revision,
                                status="outcome_unknown" if ambiguous else "failed",
                                provider=str((exc.safe_details or {}).get("provider") or "")
                                or None,
                                checkpoint={
                                    "phase": (
                                        "outcome_unknown" if ambiguous else "provider_failed"
                                    ),
                                    "retry_safe": False,
                                    "error_code": exc.code,
                                },
                            )
                            current_revision = updated["revision"]
                        else:
                            updated = self.store.update_step(
                                run_id,
                                step_id=outcome["step_id"],
                                expected_run_revision=current_revision,
                                status="completed",
                                provider=outcome["provider"],
                                model=outcome["model"],
                                input_artifact_ids=outcome["input_artifact_ids"],
                                output_artifact_ids=[outcome["artifact_id"]],
                                checkpoint=outcome["checkpoint"],
                            )
                            current_revision = updated["revision"]
                        self.store.renew_claim(
                            run_id,
                            claim_token=claim_token,
                            expected_revision=current_revision,
                            lease_seconds=lease_seconds,
                        )
                if errors:
                    ambiguous_errors = [
                        errors[step["id"]]
                        for step in ready
                        if step["id"] in errors
                        and errors[step["id"]].code
                        in {
                            "provider_timeout",
                            "provider_worker_failed",
                            "provider_protocol_error",
                        }
                    ]
                    if ambiguous_errors:
                        raise ambiguous_errors[0]
                    raise next(errors[step["id"]] for step in ready if step["id"] in errors)
                run = self.store.get_run(run_id)
                total_tokens = sum(
                    int((item["checkpoint"].get("usage") or {}).get("total_tokens") or 0)
                    for item in run["steps"]
                )
                elapsed_ms = self._critical_path_elapsed_ms(plan, run)
                if total_tokens > int(plan["task"]["constraints"]["max_tokens"]):
                    raise HubV2Error(
                        "run_token_budget_exhausted",
                        "The run exhausted its token budget.",
                        scope="run",
                    )
                if elapsed_ms > int(float(plan["task"]["constraints"]["timeout_seconds"]) * 1000):
                    raise HubV2Error(
                        "run_time_budget_exhausted",
                        "The run exhausted its time budget.",
                        scope="run",
                    )
                if all(step["status"] == "completed" for step in run["steps"]):
                    break
            final = self.store.get_run(run_id)
            completed = all(step["status"] == "completed" for step in final["steps"])
            self.store.finalize_claim(
                run_id,
                claim_token=claim_token,
                expected_revision=current_revision,
                status="completed" if completed else "paused",
                event_type="run_completed" if completed else "run_paused",
                details={"reason_code": "all_steps_completed" if completed else "wave_budget"},
            )
        except HubV2Error as exc:
            try:
                current = self.store.get_run(run_id)
                status = (
                    "outcome_unknown"
                    if exc.code
                    in {
                        "provider_timeout",
                        "provider_worker_failed",
                        "provider_protocol_error",
                    }
                    else "paused"
                )
                finalized = self.store.finalize_claim(
                    run_id,
                    claim_token=claim_token,
                    expected_revision=current["revision"],
                    status=status,
                    event_type="run_paused",
                    details={
                        "error_code": exc.code,
                        "retryable": exc.retryable,
                        "reason_code": status,
                    },
                )
                if status == "paused":
                    self._attempt_auto_replan(
                        run_id,
                        run=finalized,
                        error_code=exc.code,
                    )
            except HubV2Error as finalize_error:
                try:
                    self.store.record_runtime_event(
                        run_id,
                        event_type="run_finalize_failed",
                        details={
                            "error_code": finalize_error.code,
                            "reason_code": exc.code,
                            "retryable": finalize_error.retryable,
                        },
                    )
                except HubV2Error:
                    pass
        except Exception:  # noqa: BLE001
            try:
                current = self.store.get_run(run_id)
                self.store.finalize_claim(
                    run_id,
                    claim_token=claim_token,
                    expected_revision=current["revision"],
                    status="paused",
                    event_type="run_paused",
                    details={
                        "error_code": "run_internal_error",
                        "retryable": False,
                        "reason_code": "internal_error",
                    },
                )
            except HubV2Error as finalize_error:
                try:
                    self.store.record_runtime_event(
                        run_id,
                        event_type="run_finalize_failed",
                        details={
                            "error_code": finalize_error.code,
                            "reason_code": "run_internal_error",
                            "retryable": finalize_error.retryable,
                        },
                    )
                except HubV2Error:
                    pass
        finally:
            with self._active_lock:
                self._active.pop(run_id, None)

    def _attempt_auto_replan(
        self,
        run_id: str,
        *,
        run: Mapping[str, Any],
        error_code: str,
    ) -> None:
        allowed_errors = {
            "fallback_exhausted",
            "provider_context_limit",
            "context_limit_exceeded",
            "deterministic_verification_failed",
            "capability_changed",
        }
        if error_code not in allowed_errors or run.get("routing_mode") != "auto":
            return
        if int(run.get("replan_count") or 0) >= 1:
            return
        plan = self.store.get_plan(run_id)
        failed_ids = {step["step_id"] for step in run["steps"] if step["status"] == "failed"}
        candidate = deepcopy(plan)
        changed = False
        policy = load_policy(str(run["project_root"])).policy
        for step in candidate["steps"]:
            if step["id"] not in failed_ids:
                continue
            requirements = dict(step.get("routing_requirements") or {})
            current = str(requirements.get("planner_provider") or "")
            declared = [
                str(item)
                for item in requirements.get("fallbacks") or []
                if str(item) in policy["provider_allowlist"]
            ]
            fallback = next((item for item in declared if item != current), None)
            if fallback is None:
                continue
            requirements["planner_provider"] = fallback
            requirements["fallbacks"] = [item for item in declared if item != fallback]
            step["routing_requirements"] = requirements
            changed = True
        if not changed:
            return
        candidate.pop("plan_sha256", None)
        validated_candidate = validate_plan(candidate)
        self._validate_inspection_approval(
            validated_candidate,
            project_root=str(run["project_root"]),
        )
        replanned = self.store.replace_pending_plan(
            run_id,
            expected_revision=int(run["revision"]),
            candidate_plan=validated_candidate,
            reason_code=error_code,
        )
        timer = threading.Timer(
            0.05,
            self.dispatch,
            args=(
                "agent_hub_continue",
                {
                    "run_id": run_id,
                    "expected_revision": replanned["revision"],
                },
            ),
        )
        timer.daemon = False
        timer.start()

    def _tool_continue(self, arguments: dict[str, Any]) -> dict[str, Any]:
        run_id = str(arguments.get("run_id") or "")
        expected = require_non_negative_int(
            arguments.get("expected_revision"),
            field="expected_revision",
        )
        max_waves = int(arguments.get("max_waves", 1))
        if not 1 <= max_waves <= 8:
            raise HubV2Error(
                "invalid_request",
                "max_waves must be between 1 and 8.",
                scope="run",
            )
        plan = self.store.get_plan(run_id)
        timeout_seconds = float(plan["task"]["constraints"].get("timeout_seconds") or 60.0)
        lease_seconds = min(
            MAX_RUN_LEASE_SECONDS,
            max(60.0, timeout_seconds + RUN_LEASE_GRACE_SECONDS),
        )
        claim = self.store.claim_run(
            run_id,
            expected_revision=expected,
            lease_seconds=lease_seconds,
        )
        thread = threading.Thread(
            target=self._run_wave,
            kwargs={
                "run_id": run_id,
                "claim_token": claim.claim_token,
                "claim_revision": claim.revision,
                "max_waves": max_waves,
                "lease_seconds": lease_seconds,
            },
            daemon=False,
            name=f"agent-hub-v2-run-{run_id}",
        )
        thread.start()
        return {
            "schema": "agent_hub_continue_receipt_v2",
            "run_id": run_id,
            "revision": claim.revision,
            "status": "running",
            "lease_expires_at": claim.lease_expires_at,
            "poll": {
                "tool": "agent_hub_get",
                "arguments": {"run_id": run_id},
            },
        }

    def _tool_get(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.store.get_run(
            str(arguments.get("run_id") or ""),
            project_root=arguments.get("project_root"),
        )

    def _tool_events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.store.events(
            str(arguments.get("run_id") or ""),
            after_cursor=int(arguments.get("after_cursor", 0)),
            limit=int(arguments.get("limit", 50)),
            project_root=arguments.get("project_root"),
        )

    def _tool_cancel(self, arguments: dict[str, Any]) -> dict[str, Any]:
        run_id = str(arguments.get("run_id") or "")
        with self._active_lock:
            workers = list(self._active.get(run_id, []))
        for worker in workers:
            worker.cancel()
        return self.store.cancel_run(
            run_id,
            expected_revision=require_non_negative_int(
                arguments.get("expected_revision"),
                field="expected_revision",
            ),
        )

    def _tool_artifact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "")
        artifact_id = str(arguments.get("artifact_id") or "")
        if action == "prune":
            return self.store.prune_expired_artifacts()
        artifact = self.store.get_artifact(artifact_id, include_content=False)
        if action == "verify":
            self._artifact_text(artifact_id)
            return {
                **artifact,
                "verification": {
                    **artifact["verification"],
                    "content_authenticated": True,
                },
            }
        if action == "prepare_export":
            root = Path(str(arguments.get("project_root") or "")).expanduser().resolve(strict=True)
            supplied = Path(str(arguments.get("destination") or ""))
            if supplied.is_absolute() or ".." in supplied.parts or supplied == Path("."):
                raise HubV2Error(
                    "unsafe_export_target",
                    "The export destination must be a project-relative file.",
                    scope="artifact",
                )
            target = (root / supplied).resolve(strict=False)
            if not target.is_relative_to(root) or target == root:
                raise HubV2Error(
                    "unsafe_export_target",
                    "The export destination escapes the project.",
                    scope="artifact",
                )
            current_sha = None
            if target.exists():
                info = target.lstat()
                if (
                    stat.S_ISLNK(info.st_mode)
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                ):
                    raise HubV2Error(
                        "unsafe_export_target",
                        "The export target is not a safe regular file.",
                        scope="artifact",
                    )
                current_sha = sha256(target.read_bytes()).hexdigest()
            proposal = {
                "schema": "agent_hub_artifact_export_proposal_v1",
                "artifact_id": artifact_id,
                "artifact_sha256": artifact["content_sha256"],
                "project_root": str(root),
                "destination_alias": target.relative_to(root).as_posix(),
                "before_sha256": current_sha,
            }
            proposal["proposal_sha256"] = sha256(
                canonical_json(proposal).encode("utf-8")
            ).hexdigest()
            return proposal
        if action == "apply_export":
            proposal = arguments.get("proposal")
            if not isinstance(proposal, Mapping):
                raise HubV2Error(
                    "invalid_request",
                    "The reviewed export proposal is required.",
                    scope="artifact",
                )
            unsigned = dict(proposal)
            supplied_digest = str(unsigned.pop("proposal_sha256", ""))
            calculated = sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
            if supplied_digest != calculated or supplied_digest != str(
                arguments.get("proposal_sha256") or ""
            ):
                raise HubV2Error(
                    "proposal_digest_conflict",
                    "The export proposal digest does not match.",
                    scope="artifact",
                )
            if str(proposal.get("artifact_id") or "") != artifact_id:
                raise HubV2Error(
                    "proposal_digest_conflict",
                    "The export proposal targets a different artifact.",
                    scope="artifact",
                )
            if proposal.get("artifact_sha256") != artifact["content_sha256"]:
                raise HubV2Error(
                    "artifact_revision_conflict",
                    "The artifact identity changed after export planning.",
                    scope="artifact",
                )
            root = Path(str(proposal.get("project_root") or "")).resolve(strict=True)
            alias = Path(str(proposal.get("destination_alias") or ""))
            target = (root / alias).resolve(strict=False)
            if (
                alias.is_absolute()
                or ".." in alias.parts
                or not target.is_relative_to(root)
                or target == root
            ):
                raise HubV2Error(
                    "unsafe_export_target",
                    "The export proposal has an unsafe destination.",
                    scope="artifact",
                )
            before = None
            if target.exists():
                info = target.lstat()
                if (
                    stat.S_ISLNK(info.st_mode)
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                ):
                    raise HubV2Error(
                        "unsafe_export_target",
                        "The export target is not a safe regular file.",
                        scope="artifact",
                    )
                before = sha256(target.read_bytes()).hexdigest()
            if before != proposal.get("before_sha256"):
                raise HubV2Error(
                    "export_target_conflict",
                    "The export target changed after planning.",
                    scope="artifact",
                    retryable=True,
                )
            plaintext = self._artifact_text(artifact_id).encode("utf-8")
            if sha256(plaintext).hexdigest() != artifact["content_sha256"]:
                raise HubV2Error(
                    "artifact_integrity_failed",
                    "The artifact failed content verification.",
                    scope="artifact",
                )
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=".agent-hub-export.",
                dir=target.parent,
            )
            temp = Path(temp_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(plaintext)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp, 0o600)
                os.replace(temp, target)
            finally:
                if temp.exists():
                    temp.unlink()
            return self.store.record_artifact_export(
                artifact_id=artifact_id,
                destination_alias=alias.as_posix(),
                content_sha256=artifact["content_sha256"],
            )
        if action != "get":
            raise HubV2Error(
                "invalid_request",
                "artifact action is not supported.",
                scope="artifact",
            )
        if arguments.get("include_text") is True:
            artifact["text"] = self._artifact_text(artifact_id)
        return artifact

    def _tool_feedback(self, arguments: dict[str, Any]) -> dict[str, Any]:
        run_id = str(arguments.get("run_id") or "")
        step_id = arguments.get("step_id")
        feedback = self.store.record_feedback(
            run_id=run_id,
            step_id=str(step_id) if step_id else None,
            expected_revision=require_non_negative_int(
                arguments.get("expected_revision"),
                field="expected_revision",
            ),
            outcome=str(arguments.get("outcome") or ""),
            rating=arguments.get("rating"),
        )
        if step_id:
            run = self.store.get_run(run_id)
            step = next(
                (item for item in run["steps"] if item["step_id"] == step_id),
                None,
            )
            plan = self.store.get_plan(run_id)
            if step and step.get("provider"):
                rating = arguments.get("rating")
                outcome = str(arguments.get("outcome") or "")
                quality = (
                    (int(rating) - 1) / 4
                    if rating is not None
                    else {"accepted": 1.0, "partial": 0.5, "rejected": 0.0}.get(outcome)
                )
                self.store.record_routing_sample(
                    context=routing_context(plan["task"], model=step.get("model")),
                    provider=step["provider"],
                    model=step.get("model"),
                    capability=step["capability"],
                    success=outcome not in {"rejected", "failed"},
                    quality=quality,
                    latency_ms=step["checkpoint"].get("elapsed_ms"),
                    total_tokens=None,
                    signal_weight=feedback["signal_weight"],
                )
        return feedback

    def _tool_policy(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "")
        project_root = str(arguments.get("project_root") or "")
        if action == "get":
            return load_policy(project_root).public()
        if action == "prepare_update":
            patch = arguments.get("patch")
            if not isinstance(patch, Mapping):
                raise HubV2Error(
                    "invalid_request",
                    "policy patch must be an object.",
                    scope="policy",
                )
            return prepare_policy_update(
                project_root,
                patch=patch,
                expected_revision=require_non_negative_int(
                    arguments.get("expected_revision"),
                    field="expected_revision",
                ),
            )
        if action == "apply_update":
            proposal = arguments.get("proposal")
            if not isinstance(proposal, Mapping):
                raise HubV2Error(
                    "invalid_request",
                    "policy proposal must be an object.",
                    scope="policy",
                )
            return apply_policy_update(
                project_root,
                proposal=proposal,
                proposal_sha256=str(arguments.get("proposal_sha256") or ""),
            ).public()
        raise HubV2Error(
            "invalid_request",
            "policy action is not supported.",
            scope="policy",
        )

    def _tool_handoff(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action") or "")
        project_root = str(arguments.get("project_root") or "")
        supplied = arguments.get("arguments")
        extra = dict(supplied) if isinstance(supplied, Mapping) else {}
        if action == "get":
            snapshot = handoff_state.load_handoff(
                project_root,
                mode=str(extra.get("mode") or "auto"),
                search=str(extra.get("search") or "nearest"),
                file=str(extra.get("file") or ""),
                max_chars=int(extra.get("max_chars") or handoff_state.DEFAULT_MAX_CHARS),
            )
            return {
                "success": True,
                "text": (
                    handoff_state.render_context(snapshot)
                    if snapshot.get("loaded")
                    else "No project handoff was found."
                ),
                "handoff": handoff_state.public_snapshot(snapshot),
            }
        if action == "prepare_update":
            kwargs: dict[str, Any] = {
                "body": str(extra.get("body") or ""),
                "file": str(extra.get("file") or ""),
                "search": str(extra.get("search") or "project-only"),
            }
            if "base_managed_sha256" in extra:
                kwargs["base_managed_sha256"] = extra.get("base_managed_sha256")
            return {
                "success": True,
                "text": "Handoff update prepared; no file was changed.",
                **handoff_state.prepare_handoff_update(project_root, **kwargs),
            }
        if action == "apply_update":
            if "expected_sha256" not in extra:
                raise HubV2Error(
                    "invalid_request",
                    "expected_sha256 is required for a handoff update.",
                    scope="handoff",
                )
            return {
                "success": True,
                "text": "HANDOFF.md updated atomically.",
                **handoff_state.apply_handoff_update(
                    project_root,
                    file=str(extra.get("file") or ""),
                    content=str(extra.get("content") or ""),
                    expected_sha256=extra.get("expected_sha256"),
                ),
            }
        if action == "takeover":
            run = self.store.get_run(
                str(extra.get("run_id") or ""),
                project_root=project_root,
            )
            plan = self.store.get_plan(run["run_id"])
            artifact_ids = sorted(
                {
                    artifact_id
                    for step in run["steps"]
                    for artifact_id in step["output_artifact_ids"]
                }
            )
            artifacts = [
                {
                    "artifact_id": artifact_id,
                    "content_sha256": self.store.get_artifact(artifact_id)["content_sha256"],
                }
                for artifact_id in artifact_ids
            ]
            capsule = {
                "schema": "agent_hub_takeover_capsule_v2",
                "run_id": run["run_id"],
                "revision": run["revision"],
                "status": run["status"],
                "plan_sha256": str(plan.get("plan_sha256") or ""),
                "artifacts": artifacts,
            }
            return {
                "success": True,
                "text": "Takeover capsule prepared from the durable run store.",
                "capsule": {
                    **capsule,
                    "capsule_sha256": sha256(canonical_json(capsule).encode("utf-8")).hexdigest(),
                },
            }
        raise HubV2Error(
            "invalid_request",
            "handoff action is not supported.",
            scope="handoff",
        )

    def _tool_doctor(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_root = str(arguments.get("project_root") or "")
        host_checks = run_doctor(
            project_root,
            live=bool(arguments.get("live", False)),
        )
        store_health = self.store.health()
        repair = str(arguments.get("repair") or "none")
        repair_plan = []
        if repair == "prepare":
            if not store_health["ok"]:
                repair_plan.append(
                    {
                        "action": "restore_store_backup",
                        "automatic": False,
                        "reason_code": "store_integrity_failed",
                    }
                )
            for check in host_checks.get("checks", []):
                if check.get("status") == "fail":
                    repair_plan.append(
                        {
                            "action": "review_host_check",
                            "automatic": False,
                            "check_id": check.get("id"),
                        }
                    )
        return {
            "schema": "agent_hub_doctor_v2",
            "store": store_health,
            "host_checks": host_checks,
            "repair_plan": repair_plan,
            "read_only": True,
        }


def definitions() -> list[dict[str, Any]]:
    return tool_definitions()
