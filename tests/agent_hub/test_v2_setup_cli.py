from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest

from agent_hub import local_setup
from agent_hub.v2.setup import apply_setup, plan_setup


def test_local_setup_renders_only_the_canonical_bridge(tmp_path):
    for relative in local_setup.CONFIG_PATHS:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
    plan = local_setup.plan_setup(tmp_path)

    codex = next(
        item.rendered for item in plan.changes if item.relative_path == ".codex/config.toml"
    )
    assert b"agent-hub-mcp" in codex
    with pytest.raises(local_setup.SetupError, match="unsupported"):
        local_setup.plan_setup(tmp_path, hub_executable="agent-hub-v2-mcp")


def test_cli_help_has_no_retired_migration_command():
    result = subprocess.run(
        [sys.executable, "-m", "agent_hub.v2.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "migrate-v1" not in result.stdout


def test_retired_runtime_modules_are_absent():
    assert importlib.util.find_spec("agent_hub.operations") is None
    assert importlib.util.find_spec("agent_hub.providers.hub") is None
    assert importlib.util.find_spec("agent_hub.core.inprocess") is None
    assert importlib.util.find_spec("agent_hub.core.run_lifecycle") is None
    assert importlib.util.find_spec("agent_hub.core.takeover") is None
    assert importlib.util.find_spec("orchestrate_codex.mcp_server") is None
    assert importlib.util.find_spec("orchestrate_codex.runner") is None
    assert importlib.util.find_spec("orchestrate_codex.store") is None


def test_v2_setup_is_digest_fenced_and_can_apply_without_launchctl(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in local_setup.CONFIG_PATHS:
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
    executable = repo / ".venv/bin/agent-hubd"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    launch_dir = tmp_path / "LaunchAgents"
    state_root = tmp_path / "state"

    proposal = plan_setup(
        repo,
        target_root=repo,
        launch_agents_dir=launch_dir,
        state_root=state_root,
    )
    result = apply_setup(
        proposal,
        proposal_sha256=proposal["proposal_sha256"],
        activate=False,
    )

    assert result["success"] is True
    assert result["activation"]["attempted"] is False
    assert (launch_dir / "com.agent-hub.daemon.plist").exists()
    assert "agent-hub-mcp" in (repo / ".codex/config.toml").read_text()
