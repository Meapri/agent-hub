from __future__ import annotations

import importlib.util
from pathlib import Path
import plistlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_hub import local_setup
from agent_hub.v2.errors import HubV2Error
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


def test_local_setup_accepts_reviewed_absolute_versioned_bridge(tmp_path):
    for relative in local_setup.CONFIG_PATHS:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
    bridge = tmp_path / "releases/2.0.1/bin/agent-hub-mcp"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("#!/bin/sh\n")
    bridge.chmod(0o700)

    plan = local_setup.plan_setup(tmp_path, hub_command=bridge)

    codex = next(
        item.rendered for item in plan.changes if item.relative_path == ".codex/config.toml"
    )
    assert str(bridge).encode() in codex


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
    assert importlib.util.find_spec("agent_hub.core.mcp") is None
    assert importlib.util.find_spec("agent_hub.core.rpc") is None
    assert importlib.util.find_spec("agent_hub.core.parallel") is None
    assert importlib.util.find_spec("google_antigravity_codex.cli") is None
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
    executable.chmod(0o700)
    bridge = repo / ".venv/bin/agent-hub-mcp"
    bridge.write_text("#!/bin/sh\n")
    bridge.chmod(0o700)
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


def test_v2_setup_can_target_a_versioned_runtime_root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in local_setup.CONFIG_PATHS:
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
    runtime = tmp_path / "releases/2.0.1"
    runtime.mkdir(parents=True)
    for name in ("agent-hubd", "agent-hub-mcp"):
        executable = runtime / "bin" / name
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o700)

    proposal = plan_setup(
        repo,
        target_root=repo,
        launch_agents_dir=tmp_path / "LaunchAgents",
        state_root=tmp_path / "state",
        runtime_root=runtime,
    )

    assert proposal["runtime_root"] == str(runtime)
    assert str(runtime / "bin/agent-hubd") in proposal["launch_agent"]["rendered"]
    launch_payload = plistlib.loads(proposal["launch_agent"]["rendered"].encode())
    assert launch_payload["EnvironmentVariables"]["HOME"] == str(Path.home())
    codex = next(
        item.rendered
        for item in proposal["_host_plan"].changes
        if item.relative_path == ".codex/config.toml"
    )
    assert str(runtime / "bin/agent-hub-mcp").encode() in codex


def test_local_setup_rolls_back_prior_files_when_a_later_write_fails(
    tmp_path,
    monkeypatch,
):
    for relative in local_setup.CONFIG_PATHS:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
    plan = local_setup.plan_setup(tmp_path)
    real_write = local_setup._atomic_write
    failing = plan.changed[1]

    def flaky_write(path, content, *, target_root):
        if path == failing.target and content == failing.rendered:
            raise local_setup.SetupError("fixture write failure")
        return real_write(path, content, target_root=target_root)

    monkeypatch.setattr(local_setup, "_atomic_write", flaky_write)
    with pytest.raises(local_setup.SetupError, match="fixture write failure"):
        local_setup.apply_plan(plan)

    assert not plan.changed[0].target.exists()
    assert not plan.changed[1].target.exists()


def test_v2_setup_rejects_symlinked_launch_agent_ancestor(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in local_setup.CONFIG_PATHS:
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
    for name in ("agent-hubd", "agent-hub-mcp"):
        executable = repo / ".venv/bin" / name
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(HubV2Error) as error:
        plan_setup(
            repo,
            launch_agents_dir=linked / "LaunchAgents",
            state_root=tmp_path / "state",
        )

    assert error.value.code == "unsafe_setup_target"


def test_v2_setup_rejects_runtime_binary_drift(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in local_setup.CONFIG_PATHS:
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
    for name in ("agent-hubd", "agent-hub-mcp"):
        executable = repo / ".venv/bin" / name
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o700)
    proposal = plan_setup(
        repo,
        launch_agents_dir=tmp_path / "LaunchAgents",
        state_root=tmp_path / "state",
    )
    (repo / ".venv/bin/agent-hubd").write_text("#!/bin/sh\nexit 1\n")

    with pytest.raises(HubV2Error) as error:
        apply_setup(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            activate=False,
        )

    assert error.value.code == "runtime_digest_conflict"


def test_v2_setup_activation_failure_rolls_back_host_and_launch_agent(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in local_setup.CONFIG_PATHS:
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
    for name in ("agent-hubd", "agent-hub-mcp"):
        executable = repo / ".venv/bin" / name
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o700)
    launch_dir = tmp_path / "LaunchAgents"
    proposal = plan_setup(
        repo,
        launch_agents_dir=launch_dir,
        state_root=tmp_path / "state",
    )
    monkeypatch.setattr(
        "agent_hub.v2.setup.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(HubV2Error) as error:
        apply_setup(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            activate=True,
        )

    assert error.value.code == "setup_activation_failed"
    assert not (launch_dir / "com.agent-hub.daemon.plist").exists()
    assert not (repo / ".codex/config.toml").exists()
