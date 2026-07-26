from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time

from agent_hub.v2.contracts import PLAN_SCHEMA, TASK_SCHEMA, validate_plan
from agent_hub.v2.crypto import ArtifactCipher, StaticKeyProvider
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
    invoked: list[str] = []

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method != "invoke":
            return super().request(method, params=params, timeout=timeout)
        self.__class__.invoked.append(self.provider)
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


class _CheckpointWorker(_FakeWorker):
    slow_started = threading.Event()
    release_slow = threading.Event()

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "invoke" and params["task"]["intent"] == "Slow.":
            self.__class__.slow_started.set()
            if not self.__class__.release_slow.wait(timeout=5.0):
                raise AssertionError("slow fixture was not released")
        return super().request(method, params=params, timeout=timeout)


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
    assert _FallbackWorker.invoked == ["gpt", "gemini"]
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
