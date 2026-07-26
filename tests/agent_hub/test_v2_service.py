from __future__ import annotations

import secrets
import time

from agent_hub.v2.contracts import PLAN_SCHEMA, TASK_SCHEMA, validate_plan
from agent_hub.v2.crypto import ArtifactCipher, StaticKeyProvider
from agent_hub.v2.service import HubService
from agent_hub.v2.store import HubStore


class _FakeWorker:
    invoke_delay = 0.0

    def __init__(self, provider):
        self.provider = provider

    def request(self, method, params=None, timeout=30.0):
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

    def request(self, method, params=None, timeout=30.0):
        if method != "plan":
            return super().request(method, params=params, timeout=timeout)
        self.__class__.last_prompt = str(params["planner_prompt"])
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


def _service(tmp_path):
    return HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_FakeWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )


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
    assert "fact.txt" in _PlannerWorker.last_prompt
    assert proposal["manifest"]["manifest_sha256"] in _PlannerWorker.last_prompt
    assert len(_PlannerWorker.last_prompt) < 33_000
    assert [step["capability"] for step in applied["data"]["plan"]["steps"]] == [
        "inspect",
        "write",
        "review",
    ]
    assert applied["data"]["plan"]["steps"][1]["routing_requirements"]["fallbacks"] == [
        "claude",
        "gemini",
    ]


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
                "constraints": {"provider_allowlist": ["gpt"]},
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
