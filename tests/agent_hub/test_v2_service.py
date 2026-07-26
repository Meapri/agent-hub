from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time

import pytest

from agent_hub.v2.contracts import PLAN_SCHEMA, TASK_SCHEMA, validate_plan
from agent_hub.v2.crypto import ArtifactCipher, StaticKeyProvider
from agent_hub.v2.dependency_context import (
    DependencyContextPart,
    assemble_dependency_context,
    enforce_provider_context_budget,
)
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.service import HubService
from agent_hub.v2.store import HubStore


class _FakeWorker:
    invoke_delay = 0.0

    def __init__(self, provider):
        self.provider = provider

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "status":
            return {
                "success": True,
                "data": {"providers": {self.provider: {"ready": True}}},
            }
        if method == "catalog":
            return {"success": True, "warnings": [], "data": {"models": {}}}
        if method == "invoke":
            if self.invoke_delay:
                time.sleep(self.invoke_delay)
            return {
                "success": True,
                "text": f"completed by {self.provider}",
                "model": f"{self.provider}-fixture",
                "usage": {"total_tokens": 10},
            }
        raise AssertionError(method)

    def cancel(self):
        return True


class _RefreshableGeminiWorker(_FakeWorker):
    refreshed = False
    invocations = 0

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "status" and self.provider == "gemini":
            return {
                "success": True,
                "data": {
                    "providers": {
                        "gemini": {
                            "consent": True,
                            "configured": True,
                            "ready": self.__class__.refreshed,
                            "logged_in": True,
                            "auth_ready": self.__class__.refreshed,
                            "refreshable": not self.__class__.refreshed,
                            "auto_refresh_on_invoke": not self.__class__.refreshed,
                            "invocation_ready": True,
                        }
                    }
                },
            }
        if method == "invoke" and self.provider == "gemini":
            self.__class__.invocations += 1
            self.__class__.refreshed = True
        return super().request(
            method,
            params=params,
            timeout=timeout,
            request_id=request_id,
        )


class _PlannerWorker(_FakeWorker):
    last_prompt = ""
    last_params = {}

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method != "plan":
            return super().request(method, params=params, timeout=timeout)
        self.__class__.last_prompt = str(params["planner_prompt"])
        self.__class__.last_params = dict(params)
        return {
            "success": True,
            "data": {
                "plan": {
                    "steps": [
                        {
                            "id": "inspect",
                            "capability": "inspect_codebase",
                            "instruction": "Inspect the approved sources.",
                        },
                        {
                            "id": "write",
                            "capability": "write",
                            "depends_on": ["inspect"],
                            "instruction": "Draft the document.",
                            "fallback_providers": ["claude", "gemini"],
                        },
                        {
                            "id": "review",
                            "capability": "review_text",
                            "depends_on": ["write"],
                            "instruction": "Review the draft.",
                        },
                    ]
                }
            },
        }


class _FallbackWorker(_FakeWorker):
    invoked: list[tuple[str, str | None]] = []

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "status":
            return {
                "success": True,
                "data": {
                    "providers": {
                        self.provider: {
                            "ready": True,
                            "default_model": f"{self.provider}-default",
                        }
                    }
                },
            }
        if method != "invoke":
            return super().request(method, params=params, timeout=timeout)
        self.__class__.invoked.append((self.provider, params.get("model")))
        if self.provider == "gpt":
            raise HubV2Error(
                "provider_quota_exhausted",
                "The fixture primary is unavailable.",
                scope="provider",
                retryable=True,
            )
        return super().request(method, params=params, timeout=timeout)


class _ArtifactPlannerWorker(_FakeWorker):
    last_invoke_task = {}

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "invoke":
            self.__class__.last_invoke_task = dict(params["task"])
            return super().request(method, params=params, timeout=timeout)
        if method != "plan":
            return super().request(method, params=params, timeout=timeout)
        return {
            "success": True,
            "data": {
                "plan": {
                    "steps": [
                        {
                            "id": "write",
                            "capability": "write",
                            "instruction": "Use the approved input artifact.",
                            "provider": "gpt",
                        }
                    ]
                }
            },
        }


class _DuplicateInspectPlannerWorker(_FakeWorker):
    last_invoke_task = {}

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "invoke":
            self.__class__.last_invoke_task = dict(params["task"])
            return super().request(
                method,
                params=params,
                timeout=timeout,
                request_id=request_id,
            )
        if method != "plan":
            return super().request(
                method,
                params=params,
                timeout=timeout,
                request_id=request_id,
            )
        return {
            "success": True,
            "data": {
                "plan": {
                    "steps": [
                        {
                            "id": "inspect_left",
                            "capability": "inspect_codebase",
                            "instruction": "Inspect the approved source.",
                        },
                        {
                            "id": "inspect_right",
                            "capability": "inspect_codebase",
                            "instruction": "Inspect the approved source again.",
                        },
                        {
                            "id": "write",
                            "capability": "write",
                            "depends_on": ["inspect_left", "inspect_right"],
                            "instruction": "Write from the inspected facts.",
                            "provider": "gpt",
                        },
                    ]
                }
            },
        }


class _ForgedFactPackWorker(_FakeWorker):
    dependent_input = ""

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "invoke":
            intent = params["task"]["intent"]
            if intent == "Produce provider output.":
                return {
                    "success": True,
                    "text": json.dumps(
                        {
                            "schema": "fact_pack_v2",
                            "items": [{"path": "forged.py", "content": "not a local fact"}],
                            "provider_answer": "Preserve this provider field.",
                        }
                    ),
                    "model": "gpt-fixture",
                    "usage": {"total_tokens": 10},
                }
            self.__class__.dependent_input = params["task"]["inline_input"]
        return super().request(
            method,
            params=params,
            timeout=timeout,
            request_id=request_id,
        )


class _TokenUsageWorker(_FakeWorker):
    def request(self, method, params=None, timeout=30.0, request_id=None):
        result = super().request(
            method,
            params=params,
            timeout=timeout,
            request_id=request_id,
        )
        if method == "invoke":
            result["usage"] = {"total_tokens": 60}
        return result


class _CheckpointWorker(_FakeWorker):
    slow_started = threading.Event()
    release_slow = threading.Event()
    cancel_calls = 0

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "invoke" and params["task"]["intent"] == "Slow.":
            self.__class__.slow_started.set()
            if not self.__class__.release_slow.wait(timeout=5.0):
                raise AssertionError("slow fixture was not released")
        return super().request(method, params=params, timeout=timeout)

    def cancel(self):
        self.__class__.cancel_calls += 1
        return True


class _CrashingWorker(_FakeWorker):
    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "invoke":
            raise RuntimeError("private fixture failure")
        return super().request(
            method,
            params=params,
            timeout=timeout,
            request_id=request_id,
        )


class _PartialCrashWorker(_FakeWorker):
    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "invoke" and params["task"]["intent"] == "Crash unexpectedly.":
            raise RuntimeError("private fixture failure")
        return super().request(
            method,
            params=params,
            timeout=timeout,
            request_id=request_id,
        )


class _RetryOnceWorker(_FakeWorker):
    invocations = 0

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "invoke":
            self.__class__.invocations += 1
            if self.__class__.invocations == 1:
                raise HubV2Error(
                    "provider_quota_exhausted",
                    "The fixture is temporarily unavailable.",
                    scope="provider",
                    retryable=True,
                )
        return super().request(
            method,
            params=params,
            timeout=timeout,
            request_id=request_id,
        )


class _CatalogLimitWorker(_FakeWorker):
    invocations = 0

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "status":
            return {
                "success": True,
                "data": {
                    "providers": {
                        self.provider: {
                            "ready": True,
                            "default_model": f"{self.provider}-live",
                        }
                    }
                },
            }
        if method == "catalog":
            return {
                "success": True,
                "data": {
                    "models": {
                        self.provider: {
                            "success": True,
                            "source": "live",
                            "models": [
                                {
                                    "id": f"{self.provider}-live",
                                    "max_input_tokens": 10,
                                }
                            ],
                        }
                    }
                },
            }
        if method == "invoke":
            self.__class__.invocations += 1
        return super().request(
            method,
            params=params,
            timeout=timeout,
            request_id=request_id,
        )


def _service(tmp_path):
    return HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_FakeWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )


def _input_artifact(service: HubService, content: str) -> str:
    encrypted = service.cipher.encrypt(
        content.encode("utf-8"),
        aad=b"agent-hub-v2-task-input",
    )
    artifact = service.store.put_artifact(
        content=encrypted["payload"],
        media_type="text/plain; charset=utf-8",
        sensitivity="project",
        encrypted=True,
        content_sha256=encrypted["content_sha256"],
    )
    return artifact["artifact_id"]


def _approve_review(service: HubService, proposal: dict) -> str:
    review_id = proposal["approval_request"]["review_id"]
    approved = service.store.decide_egress_review(review_id, decision="approve")
    assert approved["status"] == "approved"
    return review_id


def _plan():
    return validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Complete the fixture.",
                "capability": "chat",
                "inline_input": "private run input",
                "constraints": {"provider_allowlist": ["gpt"]},
                "retention": "durable_private",
            },
            "steps": [
                {
                    "id": "answer",
                    "capability": "chat",
                    "instruction": "Return the fixture result.",
                    "routing_requirements": {"planner_provider": "gpt"},
                }
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )


def test_artifact_cipher_authenticates_digest_and_aad():
    cipher = ArtifactCipher(StaticKeyProvider(b"a" * 32))
    encrypted = cipher.encrypt(b"private", aad=b"fixture")

    assert (
        cipher.decrypt(
            encrypted["payload"],
            aad=b"fixture",
            expected_sha256=encrypted["content_sha256"],
        )
        == b"private"
    )
    tampered = bytearray(encrypted["payload"])
    tampered[-1] ^= 1
    with pytest.raises(HubV2Error) as payload_error:
        cipher.decrypt(
            bytes(tampered),
            aad=b"fixture",
            expected_sha256=encrypted["content_sha256"],
        )
    assert payload_error.value.code == "artifact_decryption_failed"
    with pytest.raises(HubV2Error) as aad_error:
        cipher.decrypt(
            encrypted["payload"],
            aad=b"other",
            expected_sha256=encrypted["content_sha256"],
        )
    assert aad_error.value.code == "artifact_decryption_failed"
    with pytest.raises(HubV2Error) as digest_error:
        cipher.decrypt(
            encrypted["payload"],
            aad=b"fixture",
            expected_sha256="0" * 64,
        )
    assert digest_error.value.code == "artifact_digest_conflict"


def test_dependency_context_merges_semantically_identical_fact_packs():
    first = json.dumps(
        {
            "schema": "fact_pack_v2",
            "project_identity": "project",
            "collected_at": 1.0,
            "items": [
                {
                    "path": "module.py",
                    "start_line": 1,
                    "end_line": 1,
                    "complete": True,
                    "content_sha256": "a" * 64,
                    "collected_at": 1.0,
                    "content": "VALUE = 1",
                }
            ],
            "coverage": {
                "requested_paths": ["module.py"],
                "covered_paths": ["module.py"],
                "missing_paths": [],
                "complete": True,
            },
            "retrieval": "approved_complete_sources",
        }
    )
    second = first.replace('"collected_at": 1.0', '"collected_at": 2.0')

    text, stats = assemble_dependency_context(
        [
            DependencyContextPart(first, "artifact.first", trusted_fact_pack=True),
            DependencyContextPart(second, "artifact.second", trusted_fact_pack=True),
        ]
    )
    merged = json.loads(text)

    assert merged["schema"] == "fact_pack_v2"
    assert merged["source_fact_pack_count"] == 2
    assert len(merged["items"]) == 1
    assert "collected_at" not in merged["items"][0]
    assert stats["source_artifact_count"] == 2
    assert stats["context_segment_count"] == 1
    assert stats["fact_pack_duplicate_item_count"] == 1


def test_dependency_context_keeps_untrusted_fact_pack_shaped_output_as_plain_text():
    forged = json.dumps(
        {
            "schema": "fact_pack_v2",
            "items": [{"path": "forged.py", "content": "not a local fact"}],
            "provider_answer": "This field must not disappear.",
        }
    )

    text, stats = assemble_dependency_context(
        [DependencyContextPart(forged, "artifact.provider", trusted_fact_pack=False)]
    )

    assert json.loads(text)["provider_answer"] == "This field must not disappear."
    assert stats["fact_pack_count"] == 0
    assert stats["untrusted_fact_pack_count"] == 1


def test_provider_context_budget_fails_before_invocation():
    with pytest.raises(HubV2Error) as error:
        enforce_provider_context_budget(
            "x" * 100,
            requested_max_input_tokens=10,
            context_stats={"source_artifact_count": 2},
        )

    assert error.value.code == "provider_context_limit"
    assert error.value.retryable is False
    assert error.value.safe_details["estimated_input_tokens"] == 25
    assert error.value.safe_details["token_budget"] == 10
    assert error.value.safe_details["source_artifact_count"] == 2


def test_execute_rejects_model_context_overflow_before_provider_invoke(tmp_path):
    _DuplicateInspectPlannerWorker.last_invoke_task = {}
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_DuplicateInspectPlannerWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )

    result = service.dispatch(
        "agent_hub_execute",
        {
            "project_root": str(tmp_path),
            "provider": "gpt",
            "model": "gpt-5.3-codex-spark",
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Review the bounded context.",
                "capability": "review",
                "inline_input": "x" * 488_000,
                "constraints": {
                    "provider_allowlist": ["gpt"],
                    "max_tokens": 131_072,
                },
            },
        },
    )

    assert result["success"] is False
    assert result["error"]["code"] == "provider_context_limit"
    assert result["error"]["safe_details"]["estimated_input_tokens"] == 122_000
    assert result["error"]["safe_details"]["largest_candidate_max_input_tokens"] == 121_600
    assert _DuplicateInspectPlannerWorker.last_invoke_task == {}


def test_execute_does_not_use_output_budget_as_input_context_limit(tmp_path):
    _DuplicateInspectPlannerWorker.last_invoke_task = {}
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_DuplicateInspectPlannerWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )

    result = service.dispatch(
        "agent_hub_execute",
        {
            "project_root": str(tmp_path),
            "provider": "gpt",
            "model": "gpt-5.3-codex-spark",
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Review the bounded context.",
                "capability": "review",
                "inline_input": "x" * 20_000,
                "constraints": {
                    "provider_allowlist": ["gpt"],
                    "max_output_tokens": 4_096,
                    "max_total_tokens": 8_192,
                },
            },
        },
    )

    assert result["success"] is True
    assert (
        _DuplicateInspectPlannerWorker.last_invoke_task["constraints"]["max_output_tokens"] == 4_096
    )


def test_execute_uses_compact_v2_envelope(tmp_path):
    service = _service(tmp_path)

    result = service.dispatch(
        "agent_hub_execute",
        {
            "provider": "gpt",
            "project_root": str(tmp_path),
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Answer.",
                "capability": "chat",
                "inline_input": "fixture",
            },
        },
    )

    assert result["success"] is True
    assert result["data"]["provider"] == "gpt"
    assert result["data"]["result"]["text"] == "completed by gpt"


def test_public_run_management_tools_are_covered_through_dispatch(tmp_path):
    service = _service(tmp_path)
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": _plan(),
            "project_root": str(tmp_path),
            "idempotency_key": f"public-dispatch.{secrets.token_hex(4)}",
        },
    )
    run_id = started["data"]["run_id"]

    fetched = service.dispatch(
        "agent_hub_get",
        {"run_id": run_id, "project_root": str(tmp_path)},
    )
    events = service.dispatch(
        "agent_hub_events",
        {"run_id": run_id, "after_cursor": 0, "limit": 10},
    )
    cancelled = service.dispatch(
        "agent_hub_cancel",
        {"run_id": run_id, "expected_revision": fetched["data"]["revision"]},
    )
    feedback = service.dispatch(
        "agent_hub_feedback",
        {
            "run_id": run_id,
            "expected_revision": cancelled["data"]["revision"],
            "outcome": "rejected",
            "rating": 1,
        },
    )
    policy = service.dispatch(
        "agent_hub_policy",
        {"action": "get", "project_root": str(tmp_path)},
    )
    doctor = service.dispatch(
        "agent_hub_doctor",
        {"project_root": str(tmp_path), "live": False, "repair": "none"},
    )

    assert fetched["success"] is True
    assert fetched["data"]["schema"] == "run_v3"
    assert events["success"] is True
    assert events["data"]["events"]
    assert cancelled["success"] is True
    assert cancelled["data"]["status"] == "cancelled"
    assert feedback["success"] is True
    assert feedback["data"]["outcome"] == "rejected"
    assert policy["success"] is True
    assert policy["data"]["schema"] == "agent_hub_policy_snapshot_v2"
    assert doctor["success"] is True
    assert doctor["data"]["schema"] == "agent_hub_doctor_v2"
    assert doctor["data"]["read_only"] is True


def test_execute_allows_gemini_to_refresh_expired_access_token_on_invoke(tmp_path):
    _RefreshableGeminiWorker.refreshed = False
    _RefreshableGeminiWorker.invocations = 0
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_RefreshableGeminiWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )

    before = service.dispatch("agent_hub_status", {})
    gemini = before["data"]["providers"]["gemini"]
    assert gemini["ready"] is False
    assert gemini["invocation_ready"] is True
    assert gemini["state"]["refreshable"] is True

    result = service.dispatch(
        "agent_hub_execute",
        {
            "provider": "gemini",
            "project_root": str(tmp_path),
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Answer after refreshing the session.",
                "capability": "chat",
                "inline_input": "fixture",
                "constraints": {"provider_allowlist": ["gemini"]},
            },
        },
    )

    assert result["success"] is True
    assert result["data"]["provider"] == "gemini"
    assert _RefreshableGeminiWorker.invocations == 1
    after = service.dispatch("agent_hub_status", {})
    assert after["data"]["providers"]["gemini"]["ready"] is True


def test_execute_rejects_stored_artifact_without_egress_approval(tmp_path):
    service = _service(tmp_path)
    artifact_id = _input_artifact(service, "stored project content")

    result = service.dispatch(
        "agent_hub_execute",
        {
            "provider": "gpt",
            "project_root": str(tmp_path),
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Answer from the artifact.",
                "capability": "chat",
                "input_artifacts": [artifact_id],
            },
        },
    )

    assert result["success"] is False
    assert result["error"]["code"] == "egress_approval_required"


def test_plan_approval_covers_stored_artifact_egress(tmp_path):
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_ArtifactPlannerWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    artifact_id = _input_artifact(
        service,
        "safe input\napi_key = '123456789-secret-value'\n",
    )
    task = {
        "schema": TASK_SCHEMA,
        "intent": "Write from the approved artifact.",
        "capability": "write",
        "input_artifacts": [artifact_id],
        "constraints": {"provider_allowlist": ["gpt"]},
        "retention": "durable_private",
    }
    prepared = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "prepare",
            "task": task,
            "project_root": str(tmp_path),
            "provider": "gpt",
            "source_paths": [],
        },
    )
    proposal = prepared["data"]
    assert proposal["manifest"]["entries"][0]["artifact_id"] == artifact_id
    assert "secret-value" not in proposal["fact_pack"]["items"][0]["content"]
    applied = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "apply",
            "task": task,
            "project_root": str(tmp_path),
            "provider": "gpt",
            "proposal": proposal,
            "proposal_sha256": proposal["proposal_sha256"],
            "expected_policy_revision": 0,
            "approval_request_id": _approve_review(service, proposal),
        },
    )

    assert applied["success"] is True
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": applied["data"]["plan"],
            "project_root": str(tmp_path),
            "idempotency_key": f"artifact.{secrets.token_hex(4)}",
        },
    )
    assert started["success"] is True
    continued = service.dispatch(
        "agent_hub_continue",
        {
            "run_id": started["data"]["run_id"],
            "expected_revision": 0,
        },
    )
    assert continued["success"] is True
    for _ in range(100):
        current = service.store.get_run(started["data"]["run_id"])
        if current["status"] != "running":
            break
        time.sleep(0.01)
    assert current["status"] == "completed"
    sent = _ArtifactPlannerWorker.last_invoke_task["inline_input"]
    assert "safe input" in sent
    assert "secret-value" not in sent
    assert "[REDACTED SECRET CANDIDATE]" in sent


def test_status_exposes_only_content_free_operation_metrics(tmp_path):
    service = _service(tmp_path)
    service.dispatch(
        "agent_hub_execute",
        {
            "provider": "gpt",
            "project_root": str(tmp_path),
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Answer.",
                "capability": "chat",
                "inline_input": "fixture",
            },
        },
    )

    status = service.dispatch("agent_hub_status", {})

    metrics = status["data"]["observability"]
    assert metrics["content_recorded"] is False
    assert metrics["operations"]["agent_hub_execute"]["count"] == 1
    assert "fixture" not in str(metrics)


def test_start_seals_inline_input_and_continue_completes_in_background(tmp_path):
    service = _service(tmp_path)
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": _plan(),
            "project_root": str(tmp_path),
            "idempotency_key": f"fixture.{secrets.token_hex(4)}",
        },
    )

    assert started["success"] is True
    run_id = started["data"]["run_id"]
    stored_plan = service.store.get_plan(run_id)
    assert stored_plan["task"]["inline_input"] == ""
    assert len(stored_plan["task"]["input_artifacts"]) == 1
    assert "private run input" not in str(stored_plan)

    receipt = service.dispatch(
        "agent_hub_continue",
        {"run_id": run_id, "expected_revision": 0},
    )
    assert receipt["success"] is True
    assert receipt["data"]["status"] == "running"
    assert receipt["data"]["lease_expires_at"] > time.time() + 1700

    for _ in range(100):
        current = service.store.get_run(run_id)
        if current["status"] in {"completed", "paused", "outcome_unknown"}:
            break
        time.sleep(0.01)
    assert current["status"] == "completed"
    step = current["steps"][0]
    assert step["provider"] == "gpt"
    artifact = service.dispatch(
        "agent_hub_artifact",
        {
            "action": "get",
            "artifact_id": step["output_artifact_ids"][0],
            "include_text": True,
        },
    )
    assert artifact["data"]["text"] == "completed by gpt"

    prepared = service.dispatch(
        "agent_hub_artifact",
        {
            "action": "prepare_export",
            "artifact_id": step["output_artifact_ids"][0],
            "project_root": str(tmp_path),
            "destination": "exports/result.txt",
        },
    )
    assert prepared["success"] is True
    proposal = prepared["data"]
    exported = service.dispatch(
        "agent_hub_artifact",
        {
            "action": "apply_export",
            "artifact_id": step["output_artifact_ids"][0],
            "proposal": proposal,
            "proposal_sha256": proposal["proposal_sha256"],
        },
    )
    assert exported["success"] is True
    assert (tmp_path / "exports/result.txt").read_text() == "completed by gpt"
    stored_artifact = service.store.get_artifact(step["output_artifact_ids"][0])
    assert stored_artifact["export_count"] == 1
    assert stored_artifact["provenance"]["sources"]


def test_plan_prepare_reads_sources_without_provider_call(tmp_path):
    service = _service(tmp_path)
    (tmp_path / "fact.txt").write_text("safe fact")

    result = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "prepare",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "source_paths": ["fact.txt"],
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Plan.",
                "capability": "chat",
                "inline_input": "",
            },
        },
    )

    assert result["success"] is True
    assert result["data"]["approval_required"] is True
    assert result["data"]["manifest"]["entries"][0]["path_alias"] == "fact.txt"
    assert result["data"]["manifest"]["destinations"] == [
        "gpt",
        "claude",
        "grok",
        "gemini",
    ]


def test_plan_apply_requires_one_time_local_gui_approval_before_provider_call(tmp_path):
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_PlannerWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    (tmp_path / "fact.txt").write_text("safe fact\n")
    task = {
        "schema": TASK_SCHEMA,
        "intent": "Plan from the reviewed source.",
        "capability": "write",
        "inline_input": "",
    }
    prepared = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "prepare",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "source_paths": ["fact.txt"],
            "task": task,
        },
    )["data"]
    _PlannerWorker.last_prompt = ""
    arguments = {
        "mode": "apply",
        "project_root": str(tmp_path),
        "provider": "gpt",
        "task": task,
        "proposal": prepared,
        "proposal_sha256": prepared["proposal_sha256"],
        "expected_policy_revision": 0,
    }

    unapproved = service.dispatch("agent_hub_plan", arguments)

    assert unapproved["success"] is False
    assert unapproved["error"]["code"] == "egress_human_approval_required"
    assert unapproved["error"]["next_action"] == {
        "type": "local_gui",
        "command": "agent-hub-connect",
    }
    assert _PlannerWorker.last_prompt == ""

    review_id = _approve_review(service, prepared)
    approved = service.dispatch(
        "agent_hub_plan",
        {**arguments, "approval_request_id": review_id},
    )
    repeated = service.dispatch(
        "agent_hub_plan",
        {**arguments, "approval_request_id": review_id},
    )

    assert approved["success"] is True
    assert repeated["success"] is False
    assert repeated["error"]["code"] == "egress_review_already_consumed"


def test_global_auto_approve_skips_individual_click_but_remains_one_time(tmp_path):
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_PlannerWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    service.update_egress_settings(auto_approve=True, expected_revision=0)
    (tmp_path / "fact.txt").write_text("safe fact\n")
    task = {
        "schema": TASK_SCHEMA,
        "intent": "Plan from the automatically approved source.",
        "capability": "write",
        "inline_input": "",
    }

    prepared = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "prepare",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "source_paths": ["fact.txt"],
            "task": task,
        },
    )["data"]
    review = prepared["approval_request"]
    arguments = {
        "mode": "apply",
        "project_root": str(tmp_path),
        "provider": "gpt",
        "task": task,
        "proposal": prepared,
        "proposal_sha256": prepared["proposal_sha256"],
        "expected_policy_revision": 0,
        "approval_request_id": review["review_id"],
    }

    applied = service.dispatch("agent_hub_plan", arguments)
    repeated = service.dispatch("agent_hub_plan", arguments)

    assert prepared["approval_required"] is False
    assert prepared["approval_mode"] == "automatic"
    assert review["status"] == "approved"
    assert review["decision_source"] == "global_auto_approve"
    assert review["decision_settings_revision"] == 1
    assert prepared["next_action"]["approval_request_id"] == review["review_id"]
    assert applied["success"] is True
    assert repeated["success"] is False
    assert repeated["error"]["code"] == "egress_review_already_consumed"


def test_global_auto_approve_does_not_override_project_egress_denial(tmp_path):
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_PlannerWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    service.update_egress_settings(auto_approve=True, expected_revision=0)
    policy_dir = tmp_path / ".agent-hub"
    policy_dir.mkdir()
    (policy_dir / "project.toml").write_text(
        '[egress]\nrepository_content = "denied"\n',
    )
    (tmp_path / "fact.txt").write_text("safe fact\n")
    _PlannerWorker.last_prompt = ""

    result = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "prepare",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "source_paths": ["fact.txt"],
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Do not send this source.",
                "capability": "write",
                "inline_input": "",
            },
        },
    )

    assert result["success"] is False
    assert result["error"]["code"] == "egress_policy_denied"
    assert service.pending_egress_reviews()["reviews"] == []
    assert _PlannerWorker.last_prompt == ""


def test_plan_apply_sends_bounded_manifest_without_source_contents(tmp_path):
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_PlannerWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    sentinel = "PRIVATE_SOURCE_SENTINEL_" + ("x" * 100_000)
    (tmp_path / "fact.txt").write_text(sentinel)
    task = {
        "schema": TASK_SCHEMA,
        "intent": "Plan a document rewrite.",
        "capability": "write",
        "inline_input": "",
    }
    prepared = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "prepare",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "source_paths": ["fact.txt"],
            "task": task,
        },
    )
    proposal = prepared["data"]

    applied = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "apply",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "task": task,
            "proposal": proposal,
            "proposal_sha256": proposal["proposal_sha256"],
            "expected_policy_revision": 0,
            "approval_request_id": _approve_review(service, proposal),
        },
    )

    assert applied["success"] is True
    assert sentinel not in _PlannerWorker.last_prompt
    assert "project_root" not in _PlannerWorker.last_params
    assert "fact.txt" in _PlannerWorker.last_prompt
    assert proposal["manifest"]["manifest_sha256"] in _PlannerWorker.last_prompt
    assert len(_PlannerWorker.last_prompt) < 33_000
    assert [step["capability"] for step in applied["data"]["plan"]["steps"]] == [
        "inspect",
        "write",
        "review",
    ]
    assert applied["data"]["plan"]["steps"][0]["output_contract"] == {
        "type": "fact_pack_v2",
        "source_paths": ["fact.txt"],
        "require_complete": True,
    }
    assert applied["data"]["plan"]["steps"][1]["routing_requirements"]["fallbacks"] == [
        "claude",
        "gemini",
    ]


def test_plan_apply_rejects_provider_outside_approved_destinations(tmp_path):
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_PlannerWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    (tmp_path / "fact.txt").write_text("safe fact")
    task = {
        "schema": TASK_SCHEMA,
        "intent": "Use GPT only.",
        "capability": "write",
        "inline_input": "",
        "constraints": {"provider_allowlist": ["gpt"]},
    }
    prepared = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "prepare",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "source_paths": ["fact.txt"],
            "task": task,
        },
    )["data"]

    applied = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "apply",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "task": task,
            "proposal": prepared,
            "proposal_sha256": prepared["proposal_sha256"],
            "expected_policy_revision": 0,
            "approval_request_id": _approve_review(service, prepared),
        },
    )

    assert prepared["manifest"]["destinations"] == ["gpt"]
    assert applied["success"] is False
    assert applied["error"]["code"] == "planner_egress_violation"


def test_local_inspect_uses_fts_without_provider_call(tmp_path):
    service = _service(tmp_path)
    (tmp_path / "module.py").write_text("def checkpoint_recovery():\n    return True\n")

    result = service.dispatch(
        "agent_hub_execute",
        {
            "project_root": str(tmp_path),
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Find checkpoint recovery.",
                "capability": "inspect",
                "inline_input": "checkpoint recovery",
            },
        },
    )

    assert result["success"] is True
    assert result["data"]["provider"] == "local"
    assert result["data"]["result"]["fact_pack"]["items"][0]["path"] == "module.py"
    assert result["data"]["result"]["fact_pack"]["items"][0]["start_line"] == 1
    assert result["data"]["result"]["fact_pack"]["items"][0]["complete"] is True


def test_start_rejects_unapproved_inspect_scope_feeding_provider(tmp_path):
    service = _service(tmp_path)
    (tmp_path / "module.py").write_text("safe")
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Inspect then write.",
                "capability": "write",
                "inline_input": "",
                "constraints": {"provider_allowlist": ["gpt"]},
            },
            "steps": [
                {
                    "id": "inspect",
                    "capability": "inspect",
                    "instruction": "Inspect module.py.",
                    "output_contract": {
                        "type": "fact_pack_v2",
                        "source_paths": ["module.py"],
                        "require_complete": True,
                    },
                },
                {
                    "id": "write",
                    "capability": "write",
                    "depends_on": ["inspect"],
                    "instruction": "Write.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )

    result = service.dispatch(
        "agent_hub_start",
        {
            "plan": plan,
            "project_root": str(tmp_path),
            "idempotency_key": f"unapproved.{secrets.token_hex(4)}",
        },
    )

    assert result["success"] is False
    assert result["error"]["code"] == "egress_approval_required"


def test_approved_plan_executes_complete_scoped_inspection(tmp_path):
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_PlannerWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    (tmp_path / "fact.txt").write_text("safe fact\n")
    task = {
        "schema": TASK_SCHEMA,
        "intent": "Inspect and write.",
        "capability": "write",
        "inline_input": "",
    }
    prepared = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "prepare",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "source_paths": ["fact.txt"],
            "task": task,
        },
    )["data"]
    applied = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "apply",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "task": task,
            "proposal": prepared,
            "proposal_sha256": prepared["proposal_sha256"],
            "expected_policy_revision": 0,
            "approval_request_id": _approve_review(service, prepared),
        },
    )["data"]["plan"]
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": applied,
            "project_root": str(tmp_path),
            "idempotency_key": f"approved.{secrets.token_hex(4)}",
        },
    )
    service.dispatch(
        "agent_hub_continue",
        {
            "run_id": started["data"]["run_id"],
            "expected_revision": 0,
            "max_waves": 8,
        },
    )
    for _ in range(100):
        current = service.store.get_run(started["data"]["run_id"])
        if current["status"] != "running":
            break
        time.sleep(0.01)

    inspect = current["steps"][0]
    assert inspect["status"] == "completed"
    assert inspect["checkpoint"]["retrieval"] == "approved_complete_sources"
    artifact = service.dispatch(
        "agent_hub_artifact",
        {
            "action": "get",
            "artifact_id": inspect["output_artifact_ids"][0],
            "include_text": True,
        },
    )
    facts = json.loads(artifact["data"]["text"])
    assert facts["coverage"]["complete"] is True
    assert facts["items"][0]["path"] == "fact.txt"
    assert facts["items"][0]["start_line"] == 1
    write = current["steps"][1]
    review = current["steps"][2]
    assert write["input_artifact_ids"] == inspect["output_artifact_ids"]
    assert review["input_artifact_ids"] == write["output_artifact_ids"]


def test_duplicate_inspection_artifacts_are_compacted_before_provider_call(tmp_path):
    _DuplicateInspectPlannerWorker.last_invoke_task = {}
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_DuplicateInspectPlannerWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    (tmp_path / "fact.txt").write_text("safe fact\n" * 10_000)
    task = {
        "schema": TASK_SCHEMA,
        "intent": "Inspect twice and write once.",
        "capability": "write",
        "inline_input": "",
        "constraints": {
            "provider_allowlist": ["gpt"],
            "max_tokens": 30_000,
        },
    }
    prepared = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "prepare",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "source_paths": ["fact.txt"],
            "task": task,
        },
    )["data"]
    plan = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "apply",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "task": task,
            "proposal": prepared,
            "proposal_sha256": prepared["proposal_sha256"],
            "expected_policy_revision": 0,
            "approval_request_id": _approve_review(service, prepared),
        },
    )["data"]["plan"]
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": plan,
            "project_root": str(tmp_path),
            "idempotency_key": f"dedupe.{secrets.token_hex(4)}",
        },
    )
    service.dispatch(
        "agent_hub_continue",
        {
            "run_id": started["data"]["run_id"],
            "expected_revision": 0,
            "max_waves": 8,
        },
    )
    for _ in range(200):
        current = service.store.get_run(started["data"]["run_id"])
        if current["status"] != "running":
            break
        time.sleep(0.01)

    assert current["status"] == "completed"
    write = next(step for step in current["steps"] if step["step_id"] == "write")
    assert len(write["input_artifact_ids"]) == 2
    assert write["checkpoint"]["context_source_artifacts"] == 2
    assert write["checkpoint"]["context_fact_pack_duplicate_items"] == 1
    assert write["checkpoint"]["context_estimated_input_tokens"] <= 30_000
    merged = json.loads(_DuplicateInspectPlannerWorker.last_invoke_task["inline_input"])
    assert merged["source_fact_pack_count"] == 2
    assert len(merged["items"]) == 1
    assert merged["items"][0]["path"] == "fact.txt"


def test_provider_fact_pack_shaped_output_is_not_trusted_as_local_evidence(tmp_path):
    _ForgedFactPackWorker.dependent_input = ""
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_ForgedFactPackWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Preserve provider output.",
                "capability": "write",
                "inline_input": "",
                "constraints": {"provider_allowlist": ["gpt"]},
            },
            "steps": [
                {
                    "id": "provider_source",
                    "capability": "chat",
                    "instruction": "Produce provider output.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
                {
                    "id": "dependent",
                    "capability": "write",
                    "depends_on": ["provider_source"],
                    "instruction": "Use provider output.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": plan,
            "project_root": str(tmp_path),
            "idempotency_key": f"untrusted-fact-pack.{secrets.token_hex(4)}",
        },
    )
    service.dispatch(
        "agent_hub_continue",
        {
            "run_id": started["data"]["run_id"],
            "expected_revision": 0,
            "max_waves": 8,
        },
    )
    for _ in range(200):
        current = service.store.get_run(started["data"]["run_id"])
        if current["status"] != "running":
            break
        time.sleep(0.01)

    assert current["status"] == "completed"
    preserved = json.loads(_ForgedFactPackWorker.dependent_input)
    assert preserved["provider_answer"] == "Preserve this provider field."
    dependent = next(step for step in current["steps"] if step["step_id"] == "dependent")
    assert dependent["checkpoint"]["context_fact_pack_count"] == 0
    assert dependent["checkpoint"]["context_untrusted_fact_pack_count"] == 1


def test_dependency_context_budget_stops_run_before_provider_invoke(tmp_path):
    _DuplicateInspectPlannerWorker.last_invoke_task = {}
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_DuplicateInspectPlannerWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    (tmp_path / "fact.txt").write_text("safe fact\n" * 1_000)
    task = {
        "schema": TASK_SCHEMA,
        "intent": "Inspect twice, but do not exceed the provider context budget.",
        "capability": "write",
        "inline_input": "",
        "constraints": {
            "provider_allowlist": ["gpt"],
            "max_input_tokens": 100,
        },
    }
    prepared = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "prepare",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "source_paths": ["fact.txt"],
            "task": task,
        },
    )["data"]
    plan = service.dispatch(
        "agent_hub_plan",
        {
            "mode": "apply",
            "project_root": str(tmp_path),
            "provider": "gpt",
            "task": task,
            "proposal": prepared,
            "proposal_sha256": prepared["proposal_sha256"],
            "expected_policy_revision": 0,
            "approval_request_id": _approve_review(service, prepared),
        },
    )["data"]["plan"]
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": plan,
            "project_root": str(tmp_path),
            "idempotency_key": f"context-limit.{secrets.token_hex(4)}",
        },
    )
    service.dispatch(
        "agent_hub_continue",
        {
            "run_id": started["data"]["run_id"],
            "expected_revision": 0,
            "max_waves": 8,
        },
    )
    for _ in range(200):
        current = service.store.get_run(started["data"]["run_id"])
        if current["status"] != "running":
            break
        time.sleep(0.01)

    assert current["status"] == "paused"
    write = next(step for step in current["steps"] if step["step_id"] == "write")
    assert write["status"] == "failed"
    assert write["checkpoint"]["error_code"] == "provider_context_limit"
    assert _DuplicateInspectPlannerWorker.last_invoke_task == {}


def test_run_total_token_budget_is_independent_from_provider_output_limit(tmp_path):
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_TokenUsageWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Bound aggregate usage.",
                "capability": "chat",
                "inline_input": "",
                "constraints": {
                    "provider_allowlist": ["gpt"],
                    "max_output_tokens": 1_000,
                    "max_total_tokens": 100,
                },
            },
            "steps": [
                {
                    "id": "first",
                    "capability": "chat",
                    "instruction": "First.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
                {
                    "id": "second",
                    "capability": "chat",
                    "depends_on": ["first"],
                    "instruction": "Second.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": plan,
            "project_root": str(tmp_path),
            "idempotency_key": f"total-token-budget.{secrets.token_hex(4)}",
        },
    )
    run_id = started["data"]["run_id"]
    service.dispatch(
        "agent_hub_continue",
        {"run_id": run_id, "expected_revision": 0, "max_waves": 8},
    )
    for _ in range(100):
        current = service.store.get_run(run_id)
        if current["status"] != "running":
            break
        time.sleep(0.01)

    assert current["status"] == "paused"
    assert all(step["status"] == "completed" for step in current["steps"])
    events = service.store.events(run_id)["events"]
    assert events[-1]["details"]["error_code"] == "run_token_budget_exhausted"


def test_start_idempotency_does_not_create_orphan_input_artifact(tmp_path):
    service = _service(tmp_path)
    key = f"fixture.{secrets.token_hex(4)}"
    first = service.dispatch(
        "agent_hub_start",
        {"plan": _plan(), "project_root": str(tmp_path), "idempotency_key": key},
    )
    second = service.dispatch(
        "agent_hub_start",
        {"plan": _plan(), "project_root": str(tmp_path), "idempotency_key": key},
    )

    assert first["data"]["run_id"] == second["data"]["run_id"]
    assert (
        service.store.get_plan(first["data"]["run_id"])["task"]["input_artifacts"]
        == service.store.get_plan(second["data"]["run_id"])["task"]["input_artifacts"]
    )


def test_start_rejects_idempotency_key_reuse_for_different_plan(tmp_path):
    service = _service(tmp_path)
    key = f"fixture.{secrets.token_hex(4)}"
    original = _plan()
    first = service.dispatch(
        "agent_hub_start",
        {"plan": original, "project_root": str(tmp_path), "idempotency_key": key},
    )
    changed = _plan()
    changed["steps"][0]["instruction"] = "A different request."
    changed.pop("plan_sha256")
    changed = validate_plan(changed)

    second = service.dispatch(
        "agent_hub_start",
        {"plan": changed, "project_root": str(tmp_path), "idempotency_key": key},
    )

    assert first["success"] is True
    assert second["success"] is False
    assert second["error"]["code"] == "idempotency_key_conflict"
    with sqlite3.connect(service.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1


def test_independent_ready_steps_execute_in_parallel(tmp_path):
    service = _service(tmp_path)
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Parallel fixture.",
                "capability": "chat",
                "inline_input": "",
                "constraints": {
                    "provider_allowlist": ["gpt"],
                    "timeout_seconds": 0.15,
                },
            },
            "steps": [
                {
                    "id": "left",
                    "capability": "chat",
                    "instruction": "Left.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
                {
                    "id": "right",
                    "capability": "chat",
                    "instruction": "Right.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )
    _FakeWorker.invoke_delay = 0.12
    try:
        started = service.dispatch(
            "agent_hub_start",
            {
                "plan": plan,
                "project_root": str(tmp_path),
                "idempotency_key": f"parallel.{secrets.token_hex(4)}",
            },
        )
        begin = time.monotonic()
        service.dispatch(
            "agent_hub_continue",
            {"run_id": started["data"]["run_id"], "expected_revision": 0},
        )
        for _ in range(100):
            current = service.store.get_run(started["data"]["run_id"])
            if current["status"] == "completed":
                break
            time.sleep(0.01)
        elapsed = time.monotonic() - begin
    finally:
        _FakeWorker.invoke_delay = 0.0

    assert current["status"] == "completed"
    assert elapsed < 0.22


def test_parallel_wave_checkpoints_each_completed_provider_immediately(tmp_path):
    _CheckpointWorker.slow_started = threading.Event()
    _CheckpointWorker.release_slow = threading.Event()
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_CheckpointWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Checkpoint parallel work.",
                "capability": "chat",
                "inline_input": "",
                "constraints": {"provider_allowlist": ["gpt"], "timeout_seconds": 5},
            },
            "steps": [
                {
                    "id": "fast",
                    "capability": "chat",
                    "instruction": "Fast.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
                {
                    "id": "slow",
                    "capability": "chat",
                    "instruction": "Slow.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": plan,
            "project_root": str(tmp_path),
            "idempotency_key": f"checkpoint.{secrets.token_hex(4)}",
        },
    )
    service.dispatch(
        "agent_hub_continue",
        {"run_id": started["data"]["run_id"], "expected_revision": 0},
    )
    assert _CheckpointWorker.slow_started.wait(timeout=1.0)
    try:
        for _ in range(100):
            current = service.store.get_run(started["data"]["run_id"])
            statuses = {step["step_id"]: step["status"] for step in current["steps"]}
            if statuses["fast"] == "completed":
                break
            time.sleep(0.01)
        assert current["status"] == "running"
        assert statuses == {"fast": "completed", "slow": "running"}
    finally:
        _CheckpointWorker.release_slow.set()
    for _ in range(100):
        current = service.store.get_run(started["data"]["run_id"])
        if current["status"] == "completed":
            break
        time.sleep(0.01)
    assert current["status"] == "completed"


def test_parallel_cancel_is_cas_fenced_and_ignores_late_results(tmp_path):
    _CheckpointWorker.slow_started = threading.Event()
    _CheckpointWorker.release_slow = threading.Event()
    _CheckpointWorker.cancel_calls = 0
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_CheckpointWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Cancel parallel work safely.",
                "capability": "chat",
                "inline_input": "",
                "constraints": {"provider_allowlist": ["gpt"], "timeout_seconds": 5},
            },
            "steps": [
                {
                    "id": "fast",
                    "capability": "chat",
                    "instruction": "Fast.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
                {
                    "id": "slow",
                    "capability": "chat",
                    "instruction": "Slow.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": plan,
            "project_root": str(tmp_path),
            "idempotency_key": f"cancel-wave.{secrets.token_hex(4)}",
        },
    )
    run_id = started["data"]["run_id"]
    service.dispatch(
        "agent_hub_continue",
        {"run_id": run_id, "expected_revision": 0},
    )
    assert _CheckpointWorker.slow_started.wait(timeout=1.0)
    for _ in range(100):
        current = service.store.get_run(run_id)
        statuses = {step["step_id"]: step["status"] for step in current["steps"]}
        if statuses["fast"] == "completed":
            break
        time.sleep(0.01)

    stale = service.dispatch(
        "agent_hub_cancel",
        {"run_id": run_id, "expected_revision": 0},
    )
    assert stale["success"] is False
    assert stale["error"]["code"] == "revision_conflict"
    assert _CheckpointWorker.cancel_calls == 0

    current = service.store.get_run(run_id)
    cancelled = service.dispatch(
        "agent_hub_cancel",
        {"run_id": run_id, "expected_revision": current["revision"]},
    )
    assert cancelled["success"] is True
    assert cancelled["data"]["status"] == "cancelled"
    cancelled_steps = {step["step_id"]: step for step in cancelled["data"]["steps"]}
    assert cancelled_steps["fast"]["status"] == "completed"
    assert cancelled_steps["slow"]["status"] == "cancelled"
    assert cancelled_steps["slow"]["output_artifact_ids"] == []
    assert _CheckpointWorker.cancel_calls >= 1

    _CheckpointWorker.release_slow.set()
    for _ in range(100):
        with service._active_lock:
            active = run_id in service._active
        if not active:
            break
        time.sleep(0.01)
    final = service.store.get_run(run_id)
    assert final["status"] == "cancelled"
    assert all(step["status"] != "running" for step in final["steps"])
    slow = next(step for step in final["steps"] if step["step_id"] == "slow")
    assert slow["status"] == "cancelled"
    assert slow["output_artifact_ids"] == []


def test_unexpected_worker_exception_releases_run_claim_safely(tmp_path):
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_CrashingWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": _plan(),
            "project_root": str(tmp_path),
            "idempotency_key": f"crash.{secrets.token_hex(4)}",
        },
    )
    service.dispatch(
        "agent_hub_continue",
        {"run_id": started["data"]["run_id"], "expected_revision": 0},
    )
    for _ in range(100):
        current = service.store.get_run(started["data"]["run_id"])
        if current["status"] != "running":
            break
        time.sleep(0.01)

    assert current["status"] == "paused"
    assert current["lease_active"] is False
    events = service.store.events(started["data"]["run_id"])["events"]
    assert events[-1]["details"]["error_code"] == "run_internal_error"
    assert "private fixture failure" not in str(events)


def test_unexpected_parallel_step_exception_preserves_completed_sibling(tmp_path):
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_PartialCrashWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Run independent steps.",
                "capability": "chat",
                "inline_input": "",
                "constraints": {"provider_allowlist": ["gpt"]},
            },
            "steps": [
                {
                    "id": "crash",
                    "capability": "chat",
                    "instruction": "Crash unexpectedly.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
                {
                    "id": "succeed",
                    "capability": "chat",
                    "instruction": "Complete normally.",
                    "routing_requirements": {"planner_provider": "gpt"},
                },
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": plan,
            "project_root": str(tmp_path),
            "idempotency_key": f"parallel-crash.{secrets.token_hex(4)}",
        },
    )
    service.dispatch(
        "agent_hub_continue",
        {"run_id": started["data"]["run_id"], "expected_revision": 0},
    )
    for _ in range(100):
        current = service.store.get_run(started["data"]["run_id"])
        if current["status"] != "running":
            break
        time.sleep(0.01)

    assert current["status"] == "paused"
    steps = {step["step_id"]: step for step in current["steps"]}
    assert steps["crash"]["status"] == "failed"
    assert steps["crash"]["checkpoint"]["error_code"] == "run_internal_error"
    assert steps["succeed"]["status"] == "completed"
    assert len(steps["succeed"]["output_artifact_ids"]) == 1
    assert current["lease_active"] is False
    assert "private fixture failure" not in str(service.store.events(started["data"]["run_id"]))


def test_explicit_retry_requeues_safe_failed_step_and_completes(tmp_path):
    _RetryOnceWorker.invocations = 0
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_RetryOnceWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": _plan(),
            "project_root": str(tmp_path),
            "idempotency_key": f"retry.{secrets.token_hex(4)}",
        },
    )
    run_id = started["data"]["run_id"]
    service.dispatch(
        "agent_hub_continue",
        {"run_id": run_id, "expected_revision": 0},
    )
    for _ in range(100):
        failed = service.store.get_run(run_id)
        if failed["status"] == "paused":
            break
        time.sleep(0.01)

    failed_step = failed["steps"][0]
    assert failed_step["status"] == "failed"
    assert failed_step["checkpoint"]["error_code"] == "fallback_exhausted"
    assert failed_step["checkpoint"]["retry_safe"] is True
    assert failed["retryable_failed_steps"] == ["answer"]
    assert failed["next_action"] == {
        "type": "call_tool",
        "tool": "agent_hub_continue",
        "arguments": {
            "run_id": run_id,
            "expected_revision": failed["revision"],
            "retry_failed_steps": ["answer"],
        },
    }

    receipt = service.dispatch(
        "agent_hub_continue",
        {
            "run_id": run_id,
            "expected_revision": failed["revision"],
            "retry_failed_steps": ["answer"],
        },
    )
    assert receipt["success"] is True
    for _ in range(100):
        completed = service.store.get_run(run_id)
        if completed["status"] == "completed":
            break
        time.sleep(0.01)

    assert completed["status"] == "completed"
    assert completed["steps"][0]["attempt"] == 2
    assert _RetryOnceWorker.invocations == 2


def test_explicit_retry_rejects_ambiguous_or_internal_failure(tmp_path):
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_CrashingWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": _plan(),
            "project_root": str(tmp_path),
            "idempotency_key": f"unsafe-retry.{secrets.token_hex(4)}",
        },
    )
    run_id = started["data"]["run_id"]
    service.dispatch(
        "agent_hub_continue",
        {"run_id": run_id, "expected_revision": 0},
    )
    for _ in range(100):
        failed = service.store.get_run(run_id)
        if failed["status"] == "paused":
            break
        time.sleep(0.01)

    result = service.dispatch(
        "agent_hub_continue",
        {
            "run_id": run_id,
            "expected_revision": failed["revision"],
            "retry_failed_steps": ["answer"],
        },
    )

    assert result["success"] is False
    assert result["error"]["code"] == "step_not_retryable"


def test_live_catalog_context_limit_is_revisioned_cached_and_used_for_routing(tmp_path):
    _CatalogLimitWorker.invocations = 0
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_CatalogLimitWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )

    catalog = service.dispatch(
        "agent_hub_catalog",
        {"provider": "gpt", "refresh": True},
    )

    provider_catalog = catalog["data"]["providers"]["gpt"]
    assert provider_catalog["catalog_revision"]
    assert provider_catalog["context_limit_model_count"] == 1
    assert provider_catalog["context_limit_ttl_seconds"] == 300
    result = service.dispatch(
        "agent_hub_execute",
        {
            "project_root": str(tmp_path),
            "provider": "gpt",
            "model": "gpt-live",
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Use the live limit.",
                "capability": "chat",
                "inline_input": "x" * 44,
                "constraints": {
                    "provider_allowlist": ["gpt"],
                    "max_tokens": 100,
                },
            },
        },
    )

    assert result["success"] is False
    assert result["error"]["code"] == "provider_context_limit"
    assert _CatalogLimitWorker.invocations == 0
    with service._catalog_limit_cache_lock:
        service._catalog_limit_cache[("gpt", "gpt-live")]["expires_at"] = 0.0
    assert service._cached_model_limit("gpt", "gpt-live") is None


def test_declared_fallbacks_limit_execution_order(tmp_path):
    _FallbackWorker.invoked = []
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_FallbackWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Use the declared fallback only.",
                "capability": "chat",
                "inline_input": "",
                "constraints": {
                    "provider_allowlist": ["gpt", "gemini", "claude"],
                },
            },
            "steps": [
                {
                    "id": "answer",
                    "capability": "chat",
                    "instruction": "Answer.",
                    "routing_requirements": {
                        "planner_provider": "gpt",
                        "fallbacks": ["gemini"],
                        "model": "gpt-primary-model",
                    },
                }
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": plan,
            "project_root": str(tmp_path),
            "idempotency_key": f"fallback.{secrets.token_hex(4)}",
        },
    )
    service.dispatch(
        "agent_hub_continue",
        {"run_id": started["data"]["run_id"], "expected_revision": 0},
    )
    for _ in range(100):
        current = service.store.get_run(started["data"]["run_id"])
        if current["status"] != "running":
            break
        time.sleep(0.01)

    assert current["status"] == "completed"
    assert current["steps"][0]["provider"] == "gemini"
    assert _FallbackWorker.invoked == [
        ("gpt", "gpt-primary-model"),
        ("gemini", "gemini-default"),
    ]
    events = service.store.events(started["data"]["run_id"])["events"]
    attempts = [
        (event["type"], event["details"].get("provider"))
        for event in events
        if event["type"].startswith("provider_attempt_")
    ]
    assert attempts == [
        ("provider_attempt_started", "gpt"),
        ("provider_attempt_failed", "gpt"),
        ("provider_attempt_started", "gemini"),
        ("provider_attempt_completed", "gemini"),
    ]


def test_success_without_explicit_quality_gate_does_not_invent_quality(tmp_path):
    service = _service(tmp_path)
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": _plan(),
            "project_root": str(tmp_path),
            "idempotency_key": f"quality.{secrets.token_hex(4)}",
        },
    )
    service.dispatch(
        "agent_hub_continue",
        {"run_id": started["data"]["run_id"], "expected_revision": 0},
    )
    for _ in range(100):
        current = service.store.get_run(started["data"]["run_id"])
        if current["status"] != "running":
            break
        time.sleep(0.01)

    with sqlite3.connect(service.store.path) as connection:
        samples = connection.execute(
            "SELECT success, quality FROM routing_samples ORDER BY sample_id"
        ).fetchall()
    assert current["status"] == "completed"
    assert samples == [(1, None)]
