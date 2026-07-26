from __future__ import annotations

from agent_hub import local_setup
from agent_hub.v2.migrate import apply_v1_import, plan_v1_import
from agent_hub.v2.setup import apply_setup, plan_setup
from agent_hub.v2.store import HubStore


def test_local_setup_can_render_v2_bridge_without_changing_v1_default(tmp_path):
    for relative in local_setup.CONFIG_PATHS:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
    v1 = local_setup.plan_setup(tmp_path)
    v2 = local_setup.plan_setup(tmp_path, hub_executable="agent-hub-v2-mcp")

    v1_codex = next(
        item.rendered for item in v1.changes if item.relative_path == ".codex/config.toml"
    )
    v2_codex = next(
        item.rendered for item in v2.changes if item.relative_path == ".codex/config.toml"
    )
    assert b"agent-hub-mcp" in v1_codex
    assert b"agent-hub-v2-mcp" not in v1_codex
    assert b"agent-hub-v2-mcp" in v2_codex


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


def test_v1_metadata_import_is_idempotent_and_source_is_unchanged(tmp_path):
    source = tmp_path / "runs"
    source.mkdir()
    run = source / "abc.json"
    run.write_text(
        '{"run_id":"abc","run_kind":"adaptive","status":"completed",'
        '"state_schema_version":2,"prompt":"private"}'
    )
    original = run.read_bytes()
    plan = plan_v1_import(source)
    store = HubStore(tmp_path / "state.sqlite3")

    first = apply_v1_import(store, plan=plan, plan_sha256=plan["plan_sha256"])
    second = apply_v1_import(store, plan=plan, plan_sha256=plan["plan_sha256"])

    assert first["imported"] == 1
    assert second["imported"] == 1
    assert run.read_bytes() == original
    assert '"prompt"' not in str(first)
