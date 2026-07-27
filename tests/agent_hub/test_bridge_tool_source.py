"""Where the MCP tool list comes from.

The bridge and the daemon are separate installs. `agent-hub update` switches the
daemon's LaunchAgent and leaves the bridge path alone, so after an update they
are routinely different releases. While the bridge answered tools/list from its
own copy, that meant the list described a version that was not executing: on the
2.4.1 to 3.0.0 switch the daemon accepted task.input_images and artifact
include_base64 while the advertised list mentioned neither.

tools/call was always forwarded to the daemon untouched. These tests pin that
tools/list is too.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from agent_hub.doctor import run_doctor
from agent_hub.v2 import bridge
from agent_hub.v2.errors import HubV2Error


class _Daemon:
    """A daemon that knows about a tool this bridge build has never heard of."""

    def __init__(self, tools=None, fail=False):
        self._tools = tools
        self._fail = fail
        self.calls = []

    def request(self, method, params=None, timeout=None):
        self.calls.append(method)
        if self._fail:
            raise HubV2Error("daemon_unavailable", "no daemon", scope="daemon")
        if method == "tools/list":
            return {"success": True, "operation": "tools/list", "data": {"tools": self._tools}}
        raise AssertionError(method)


def _list_tools(client):
    response = bridge.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        client=client,
    )
    return response["result"]["tools"]


def test_the_running_daemon_decides_what_the_tools_are():
    newer = [{"name": "agent_hub_execute", "inputSchema": {"properties": {"task": {}}}}]
    client = _Daemon(tools=newer)

    tools = _list_tools(client)

    assert tools == newer
    assert client.calls == ["tools/list"]


def test_a_tool_this_build_does_not_know_still_reaches_the_caller():
    # The failure this prevents: a newer daemon grows a field, an older bridge
    # keeps answering from its own copy, and the caller never learns the field
    # exists. The bridge must not filter the daemon's answer through what it
    # happens to know.
    client = _Daemon(tools=[{"name": "agent_hub_from_the_future", "inputSchema": {}}])

    tools = _list_tools(client)

    assert [item["name"] for item in tools] == ["agent_hub_from_the_future"]


def test_an_unreachable_daemon_still_yields_a_usable_list():
    # Every call would fail with daemon_unavailable in this state, so the list
    # describes what will exist once it is up rather than promising anything now.
    tools = _list_tools(_Daemon(fail=True))

    assert len(tools) == 14
    assert any(item["name"] == "agent_hub_execute" for item in tools)


@pytest.mark.parametrize("answer", [None, [], "not-a-list", {}])
def test_a_daemon_answering_without_tools_is_treated_as_malfunctioning(answer):
    tools = _list_tools(_Daemon(tools=answer))

    assert len(tools) == 14


def test_doctor_notices_a_bridge_from_a_different_release(tmp_path, monkeypatch):
    """The gap local_config cannot see.

    local_config takes the configured command as its input and checks it against
    itself, so a bridge that exists and is executable passes even when it
    belongs to a release the daemon is not running.
    """

    releases = tmp_path / "releases"
    daemon_root = releases / "3.0.0-aaaa"
    bridge_root = releases / "2.4.1-bbbb"
    (bridge_root / "bin").mkdir(parents=True)
    (bridge_root / "bin" / "agent-hub-mcp").write_text("#!/bin/sh\n", encoding="utf-8")
    daemon_root.mkdir(parents=True)
    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers": {"agent-hub": {"command": "%s"}}}'
        % (bridge_root / "bin" / "agent-hub-mcp"),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "prefix", str(daemon_root))

    report = run_doctor(str(tmp_path))
    check = next(item for item in report["checks"] if item["id"] == "bridge_runtime")

    assert check["status"] == "warn"
    assert "different release" in check["message"]


def test_doctor_says_nothing_about_a_development_checkout(tmp_path, monkeypatch):
    # A dev venv has no reason to match the installed bridge, and a check that
    # warns on every developer machine is a check people learn to ignore.
    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers": {"agent-hub": {"command": "/usr/bin/true"}}}', encoding="utf-8"
    )
    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".venv"))

    report = run_doctor(str(tmp_path))
    check = next(item for item in report["checks"] if item["id"] == "bridge_runtime")

    assert check["status"] == "pass"
    assert "not a staged release" in check["message"]


def test_the_same_install_reports_alignment(tmp_path, monkeypatch):
    releases = tmp_path / "releases"
    root = releases / "3.0.0-aaaa"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "agent-hub-mcp").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers": {"agent-hub": {"command": "%s"}}}' % (root / "bin" / "agent-hub-mcp"),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "prefix", str(root))

    report = run_doctor(str(tmp_path))
    check = next(item for item in report["checks"] if item["id"] == "bridge_runtime")

    assert check["status"] == "pass"
    assert Path(check["details"]["runtime_root"]) == root.resolve()
