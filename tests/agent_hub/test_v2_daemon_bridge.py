from __future__ import annotations

from pathlib import Path
import tempfile
import time

import pytest

from agent_hub.core.limits import MAX_PROVIDER_TIMEOUT_SECONDS
from agent_hub.v2.bridge import (
    DEFAULT_DAEMON_CALL_TIMEOUT_SECONDS,
    handle_request,
)
from agent_hub.v2.contracts import PLAN_SCHEMA, TASK_SCHEMA, validate_plan
from agent_hub.v2.daemon import HubDaemon, HubDaemonClient, _safe_socket_parent
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.store import HubStore
from agent_hub.v2.tools import TOOL_NAMES, tool_definitions


def test_v2_public_surface_has_exactly_fourteen_tools():
    definitions = tool_definitions()

    assert len(definitions) == 14
    assert tuple(item["name"] for item in definitions) == TOOL_NAMES


def test_daemon_socket_ping_and_tool_list(tmp_path):
    with tempfile.TemporaryDirectory(prefix="ahv2-", dir="/tmp") as socket_dir:
        socket_path = Path(socket_dir) / "hub.sock"
        try:
            daemon = HubDaemon(
                socket_path=socket_path,
                store=HubStore(tmp_path / "state" / "state.sqlite3"),
            )
        except PermissionError:
            pytest.skip("the current sandbox blocks AF_UNIX bind")
        daemon.serve_in_thread()
        client = HubDaemonClient(socket_path)
        try:
            for _ in range(20):
                try:
                    ping = client.request("ping")
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(0.01)
            else:
                raise AssertionError("daemon did not start")
            listed = client.request("tools/list")
        finally:
            daemon.close()

        assert ping["success"] is True
        assert len(listed["data"]["tools"]) == 14
        assert not socket_path.exists()


def test_daemon_exposes_egress_review_only_on_internal_gui_channel(tmp_path):
    with tempfile.TemporaryDirectory(prefix="ahv2-", dir="/tmp") as socket_dir:
        socket_path = Path(socket_dir) / "hub.sock"
        try:
            daemon = HubDaemon(
                socket_path=socket_path,
                store=HubStore(tmp_path / "state" / "state.sqlite3"),
            )
        except PermissionError:
            pytest.skip("the current sandbox blocks AF_UNIX bind")
        (tmp_path / "fact.txt").write_text("safe fact\n")
        prepared = daemon.service.dispatch(
            "agent_hub_plan",
            {
                "mode": "prepare",
                "project_root": str(tmp_path),
                "provider": "gpt",
                "source_paths": ["fact.txt"],
                "task": {
                    "schema": TASK_SCHEMA,
                    "intent": "Review the source.",
                    "capability": "write",
                    "inline_input": "",
                },
            },
        )
        review_id = prepared["data"]["approval_request"]["review_id"]
        daemon.serve_in_thread()
        client = HubDaemonClient(socket_path)
        try:
            for _ in range(20):
                try:
                    client.request("ping")
                    break
                except HubV2Error:
                    time.sleep(0.01)
            listed = client.request("egress/reviews")
            approved = client.request(
                "egress/decide",
                {"review_id": review_id, "decision": "approve"},
            )
            public = client.request(
                "tools/call",
                {
                    "name": "agent_hub_approve_egress",
                    "arguments": {"review_id": review_id},
                },
            )
        finally:
            daemon.close()

    assert listed["data"]["reviews"][0]["review_id"] == review_id
    assert approved["data"]["status"] == "approved"
    assert public["success"] is False
    assert public["error"]["code"] == "unknown_tool"


def test_bridge_lists_tools_without_daemon(tmp_path):
    client = HubDaemonClient(tmp_path / "missing.sock")

    response = handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        client=client,
    )

    assert len(response["result"]["tools"]) == 14


def test_bridge_returns_safe_daemon_unavailable_error(tmp_path):
    client = HubDaemonClient(tmp_path / "missing.sock")

    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "agent_hub_get", "arguments": {"run_id": "fixture"}},
        },
        client=client,
    )

    structured = response["result"]["structuredContent"]
    assert structured["success"] is False
    assert structured["error"]["code"] == "daemon_unavailable"
    assert "missing.sock" not in str(structured)


def test_bridge_reserves_provider_budget_for_long_running_tools():
    class CapturingClient:
        def __init__(self):
            self.calls = []

        def request(self, method, params=None, *, timeout=5.0):
            self.calls.append((method, params, timeout))
            return {"success": True, "operation": params["name"], "data": {}}

    client = CapturingClient()
    for name, arguments in (
        ("agent_hub_plan", {"mode": "apply"}),
        ("agent_hub_execute", {"task": {}}),
        ("agent_hub_get", {"run_id": "fixture"}),
    ):
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": name,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            client=client,
        )
        assert response["result"]["structuredContent"]["success"] is True

    assert client.calls[0][2] == float(MAX_PROVIDER_TIMEOUT_SECONDS)
    assert client.calls[1][2] == float(MAX_PROVIDER_TIMEOUT_SECONDS)
    assert client.calls[2][2] == DEFAULT_DAEMON_CALL_TIMEOUT_SECONDS


def test_socket_parent_rejects_directory_owned_by_another_user(tmp_path, monkeypatch):
    parent = tmp_path / "run"
    parent.mkdir()
    original = Path.lstat

    def foreign_owner(path):
        result = original(path)
        if Path(path) == parent:
            values = list(result)
            values[4] = result.st_uid + 1
            return type(result)(values)
        return result

    monkeypatch.setattr(Path, "lstat", foreign_owner)

    with pytest.raises(HubV2Error) as error:
        _safe_socket_parent(parent / "hub.sock")
    assert error.value.code == "unsafe_socket_path"


def test_daemon_recovers_lease_that_expires_after_startup(tmp_path):
    clock = [100.0]
    store = HubStore(tmp_path / "state.sqlite3", clock=lambda: clock[0])
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": "Fixture.",
                "capability": "chat",
                "inline_input": "fixture",
                "constraints": {"provider_allowlist": ["gpt"]},
            },
            "steps": [
                {
                    "id": "answer",
                    "capability": "chat",
                    "instruction": "Answer.",
                }
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )
    run = store.create_run(
        plan=plan,
        project_root=str(tmp_path),
        idempotency_key="daemon.late-expiry",
    )
    claim = store.claim_run(run["run_id"], expected_revision=0, lease_seconds=1)
    store.update_step(
        run["run_id"],
        step_id="answer",
        expected_run_revision=claim.revision,
        status="running",
        checkpoint={"retry_safe": False, "phase": "provider_request_pending"},
    )
    with tempfile.TemporaryDirectory(prefix="ahv2-recovery-", dir="/tmp") as directory:
        daemon = HubDaemon(
            socket_path=Path(directory) / "hub.sock",
            store=store,
        )
        daemon.serve_in_thread()
        try:
            clock[0] = 102.0
            for _ in range(60):
                recovered = store.get_run(run["run_id"])
                if recovered["status"] == "outcome_unknown":
                    break
                time.sleep(0.05)
        finally:
            daemon.close()

    assert recovered["status"] == "outcome_unknown"
