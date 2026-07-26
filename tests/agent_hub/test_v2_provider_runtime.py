from __future__ import annotations

import json
import socket
import socketserver
import subprocess
import threading

import pytest

from agent_hub import operations
from agent_hub.v2.contracts import TASK_SCHEMA
from agent_hub.core.limits import MAX_PROVIDER_TIMEOUT_SECONDS
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.egress_proxy import (
    PROXY_IDLE_TIMEOUT_SECONDS,
    ProviderEgressProxy,
    _ProxyHandler,
)
from agent_hub.v2.provider_client import ProviderWorkerClient
from agent_hub.v2.provider_worker import handle_request


def test_worker_initialize_returns_valid_manifest():
    result = handle_request("gpt", {"id": "x", "method": "initialize", "params": {}})

    assert result["success"] is True
    assert result["result"]["manifest"]["provider_id"] == "gpt"
    assert result["result"]["protocol"] == "agent_hub_provider_worker_v2"


def test_worker_rejects_local_only_capability_without_exception_text():
    result = handle_request(
        "gpt",
        {
            "id": "x",
            "method": "invoke",
            "params": {
                "task": {
                    "schema": TASK_SCHEMA,
                    "intent": "Inspect.",
                    "capability": "inspect",
                    "inline_input": "fixture",
                }
            },
        },
    )

    assert result["success"] is False
    assert result["error"]["code"] == "unsupported_worker_capability"


def test_worker_maps_nested_planner_failure_without_leaking_message(monkeypatch):
    def failed_dispatch(tool, arguments):
        assert tool == "agent_hub_plan_workflow"
        return {
            "success": False,
            "error": {
                "type": "workflow_validation_error",
                "message": "secret planner prompt must not cross the boundary",
            },
        }

    monkeypatch.setattr(operations, "dispatch_tool", failed_dispatch)
    result = handle_request(
        "gpt",
        {
            "id": "x",
            "method": "plan",
            "params": {
                "project_root": "/tmp",
                "task": {
                    "schema": TASK_SCHEMA,
                    "intent": "Plan.",
                    "capability": "write",
                    "inline_input": "",
                },
            },
        },
    )

    assert result["success"] is False
    assert result["error"]["code"] == "planner_execution_failed"
    assert result["error"]["safe_details"]["reason_code"] == ("workflow_validation_error")
    assert "secret" not in str(result)


def test_worker_forwards_v2_planner_budgets(monkeypatch):
    captured = {}

    def successful_dispatch(tool, arguments):
        captured.update(arguments)
        return {"success": True, "data": {"plan": {"steps": []}}}

    monkeypatch.setattr(operations, "dispatch_tool", successful_dispatch)
    result = handle_request(
        "gpt",
        {
            "id": "x",
            "method": "plan",
            "params": {
                "project_root": "/tmp",
                "task": {
                    "schema": TASK_SCHEMA,
                    "intent": "Plan.",
                    "capability": "write",
                    "inline_input": "",
                    "constraints": {
                        "max_leaf_calls": 100,
                        "max_tokens": 131_072,
                        "timeout_seconds": 1790,
                    },
                },
            },
        },
    )

    assert result["success"] is True
    assert captured["max_leaf_calls"] == 100
    assert captured["planner_max_tokens"] == 131_072
    assert captured["workflow_timeout"] == 1790
    assert captured["per_call_timeout"] == 1790


def test_worker_disables_hidden_v1_rewrites_for_v2_write(monkeypatch):
    captured = {}

    def successful_dispatch(tool, arguments):
        captured["tool"] = tool
        captured["arguments"] = arguments
        return {"success": True, "text": "draft"}

    monkeypatch.setattr(operations, "dispatch_tool", successful_dispatch)
    result = handle_request(
        "gpt",
        {
            "id": "x",
            "method": "invoke",
            "params": {
                "task": {
                    "schema": TASK_SCHEMA,
                    "intent": "Write.",
                    "capability": "write",
                    "inline_input": "facts",
                },
            },
        },
    )

    assert result["success"] is True
    assert captured["tool"] == "agent_hub_write"
    assert captured["arguments"]["quality_rewrite_attempts"] == 0


class _FixtureProcess:
    def __init__(self, *args, response, returncode=0, **kwargs):
        self._response = response
        self.returncode = returncode

    def communicate(self, value=None, timeout=None):
        return self._response, "private stderr must be ignored"

    def terminate(self):
        return None

    def kill(self):
        return None

    def poll(self):
        return self.returncode


def test_client_parses_one_safe_worker_response():
    response = json.dumps(
        {
            "id": "request",
            "success": True,
            "result": {"success": True, "operation": "status", "data": {}},
        }
    )

    client = ProviderWorkerClient(
        "gpt",
        process_factory=lambda *args, **kwargs: _FixtureProcess(*args, response=response, **kwargs),
    )

    assert client.request("status")["operation"] == "status"


def test_client_maps_timeout_without_stderr_or_prompt():
    class _TimeoutProcess(_FixtureProcess):
        def communicate(self, value=None, timeout=None):
            if timeout == 2.0:
                return "", ""
            raise subprocess.TimeoutExpired("private command", timeout)

    client = ProviderWorkerClient(
        "gpt",
        process_factory=lambda *args, **kwargs: _TimeoutProcess(*args, response="", **kwargs),
    )

    with pytest.raises(HubV2Error) as error:
        client.request("invoke", timeout=0.1)
    assert error.value.code == "provider_timeout"
    assert "private" not in str(error.value.public())


def test_provider_proxy_rejects_undeclared_connect_target():
    with ProviderEgressProxy(["example.com"]) as proxy:
        host, port = proxy.url.removeprefix("http://").split(":")
        client = socket.create_connection((host, int(port)))
        try:
            client.sendall(b"CONNECT forbidden.invalid:443 HTTP/1.1\r\n\r\n")
            response = client.recv(1024)
        finally:
            client.close()

    assert b"403 Forbidden" in response


def test_provider_proxy_keeps_quiet_long_generation_tunnel_open(monkeypatch):
    observed = []

    def no_activity(readable, writable, exceptional, timeout):
        observed.append(timeout)
        return [], [], []

    monkeypatch.setattr("agent_hub.v2.egress_proxy.select.select", no_activity)
    _ProxyHandler._relay(object(), object())

    assert observed == [float(MAX_PROVIDER_TIMEOUT_SECONDS)]
    assert PROXY_IDLE_TIMEOUT_SECONDS == float(MAX_PROVIDER_TIMEOUT_SECONDS)


def test_provider_proxy_relays_declared_localhost():
    class _Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    class _Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(1024)
            self.request.sendall(b"ok")

    upstream = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        with ProviderEgressProxy(["localhost"]) as proxy:
            host, port = proxy.url.removeprefix("http://").split(":")
            client = socket.create_connection((host, int(port)))
            try:
                client.sendall(
                    f"CONNECT localhost:{upstream.server_address[1]} HTTP/1.1\r\n\r\n".encode()
                )
                assert b"200 Connection Established" in client.recv(1024)
                client.sendall(b"fixture")
                assert client.recv(1024) == b"ok"
            finally:
                client.close()
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
