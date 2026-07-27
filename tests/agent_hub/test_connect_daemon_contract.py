"""What the setup GUI actually sends to the daemon.

The GUI's connection test was broken for two days and every provider, and the
five existing tests for it all passed. They pass a fake status_reader, and
`_use_daemon` is true only for the real one -- so `_daemon_call` returned None
without ever building its arguments. The tool contract was never exercised.

These tests capture the arguments and run them through the real dispatcher, so
a required field the GUI forgets fails here rather than in the user's hands.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from agent_hub import connect_service
from agent_hub.connect_service import CONNECTION_TEST_SCOPE, ConnectionManager
from agent_hub.v2.crypto import ArtifactCipher, StaticKeyProvider
from agent_hub.v2.service import HubService
from agent_hub.v2.store import HubStore


class _Worker:
    def __init__(self, provider):
        self.provider = provider

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "status":
            return {"success": True, "data": {"providers": {self.provider: {"ready": True}}}}
        if method == "catalog":
            return {"success": True, "warnings": [], "data": {"models": {}}}
        if method == "invoke":
            return {
                "success": True,
                "text": "connected",
                "model": f"{self.provider}-fixture",
                "usage": {"total_tokens": 4},
            }
        raise AssertionError(method)

    def cancel(self):
        return True


def _state(**overrides: Any) -> dict[str, Any]:
    base = {
        "consent": True,
        "configured": True,
        "authenticated": True,
        "ready": True,
        "invocation_ready": True,
        "default_model": "gemini-3.6-flash-high",
        "capabilities": {"chat": {"supported": True}},
        "warnings": [],
    }
    base.update(overrides)
    return base


def _reader(states):
    def read(provider="all", *, probe=False):
        selected = states if provider == "all" else {provider: states[provider]}
        return {"providers": selected, "probe": probe}

    return read


def _capture_connection_test_arguments(monkeypatch) -> dict[str, Any]:
    """Run the GUI's test and return exactly what it asked the daemon for."""

    captured: dict[str, Any] = {}
    manager = ConnectionManager(
        status_reader=_reader({"gemini": _state()}),
    )
    # The real reader turns this on; a fake one leaves the whole daemon call
    # unbuilt, which is how the missing field survived.
    manager._use_daemon = True  # noqa: SLF001

    def fake_call(name, arguments):
        captured["name"] = name
        captured["arguments"] = dict(arguments)
        return {"success": True, "data": {"result": {"text": "connected"}}}

    monkeypatch.setattr(manager, "_daemon_call", fake_call)

    started = manager.start_test("gemini")
    deadline = time.time() + 5
    job = manager.job(started["id"])
    while job["state"] == "working" and time.time() < deadline:
        time.sleep(0.01)
        job = manager.job(started["id"])
    manager.close()

    assert captured, "the GUI never called the daemon"
    return captured


def test_the_connection_test_sends_a_project_root(monkeypatch):
    # The defect: agent_hub_execute requires project_root and the GUI sent none,
    # so every provider's connection test failed with invalid_project_root.
    captured = _capture_connection_test_arguments(monkeypatch)

    assert captured["name"] == "agent_hub_execute"
    assert captured["arguments"]["project_root"] == str(CONNECTION_TEST_SCOPE)


def test_the_scope_is_machine_global_rather_than_some_project(monkeypatch):
    # A connection test asks whether this machine's account reaches the
    # provider. Borrowing a project's policy would let that project's allowlist
    # make a working account look broken.
    captured = _capture_connection_test_arguments(monkeypatch)

    assert CONNECTION_TEST_SCOPE.is_absolute()
    assert captured["arguments"]["project_root"] == str(CONNECTION_TEST_SCOPE)


def test_the_scope_is_created_on_a_machine_that_has_never_run_the_daemon(monkeypatch, tmp_path):
    """CI caught this: asking for the state root is not the same as having one.

    canonical_project_root resolves strictly, so a scope that does not exist is
    rejected exactly like the missing argument this fix replaced -- which would
    have left the connection test broken on precisely the fresh installs where
    someone reaches for it first.
    """

    fresh = tmp_path / "never-created" / ".agent-hub"
    monkeypatch.setattr(connect_service, "CONNECTION_TEST_SCOPE", fresh)
    assert not fresh.exists()

    resolved = connect_service._connection_test_scope()  # noqa: SLF001

    assert resolved == fresh
    assert fresh.is_dir()
    assert fresh.stat().st_mode & 0o777 == 0o700


def test_the_arguments_survive_the_real_dispatcher(monkeypatch, tmp_path):
    """The test that would have caught this.

    Stubbing _daemon_call proves the GUI sent something; running it through the
    dispatcher proves it sent something the tool accepts.
    """

    captured = _capture_connection_test_arguments(monkeypatch)
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_Worker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )

    result = service.dispatch(
        "agent_hub_execute",
        {**captured["arguments"], "project_root": str(tmp_path)},
    )

    assert result["success"] is True, result.get("error")
    assert result["data"]["result"]["text"] == "connected"


def test_a_missing_project_root_is_rejected_by_the_dispatcher(monkeypatch, tmp_path):
    # Pins the requirement itself, so this test file fails if either side moves.
    captured = _capture_connection_test_arguments(monkeypatch)
    arguments = {
        key: value for key, value in captured["arguments"].items() if key != "project_root"
    }
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_Worker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )

    result = service.dispatch("agent_hub_execute", arguments)

    assert result["success"] is False
    assert result["error"]["code"] == "invalid_project_root"


@pytest.mark.parametrize("provider", ["claude", "grok", "gemini", "gpt"])
def test_every_provider_takes_the_same_path(monkeypatch, provider):
    # The break was never gemini-specific; it was every provider, because the
    # missing field is in code none of them vary.
    captured: dict[str, Any] = {}
    manager = ConnectionManager(status_reader=_reader({provider: _state()}))
    manager._use_daemon = True  # noqa: SLF001
    monkeypatch.setattr(
        manager,
        "_daemon_call",
        lambda name, arguments: (
            captured.update(arguments) or {"success": True, "data": {"result": {"text": "ok"}}}
        ),
    )

    started = manager.start_test(provider)
    deadline = time.time() + 5
    job = manager.job(started["id"])
    while job["state"] == "working" and time.time() < deadline:
        time.sleep(0.01)
        job = manager.job(started["id"])
    manager.close()

    assert captured.get("project_root") == str(CONNECTION_TEST_SCOPE)
