from __future__ import annotations

from pathlib import Path
import plistlib

from agent_hub.v2.release import apply_switch, plan_rollback, plan_update


def _executable(root: Path):
    executable = root / "bin/agent-hubd"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    return executable


def _plist(path: Path, executable: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.agent-hub.daemon",
                "ProgramArguments": [str(executable), "--state-db", "/tmp/state"],
            }
        )
    )


def test_update_and_rollback_switch_are_digest_fenced(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_hub.v2.release._candidate_health",
        lambda _after: {"success": True, "check": "fixture"},
    )
    old = _executable(tmp_path / "old")
    new = _executable(tmp_path / "new")
    launch = tmp_path / "LaunchAgents/agent.plist"
    rollback = tmp_path / "rollback/agent.plist"
    _plist(launch, old)

    update = plan_update(tmp_path / "new", launch_agent_path=launch)
    applied = apply_switch(
        update,
        proposal_sha256=update["proposal_sha256"],
        activate=False,
        rollback_path=rollback,
    )

    assert applied["active_executable"] == str(new)
    assert plistlib.loads(launch.read_bytes())["ProgramArguments"][0] == str(new)
    assert rollback.exists()

    rollback_plan = plan_rollback(
        launch_agent_path=launch,
        rollback_path=rollback,
    )
    restored = apply_switch(
        rollback_plan,
        proposal_sha256=rollback_plan["proposal_sha256"],
        activate=False,
        rollback_path=rollback,
    )
    assert restored["active_executable"] == str(old)
    assert plistlib.loads(launch.read_bytes())["ProgramArguments"][0] == str(old)
