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
from statistics import median_low
from typing import Any, Callable, Mapping, NoReturn

from agent_hub import __version__
from agent_hub.core import handoff as handoff_state
from agent_hub.doctor import run_doctor

from .context import collect_scoped_fact_pack, index_project, search_fact_pack
from .contracts import (
    CAPABILITIES,
    PLAN_SCHEMA,
    TASK_SCHEMA,
    canonical_json,
    canonical_project_root,
    estimate_tokens_from_text,
    input_token_limit,
    normalize_token_usage,
    require_non_negative_int,
    safe_usage,
    total_token_limit,
    validate_plan,
    validate_reconciliation_resolutions,
    validate_task,
)
from .crypto import ArtifactCipher, MacOSKeychainKeyProvider
from .dependency_context import (
    DependencyContextPart,
    assemble_dependency_context,
    enforce_provider_context_budget,
)
from .egress import prepare_egress, redact_secret_lines, verify_egress_approval
from .errors import HubV2Error, public_failure, safe_unexpected_error
from .policy import (
    apply_policy_update,
    load_policy,
    prepare_policy_update,
)
from .provider_client import ProviderWorkerClient
from .provider_manifests import builtin_provider_manifests, manifest_for, model_input_limit
from .routing import route, routing_context
from .routing_prior import (
    apply_routing_prior_update,
    load_routing_prior,
    prepare_routing_prior_update,
    safe_load_routing_prior,
)
from .store import HubStore
from .tools import TOOL_NAMES, tool_definitions
from .verifier import verify_output

MAX_PLANNER_MANIFEST_CHARS = 32_000
MODEL_CATALOG_LIMIT_CACHE_TTL_SECONDS = 300.0
RUN_LEASE_GRACE_SECONDS = 60.0
MAX_RUN_LEASE_SECONDS = 3600.0
DEFAULT_STEP_TOKEN_ESTIMATE = 8_000
TOKEN_ESTIMATE_LOOKBACK_DAYS = 30.0
TOKEN_ESTIMATE_MIN_SAMPLES = 3
# Ordered weakest to strongest so a mixed wave reports its least reliable source.
_ESTIMATE_CONFIDENCE = (
    "default",
    "run_history",
    "routing_samples_capability",
    "routing_samples_provider",
)


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
        self._catalog_limit_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._catalog_limit_cache_lock = threading.Lock()

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
        except handoff_state.HandoffError as exc:
            response = public_failure(self._safe_handoff_error(exc), operation=name)
        except Exception:  # noqa: BLE001
            response = safe_unexpected_error(operation=name)
        try:
            failure = response.get("error")
            self.store.record_operation_metric(
                operation=name,
                success=response.get("success") is True,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_code=failure.get("code") if isinstance(failure, Mapping) else None,
            )
        except Exception:  # noqa: BLE001
            pass
        return response

    def pending_egress_reviews(self) -> dict[str, Any]:
        reviews = self.store.list_egress_reviews()
        return {
            "schema": "agent_hub_egress_review_list_v1",
            "reviews": reviews,
            "pending_count": sum(item["status"] == "pending" for item in reviews),
            "approved_count": sum(item["status"] == "approved" for item in reviews),
        }

    def decide_egress_review(
        self,
        review_id: str,
        *,
        decision: str,
    ) -> dict[str, Any]:
        return self.store.decide_egress_review(review_id, decision=decision)

    def egress_settings(self) -> dict[str, Any]:
        return self.store.egress_settings()

    def update_egress_settings(
        self,
        *,
        auto_approve: bool,
        expected_revision: int,
    ) -> dict[str, Any]:
        return self.store.update_egress_settings(
            auto_approve=auto_approve,
            expected_revision=expected_revision,
        )

    @staticmethod
    def _safe_handoff_error(error: handoff_state.HandoffError) -> HubV2Error:
        if isinstance(
            error,
            (
                handoff_state.HandoffManagedRevisionConflict,
                handoff_state.HandoffRevisionConflict,
            ),
        ):
            revision_kind = (
                "managed_block"
                if isinstance(error, handoff_state.HandoffManagedRevisionConflict)
                else "file"
            )
            return HubV2Error(
                "handoff_revision_conflict",
                "HANDOFF.md changed after the update was prepared.",
                scope="handoff",
                retryable=True,
                safe_details={
                    "revision_kind": revision_kind,
                    "expected_sha256": error.expected,
                    "current_sha256": error.current,
                },
                next_action={
                    "type": "call_tool",
                    "tool": "agent_hub_handoff",
                    "action": "prepare_update",
                },
            )
        if isinstance(error, handoff_state.HandoffQualityError):
            return HubV2Error(
                "handoff_quality_invalid",
                "The HANDOFF.md managed body did not pass quality checks.",
                scope="handoff",
                safe_details={"issue_count": len(error.issues)},
            )
        if isinstance(error, handoff_state.HandoffUnsafePath):
            return HubV2Error(
                "handoff_path_denied",
                "The requested HANDOFF.md path is not safe for this project.",
                scope="handoff",
            )
        if isinstance(error, handoff_state.HandoffNotFound):
            return HubV2Error(
                "handoff_not_found",
                "No project HANDOFF.md could be found.",
                scope="handoff",
            )
        return HubV2Error(
            "invalid_request",
            "The HANDOFF.md request is invalid.",
            scope="handoff",
        )

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
        for field in (
            "timeout_seconds",
            "max_input_tokens",
            "max_output_tokens",
            "max_total_tokens",
            "max_leaf_calls",
        ):
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
    def _routing_models(
        providers: list[str],
        states: Mapping[str, Mapping[str, Any]],
        *,
        planner_provider: str,
        requested_model: str | None,
    ) -> dict[str, str]:
        return {
            provider: (
                str(requested_model)
                if provider == planner_provider and requested_model
                else str(states.get(provider, {}).get("default_model") or "")
            )
            for provider in providers
        }

    def _cache_catalog_limits(
        self,
        provider: str,
        model_payload: Mapping[str, Any],
        *,
        catalog_state: str,
    ) -> dict[str, Any]:
        raw_models = model_payload.get("text_models")
        if not isinstance(raw_models, list):
            raw_models = model_payload.get("models")
        safe_models: list[dict[str, Any]] = []
        for raw in raw_models if isinstance(raw_models, list) else []:
            if not isinstance(raw, Mapping):
                continue
            model = str(raw.get("id") or "")
            limit = raw.get("max_input_tokens")
            if (
                not model
                or not isinstance(limit, int)
                or isinstance(limit, bool)
                or not 1 <= limit <= 10_000_000
            ):
                continue
            safe_models.append(
                {
                    "provider": provider,
                    "model": model,
                    "max_input_tokens": limit,
                }
            )
        safe_models.sort(key=lambda item: item["model"])
        revision = (
            sha256(canonical_json(safe_models).encode("utf-8")).hexdigest() if safe_models else None
        )
        if safe_models and catalog_state in {"live", "cached"}:
            expires_at = time.monotonic() + MODEL_CATALOG_LIMIT_CACHE_TTL_SECONDS
            with self._catalog_limit_cache_lock:
                for key in [key for key in self._catalog_limit_cache if key[0] == provider]:
                    self._catalog_limit_cache.pop(key, None)
                for item in safe_models:
                    self._catalog_limit_cache[(provider, item["model"])] = {
                        **item,
                        "source": ("live_catalog" if catalog_state == "live" else "cached_catalog"),
                        "catalog_revision": revision,
                        "expires_at": expires_at,
                    }
        return {
            "catalog_revision": revision,
            "context_limit_model_count": len(safe_models),
            "context_limit_ttl_seconds": int(MODEL_CATALOG_LIMIT_CACHE_TTL_SECONDS),
        }

    def _cached_model_limit(self, provider: str, model: str | None) -> dict[str, Any] | None:
        selected_model = str(model or "")
        if not selected_model:
            return None
        now = time.monotonic()
        with self._catalog_limit_cache_lock:
            cached = self._catalog_limit_cache.get((provider, selected_model))
            if cached is None:
                return None
            if float(cached.get("expires_at") or 0.0) <= now:
                self._catalog_limit_cache.pop((provider, selected_model), None)
                return None
            return {key: value for key, value in cached.items() if key != "expires_at"}

    def _routing_model_limits(self, models: Mapping[str, str]) -> dict[str, dict[str, Any]]:
        return {
            provider: cached
            for provider, model in models.items()
            if (cached := self._cached_model_limit(provider, model)) is not None
        }

    @staticmethod
    def _auth_state(state: Mapping[str, Any]) -> str:
        if state.get("ready") or state.get("auth_ready"):
            return "callable"
        if state.get("logged_in") and state.get("refreshable"):
            return "refreshable"
        if state.get("relogin_required") or not state.get("logged_in", False):
            return "login_required"
        return "unavailable"

    @staticmethod
    def _invocation_ready(state: Mapping[str, Any]) -> bool:
        return bool(state.get("invocation_ready", state.get("ready")))

    def _cache_provider_state(self, provider: str, state: Mapping[str, Any]) -> None:
        with self._status_cache_lock:
            self._status_cache[provider] = (time.monotonic(), dict(state))

    def _invalidate_provider_state(self, provider: str) -> None:
        with self._status_cache_lock:
            self._status_cache.pop(provider, None)

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
                    readiness[provider] = self._invocation_ready(cached[1])
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
                    readiness[provider] = self._invocation_ready(state)
                    self._cache_provider_state(provider, state)
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
                    "ready": bool(states[provider].get("ready")),
                    "invocation_ready": readiness[provider],
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
                self._cache_provider_state(provider_id, state)
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
                catalog_limit_state = (
                    self._cache_catalog_limits(
                        provider_id,
                        model_payload,
                        catalog_state=catalog_state,
                    )
                    if isinstance(model_payload, Mapping)
                    else {
                        "catalog_revision": None,
                        "context_limit_model_count": 0,
                        "context_limit_ttl_seconds": int(MODEL_CATALOG_LIMIT_CACHE_TTL_SECONDS),
                    }
                )
                catalog[provider_id] = {
                    "manifest": manifest,
                    "auth_state": self._auth_state(state),
                    "catalog_state": catalog_state,
                    "generation_state": (
                        verification["generation_state"] if verification else "unknown"
                    ),
                    "generation_verification": verification,
                    **catalog_limit_state,
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
        task, execute_policy = self._enforce_task_policy(
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
        base_context_budget = enforce_provider_context_budget(
            str(task.get("inline_input") or ""),
            requested_max_input_tokens=input_token_limit(task["constraints"]),
            context_stats={"source_artifact_count": 0},
        )
        readiness, states = self._readiness(allowed)
        models = self._routing_models(
            allowed,
            states,
            planner_provider=planner_provider,
            requested_model=str(arguments.get("model") or "") or None,
        )
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
            models=models,
            model_limits=self._routing_model_limits(models),
            estimated_input_tokens=base_context_budget["estimated_input_tokens"],
            routing_profile=execute_policy["routing_profile"],
            prior=safe_load_routing_prior(),
        )
        provider = decision["selected_provider"]
        selected_model = models.get(provider) or None
        selected_limit = model_input_limit(
            provider,
            selected_model,
            observed=self._cached_model_limit(provider, selected_model),
        )
        context_budget = enforce_provider_context_budget(
            str(task.get("inline_input") or ""),
            requested_max_input_tokens=input_token_limit(task["constraints"]),
            model_max_input_tokens=int(selected_limit["max_input_tokens"]),
            provider=provider,
            model=selected_model,
            context_stats={"source_artifact_count": 0},
        )
        try:
            result = self._worker(provider).request(
                "invoke",
                {"task": task, "model": selected_model},
                timeout=float(task["constraints"].get("timeout_seconds") or 1790),
            )
        except HubV2Error as exc:
            self._invalidate_provider_state(provider)
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
        self._invalidate_provider_state(provider)
        self.store.record_provider_outcome(provider=provider, success=True)
        resolved_model = str(result.get("model") or selected_model or "")
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
            "context_budget": context_budget,
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
                estimated_max_tokens=total_token_limit(
                    task["constraints"],
                    default=int(policy.policy["budgets"]["max_total_tokens"]),
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
            if proposal["manifest"].get("entries"):
                review = self.store.prepare_egress_review(
                    project_root=project_root,
                    proposal=proposal,
                )
                review = self.store.maybe_auto_approve_egress_review(review["review_id"])
                proposal["approval_request"] = review
                if review["status"] == "approved":
                    proposal["approval_required"] = False
                    proposal["approval_mode"] = "automatic"
                    proposal["next_action"] = {
                        "type": "call_tool",
                        "tool": "agent_hub_plan",
                        "mode": "apply",
                        "approval_request_id": review["review_id"],
                    }
                else:
                    proposal["approval_required"] = True
                    proposal["approval_mode"] = "manual"
                    proposal["next_action"] = {
                        "type": "local_gui",
                        "command": "agent-hub-connect",
                        "review_id": review["review_id"],
                    }
            else:
                proposal["approval_required"] = False
                proposal["approval_mode"] = "not_required"
                proposal["approval_request"] = None
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
        if verified["manifest"].get("entries"):
            self.store.consume_egress_review(
                str(arguments.get("approval_request_id") or ""),
                project_root=project_root,
                proposal_sha256=proposal_sha,
                manifest_sha256=str(verified["manifest"]["manifest_sha256"]),
                policy_revision=expected_policy_revision,
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
            if capability not in CAPABILITIES:
                raise HubV2Error(
                    "planner_capability_violation",
                    "The planner selected a capability outside the v2 runtime contract.",
                    scope="planner",
                    safe_details={"capability": capability},
                )
            output_contract = dict(step.get("output_contract") or {})
            planned_provider = str(step.get("provider") or provider)
            planned_model = str(
                step.get("model") or (model if planned_provider == provider else "") or ""
            )
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
            routing_requirements: dict[str, Any] = {
                "planner_provider": planned_provider,
                "fallbacks": planned_fallbacks,
            }
            if planned_model:
                routing_requirements["model"] = planned_model
            steps.append(
                {
                    "id": str(step.get("id") or f"step_{index + 1}"),
                    "capability": capability,
                    "depends_on": list(step.get("depends_on") or []),
                    "instruction": str(
                        step.get("instruction") or step.get("prompt") or task["intent"]
                    ),
                    "routing_requirements": routing_requirements,
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

    @staticmethod
    def _step_source_refs(
        plan: Mapping[str, Any],
        run: Mapping[str, Any],
        step: Mapping[str, Any],
    ) -> list[str]:
        return list(
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

    def _execute_ready_step(
        self,
        *,
        run_id: str,
        plan: Mapping[str, Any],
        run: Mapping[str, Any],
        step: Mapping[str, Any],
    ) -> dict[str, Any]:
        started = time.monotonic()
        source_refs = self._step_source_refs(plan, run, step)
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
            # A local inspect spends no provider tokens. "local" is recorded rather
            # than "unset" so the ledger distinguishes measured-zero from unmeasured.
            token_usage: dict[str, Any] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "source": "local",
            }
        else:
            input_artifacts = set(plan["task"].get("input_artifacts") or [])
            inline_consent = set(plan.get("inline_consent_artifacts") or [])
            approval = self.store.get_egress_approval(str(plan.get("egress_manifest_sha256") or ""))
            artifact_entries = {
                str(entry.get("artifact_id") or ""): entry
                for entry in (approval or {}).get("entries", [])
                if entry.get("kind") == "artifact"
            }
            plan_steps = {str(item["id"]): item for item in plan["steps"]}
            run_steps = {str(item["step_id"]): item for item in run["steps"]}
            dependency_parts: list[DependencyContextPart] = []
            for artifact_id in source_refs:
                artifact_text = self._artifact_text(artifact_id)
                artifact = self.store.get_artifact(artifact_id, include_content=False)
                if artifact_id in input_artifacts and artifact_id not in inline_consent:
                    entry = artifact_entries.get(artifact_id)
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
                producer_step_id = str(artifact.get("producer_step_id") or "")
                producer_plan_step = plan_steps.get(producer_step_id)
                producer_run_step = run_steps.get(producer_step_id)
                trusted_fact_pack = bool(
                    artifact.get("run_id") == run_id
                    and producer_plan_step
                    and producer_plan_step.get("capability") == "inspect"
                    and producer_run_step
                    and producer_run_step.get("status") == "completed"
                    and producer_run_step.get("provider") == "local"
                    and artifact_id in producer_run_step.get("output_artifact_ids", [])
                    and artifact.get("media_type") == "application/json"
                )
                dependency_parts.append(
                    DependencyContextPart(
                        text=artifact_text,
                        artifact_id=artifact_id,
                        trusted_fact_pack=trusted_fact_pack,
                    )
                )
            dependency_text, dependency_stats = assemble_dependency_context(dependency_parts)
            base_context_budget = enforce_provider_context_budget(
                dependency_text,
                requested_max_input_tokens=input_token_limit(plan["task"]["constraints"]),
                context_stats=dependency_stats,
            )
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
            readiness, states = self._readiness(routing_allowlist)
            models = self._routing_models(
                routing_allowlist,
                states,
                planner_provider=planner_provider,
                requested_model=requested_model,
            )
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
                models=models,
                model_limits=self._routing_model_limits(models),
                estimated_input_tokens=base_context_budget["estimated_input_tokens"],
                # Read from policy rather than the plan: adding a field to the plan
                # would change digest_json and invalidate every issued plan_sha256.
                routing_profile=load_policy(str(run["project_root"])).policy["routing_profile"],
                prior=safe_load_routing_prior(),
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
                candidate_model = models.get(candidate) or None
                candidate_limit = model_input_limit(
                    candidate,
                    candidate_model,
                    observed=self._cached_model_limit(candidate, candidate_model),
                )
                context_budget = enforce_provider_context_budget(
                    dependency_text,
                    requested_max_input_tokens=input_token_limit(plan["task"]["constraints"]),
                    model_max_input_tokens=int(candidate_limit["max_input_tokens"]),
                    provider=candidate,
                    model=candidate_model,
                    context_stats=dependency_stats,
                )
                worker = self._worker(provider)
                attempt_started = time.monotonic()
                correlation_id = f"{run_id}.{step['id']}.{provider}"
                self.store.record_runtime_event(
                    run_id,
                    event_type="provider_attempt_started",
                    details={
                        "step_id": str(step["id"]),
                        "provider": provider,
                        "model": candidate_model,
                        "capability": str(step["capability"]),
                        "correlation_id": correlation_id,
                    },
                )
                with self._active_lock:
                    self._active.setdefault(run_id, []).append(worker)
                try:
                    result = worker.request(
                        "invoke",
                        {"task": task, "model": candidate_model},
                        timeout=float(task["constraints"].get("timeout_seconds") or 1790),
                        request_id=correlation_id,
                    )
                except HubV2Error as exc:
                    self._invalidate_provider_state(provider)
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
                            "model": candidate_model,
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
                self._invalidate_provider_state(provider)
                self.store.record_runtime_event(
                    run_id,
                    event_type="provider_attempt_completed",
                    details={
                        "step_id": str(step["id"]),
                        "provider": provider,
                        "model": candidate_model,
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
                    retryable=bool(last_error and last_error.retryable),
                    safe_details={"last_error_code": last_error.code if last_error else "unknown"},
                )
            text = _structured_text(result)
            model = (
                result.get("model")
                if isinstance(result.get("model"), str)
                else models.get(provider) or None
            )
            media_type = "text/plain; charset=utf-8"
            checkpoint = {
                "routing_decision_id": decision["decision_id"],
                "context_source_artifacts": dependency_stats["source_artifact_count"],
                "context_segments": dependency_stats["context_segment_count"],
                "context_duplicate_artifacts": dependency_stats["duplicate_artifact_count"],
                "context_fact_pack_count": dependency_stats["fact_pack_count"],
                "context_untrusted_fact_pack_count": dependency_stats["untrusted_fact_pack_count"],
                "context_fact_pack_items": dependency_stats["fact_pack_item_count"],
                "context_fact_pack_duplicate_items": dependency_stats[
                    "fact_pack_duplicate_item_count"
                ],
                "context_chars": context_budget["context_chars"],
                "context_bytes": context_budget["context_bytes"],
                "context_estimated_input_tokens": context_budget["estimated_input_tokens"],
                "context_token_budget": context_budget["token_budget"],
                "context_model_max_input_tokens": context_budget["model_max_input_tokens"],
                "context_effective_input_limit": context_budget["effective_input_limit"],
            }
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
            # `context_budget` only counts assembled dependency context, so the
            # input estimate is a floor: intent and wrapper text are not included.
            token_usage = normalize_token_usage(
                result.get("usage") if isinstance(result, Mapping) else None,
                estimated_input_tokens=int(context_budget["estimated_input_tokens"]),
                estimated_output_tokens=estimate_tokens_from_text(text),
            )
            # Purely estimated numbers would pollute the routing efficiency score.
            total_tokens = (
                token_usage["total_tokens"]
                if token_usage["source"] in {"reported", "mixed"}
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
            "token_usage": token_usage,
        }

    def _raise_token_budget_exhausted(
        self,
        run_id: str,
        *,
        run: Mapping[str, Any],
        reason_code: str,
    ) -> NoReturn:
        usage = run["token_usage"]
        raise HubV2Error(
            "run_token_budget_exhausted",
            "The run exhausted its token budget.",
            scope="run",
            # Resumable: the caller can grant more budget and continue.
            retryable=True,
            safe_details={
                "reason_code": reason_code,
                "tokens_used": usage["total_tokens"],
                "tokens_budget": usage["max_total_tokens"],
                "budget_used_percent": usage["budget_used_percent"],
            },
            next_action={
                "type": "call_tool",
                "tool": "agent_hub_continue",
                "arguments": {
                    "run_id": run_id,
                    "expected_revision": run["revision"],
                    "token_budget_grant": max(1, usage["max_total_tokens"]),
                },
            },
        )

    def _forecast_wave_tokens(
        self,
        run: Mapping[str, Any],
        ready: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Estimate the next wave's token spend from observed history.

        Uses routing samples rather than the assembled dependency context because
        building that context requires decrypting artifacts, which the wave itself
        already does once per step.
        """

        history: dict[str, list[int]] = {}
        for step in run["steps"]:
            spent = int(step.get("total_tokens") or 0)
            if step["status"] == "completed" and spent > 0:
                history.setdefault(str(step["capability"]), []).append(spent)
        total = 0
        sources: list[str] = []
        # Cache per (capability, provider): a wave usually repeats the same pair,
        # and each lookup costs one SQLite connection.
        estimates: dict[tuple[str, str], dict[str, Any]] = {}
        for step in ready:
            capability = str(step["capability"])
            if capability == "inspect":
                continue
            planner_provider = str(
                (step.get("routing_requirements") or {}).get("planner_provider") or ""
            )
            cache_key = (capability, planner_provider)
            estimate = estimates.get(cache_key)
            if estimate is None:
                estimate = self.store.capability_token_estimate(
                    capability=capability,
                    provider=planner_provider or None,
                    lookback_days=TOKEN_ESTIMATE_LOOKBACK_DAYS,
                    minimum_samples=TOKEN_ESTIMATE_MIN_SAMPLES,
                )
                estimates[cache_key] = estimate
            median = estimate["median_total_tokens"]
            if median is not None:
                total += int(median)
                sources.append(str(estimate["source"]))
                continue
            observed = history.get(capability)
            if observed:
                total += median_low(observed)
                sources.append("run_history")
                continue
            total += DEFAULT_STEP_TOKEN_ESTIMATE
            sources.append("default")
        return {
            "tokens_estimated_wave": total,
            # Report the weakest source so the warning is not oversold.
            "estimate_source": (
                min(sources, key=_ESTIMATE_CONFIDENCE.index) if sources else "none"
            ),
            "wave_step_count": len(sources),
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
                usage = run["token_usage"]
                if usage["exhausted"]:
                    self._raise_token_budget_exhausted(
                        run_id,
                        run=run,
                        reason_code="pre_wave_gate",
                    )
                forecast = self._forecast_wave_tokens(run, ready)
                if forecast["tokens_estimated_wave"] > usage["remaining_tokens"]:
                    # Advisory only: the forecast is an estimate and must not block.
                    self.store.record_runtime_event(
                        run_id,
                        event_type="run_token_budget_warning",
                        details={
                            "reason_code": "wave_estimate_exceeds_remaining_budget",
                            "tokens_used": usage["total_tokens"],
                            "tokens_budget": usage["max_total_tokens"],
                            "tokens_remaining": usage["remaining_tokens"],
                            "tokens_estimated_wave": forecast["tokens_estimated_wave"],
                            "budget_used_percent": usage["budget_used_percent"],
                            "wave_step_count": forecast["wave_step_count"],
                            "estimate_source": forecast["estimate_source"],
                        },
                    )
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
                                    "retry_safe": bool(exc.retryable and not ambiguous),
                                    "error_code": exc.code,
                                },
                            )
                            current_revision = updated["revision"]
                        except Exception:  # noqa: BLE001 - isolate one failed wave future
                            exc = HubV2Error(
                                "run_internal_error",
                                "A run step failed unexpectedly.",
                                scope="run",
                                retryable=False,
                            )
                            errors[step_id] = exc
                            updated = self.store.update_step(
                                run_id,
                                step_id=step_id,
                                expected_run_revision=current_revision,
                                status="failed",
                                checkpoint={
                                    "phase": "internal_error",
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
                                token_usage=outcome.get("token_usage"),
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
                elapsed_ms = self._critical_path_elapsed_ms(plan, run)
                if run["token_usage"]["exhausted"]:
                    self._raise_token_budget_exhausted(
                        run_id,
                        run=run,
                        reason_code="post_wave_ledger",
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
                if current["status"] == "cancelled":
                    return
                reconciled = self.store.reconcile_running_steps(
                    run_id,
                    claim_token=claim_token,
                    expected_revision=current["revision"],
                    reason_code=exc.code,
                )
                current = reconciled["run"]
                status = (
                    "outcome_unknown"
                    if reconciled["outcome_unknown_step_ids"]
                    or exc.code
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
                    if self.store.get_run(run_id)["status"] == "cancelled":
                        return
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
                if current["status"] == "cancelled":
                    return
                reconciled = self.store.reconcile_running_steps(
                    run_id,
                    claim_token=claim_token,
                    expected_revision=current["revision"],
                    reason_code="run_internal_error",
                )
                current = reconciled["run"]
                self.store.finalize_claim(
                    run_id,
                    claim_token=claim_token,
                    expected_revision=current["revision"],
                    status=(
                        "outcome_unknown" if reconciled["outcome_unknown_step_ids"] else "paused"
                    ),
                    event_type="run_paused",
                    details={
                        "error_code": "run_internal_error",
                        "retryable": False,
                        "reason_code": "internal_error",
                    },
                )
            except HubV2Error as finalize_error:
                try:
                    if self.store.get_run(run_id)["status"] == "cancelled":
                        return
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
        token_budget_grant = arguments.get("token_budget_grant")
        if token_budget_grant is not None:
            # Granted before any requeue so both can arrive in one continue call.
            granted = self.store.grant_token_budget(
                run_id,
                expected_revision=expected,
                additional_tokens=require_non_negative_int(
                    token_budget_grant, field="token_budget_grant"
                ),
            )
            expected = int(granted["revision"])
        retry_failed_steps = arguments.get("retry_failed_steps")
        if retry_failed_steps is not None:
            if not isinstance(retry_failed_steps, list):
                raise HubV2Error(
                    "invalid_request",
                    "retry_failed_steps must be an array.",
                    scope="run",
                )
            retried = self.store.requeue_failed_steps(
                run_id,
                expected_revision=expected,
                step_ids=retry_failed_steps,
            )
            expected = int(retried["revision"])
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
        action = str(arguments.get("action") or "cancel")
        expected = require_non_negative_int(
            arguments.get("expected_revision"),
            field="expected_revision",
        )
        if action == "prepare_reconcile":
            normalized, _ = validate_reconciliation_resolutions(
                arguments.get("resolutions"),
                run_disposition=arguments.get("run_disposition"),
            )
            return self.store.prepare_run_reconciliation(
                run_id,
                expected_revision=expected,
                resolutions=normalized,
            )
        if action == "apply_reconcile":
            return self._apply_reconcile(run_id, expected_revision=expected, arguments=arguments)
        if action != "cancel":
            raise HubV2Error(
                "invalid_request",
                "cancel action is not supported.",
                scope="run",
            )
        cancelled = self.store.cancel_run(run_id, expected_revision=expected)
        with self._active_lock:
            workers = list(self._active.get(run_id, []))
        for worker in workers:
            try:
                worker.cancel()
            except Exception:  # noqa: BLE001 - cancellation is best effort after durable CAS
                continue
        return cancelled

    def _apply_reconcile(
        self,
        run_id: str,
        *,
        expected_revision: int,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        proposal = arguments.get("proposal")
        if not isinstance(proposal, Mapping):
            raise HubV2Error(
                "invalid_request",
                "The reviewed reconciliation proposal is required.",
                scope="run",
            )
        normalized, texts = validate_reconciliation_resolutions(
            arguments.get("resolutions"),
            run_disposition=arguments.get("run_disposition"),
        )
        submitted = {
            (str(item["step_id"]), str(item["verdict"]), str(item.get("result_sha256") or ""))
            for item in normalized["resolutions"]
        }
        reviewed = {
            (str(item["step_id"]), str(item["verdict"]), str(item.get("result_sha256") or ""))
            for item in proposal.get("resolutions") or []
        }
        if submitted != reviewed or normalized["run_disposition"] != proposal.get(
            "run_disposition"
        ):
            raise HubV2Error(
                "proposal_digest_conflict",
                "The reconciliation arguments do not match the reviewed proposal.",
                scope="run",
            )
        # Cheap pre-checks before encrypting anything, so a doomed apply does not
        # leave orphan artifacts behind.
        run = self.store.get_run(run_id)
        if run["status"] != "outcome_unknown" or run["revision"] != expected_revision:
            raise HubV2Error(
                "run_not_reconcilable",
                "Only an unclaimed outcome_unknown run at the expected revision can be applied.",
                scope="run",
                safe_details={"status": str(run["status"])},
            )
        plan = self.store.get_plan(run_id)
        plan_steps = {str(step["id"]): step for step in plan["steps"]}
        recovered: dict[str, str] = {}
        for step_id in sorted(texts):
            plan_step = plan_steps.get(step_id)
            if plan_step is None:
                raise HubV2Error(
                    "step_not_found",
                    "A reconciled step does not exist in this plan.",
                    scope="run",
                    safe_details={"step_id": step_id},
                )
            # Human-supplied text still has to satisfy the verifier the plan declared.
            verification = verify_output(texts[step_id], plan_step.get("verifier"))
            encrypted = self.cipher.encrypt(
                texts[step_id].encode("utf-8"),
                aad=f"agent-hub-v2-step:{step_id}".encode("utf-8"),
            )
            artifact = self.store.put_artifact(
                content=encrypted["payload"],
                media_type="text/plain; charset=utf-8",
                sensitivity="project",
                encrypted=True,
                run_id=run_id,
                producer_step_id=step_id,
                source_refs=self._step_source_refs(plan, run, plan_step),
                verification={**verification, "source": "human_reconciliation"},
                retention=plan["task"]["retention"],
                delete_after=(
                    time.time() + 86400.0 if plan["task"]["retention"] == "ephemeral" else None
                ),
                content_sha256=str(encrypted["content_sha256"]),
            )
            recovered[step_id] = str(artifact["artifact_id"])
        return self.store.apply_run_reconciliation(
            run_id,
            expected_revision=expected_revision,
            proposal=proposal,
            proposal_sha256=str(arguments.get("proposal_sha256") or ""),
            confirmation_phrase=str(arguments.get("confirmation_phrase") or ""),
            recovered_artifacts=recovered,
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
        target = str(arguments.get("target") or "policy")
        if target not in {"policy", "routing_prior"}:
            raise HubV2Error(
                "invalid_request",
                "policy target is not supported.",
                scope="policy",
            )
        if target == "routing_prior":
            # The prior is user-global, so project_root is validated but not used
            # as part of its identity.
            canonical_project_root(project_root)
            return self._routing_prior_action(action, arguments)
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

    @staticmethod
    def _routing_prior_action(action: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if action == "get":
            return load_routing_prior().public()
        if action == "prepare_update":
            patch = arguments.get("patch")
            if not isinstance(patch, Mapping):
                raise HubV2Error(
                    "invalid_request",
                    "routing prior patch must be an object.",
                    scope="routing",
                )
            return prepare_routing_prior_update(
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
                    "routing prior proposal must be an object.",
                    scope="routing",
                )
            return apply_routing_prior_update(
                proposal=proposal,
                proposal_sha256=str(arguments.get("proposal_sha256") or ""),
            ).public()
        raise HubV2Error(
            "invalid_request",
            "policy action is not supported.",
            scope="policy",
        )

    def _record_handoff_snapshot(
        self,
        applied: Mapping[str, Any],
        content: str,
    ) -> dict[str, Any]:
        """Persist the applied packet. A store failure must not undo a written file."""

        try:
            body = handoff_state.managed_body(content) or ""
            redacted, matches = redact_secret_lines(body)
            sections = handoff_state.section_digests(redacted)
            recorded = self.store.record_handoff_snapshot(
                scope_identity=handoff_state.scope_identity(applied["scope_root"]),
                scope_root=str(applied["scope_root"]),
                target_alias=str(applied["target_alias"]),
                project_root=str(applied["project_root"]),
                file_sha256=str(applied["sha256"]),
                managed_sha256=str(applied["managed_sha256"]),
                body=redacted,
                sections=sections,
                next_step=handoff_state.managed_sections(redacted).get("next_step", ""),
                previous_file_sha256=applied.get("previous_sha256"),
                previous_managed_sha256=applied.get("previous_managed_sha256"),
                redacted_lines=matches,
                created=bool(applied.get("created")),
            )
        except Exception:  # noqa: BLE001 - the file is already written; never re-raise
            return {"recorded": False}
        return {"recorded": True, **recorded}

    def _handoff_diff(self, project_root: str, extra: Mapping[str, Any]) -> dict[str, Any]:
        located = handoff_state.load_managed_block(
            project_root,
            file=str(extra.get("file") or ""),
            search=str(extra.get("search") or "project-only"),
        )
        identity = handoff_state.scope_identity(located["scope_root"])
        alias = located["target_alias"]

        def _snapshot(sequence: Any) -> dict[str, Any]:
            found = self.store.handoff_snapshot(
                scope_identity=identity,
                target_alias=alias,
                sequence=None if sequence is None else int(sequence),
            )
            if found is None:
                raise HubV2Error(
                    "handoff_snapshot_not_found",
                    "The requested handoff snapshot does not exist.",
                    scope="handoff",
                    safe_details={"sequence": sequence if sequence is not None else "latest"},
                )
            return found

        base = _snapshot(extra.get("base_sequence"))
        target_sequence = extra.get("target_sequence")
        if target_sequence is None:
            # Comparing a snapshot against the live file is how out-of-band edits
            # by another harness become visible.
            after_body = located["body"]
            after_ref: dict[str, Any] = {
                "kind": "working_file",
                "managed_sha256": located["managed_sha256"],
                "has_managed_block": located["has_managed_block"],
            }
        else:
            target = _snapshot(target_sequence)
            after_body = str(target.get("body") or "")
            after_ref = {
                "kind": "snapshot",
                "sequence": target["sequence"],
                "managed_sha256": target["managed_sha256"],
            }
        return {
            "success": True,
            "text": "Handoff diff computed locally.",
            "target": located["target"],
            "before": {
                "kind": "snapshot",
                "sequence": base["sequence"],
                "managed_sha256": base["managed_sha256"],
            },
            "after": after_ref,
            **handoff_state.diff_managed_bodies(
                str(base.get("body") or ""),
                after_body,
                include_text=bool(extra.get("include_text", True)),
                max_lines=int(extra.get("max_lines") or handoff_state.DIFF_DEFAULT_LINES),
            ),
        }

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
            prepared = handoff_state.prepare_handoff_update(project_root, **kwargs)
            response = {
                "success": True,
                "text": "Handoff update prepared; no file was changed.",
                **prepared,
            }
            if extra.get("include_diff"):
                # Shows which sections this handoff changes before it is written.
                located = handoff_state.load_managed_block(
                    project_root,
                    file=str(extra.get("file") or ""),
                    search=str(extra.get("search") or "project-only"),
                )
                response["diff"] = handoff_state.diff_managed_bodies(
                    located["body"],
                    str(extra.get("body") or ""),
                    include_text=bool(extra.get("include_text", True)),
                    max_lines=int(extra.get("max_lines") or handoff_state.DIFF_DEFAULT_LINES),
                )
            return response
        if action == "apply_update":
            if "expected_sha256" not in extra:
                raise HubV2Error(
                    "invalid_request",
                    "expected_sha256 is required for a handoff update.",
                    scope="handoff",
                )
            content = str(extra.get("content") or "")
            applied = handoff_state.apply_handoff_update(
                project_root,
                file=str(extra.get("file") or ""),
                content=content,
                expected_sha256=extra.get("expected_sha256"),
            )
            return {
                "success": True,
                "text": "HANDOFF.md updated atomically.",
                **applied,
                "snapshot": self._record_handoff_snapshot(applied, content),
            }
        if action == "history":
            located = handoff_state.load_managed_block(
                project_root,
                file=str(extra.get("file") or ""),
                search=str(extra.get("search") or "project-only"),
            )
            return {
                "success": True,
                "text": "Handoff history read from the local store.",
                "target": located["target"],
                **self.store.handoff_history(
                    scope_identity=handoff_state.scope_identity(located["scope_root"]),
                    target_alias=located["target_alias"],
                    limit=int(extra.get("limit") or 20),
                    include_body=bool(extra.get("include_body", False)),
                ),
            }
        if action == "diff":
            return self._handoff_diff(project_root, extra)
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
        prior_snapshot = safe_load_routing_prior()
        prior_public = prior_snapshot.public()
        if prior_snapshot.state == "invalid":
            prior_status = "fail"
        elif prior_snapshot.stale or prior_public["active_entry_count"] == 0:
            prior_status = "warn"
        else:
            prior_status = "pass"
        repair = str(arguments.get("repair") or "none")
        repair_plan = []
        if repair == "prepare":
            if prior_status != "pass":
                repair_plan.append(
                    {
                        "action": "review_routing_prior",
                        "automatic": False,
                        "reason_code": (
                            prior_snapshot.reason_code
                            if prior_snapshot.state == "invalid"
                            else ("prior_stale" if prior_snapshot.stale else "prior_inactive")
                        ),
                    }
                )
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
            "routing_prior": {**prior_public, "status": prior_status},
            "repair_plan": repair_plan,
            "read_only": True,
        }


def definitions() -> list[dict[str, Any]]:
    return tool_definitions()
