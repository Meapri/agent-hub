from __future__ import annotations

from pathlib import Path
import plistlib
import sqlite3
from types import SimpleNamespace

import pytest

from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.release import (
    _restore_database_snapshot,
    apply_switch,
    plan_rollback,
    plan_update,
)
from agent_hub.v2.store import HubStore


def _executable(root: Path):
    executable = root / "bin/agent-hubd"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    return executable


def _plist(path: Path, executable: Path, state_db: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.agent-hub.daemon",
                "ProgramArguments": [str(executable), "--state-db", str(state_db)],
            }
        )
    )


def test_update_and_rollback_switch_are_digest_fenced(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_hub.v2.release._candidate_health",
        lambda _after, *, source_state_db: {
            "success": True,
            "check": "fixture",
            "source_state_db": str(source_state_db),
        },
    )
    old = _executable(tmp_path / "old")
    new = _executable(tmp_path / "new")
    launch = tmp_path / "LaunchAgents/agent.plist"
    rollback = tmp_path / "rollback/agent.plist"
    state_db = tmp_path / "state/state.sqlite3"
    HubStore(state_db)
    _plist(launch, old, state_db)

    update = plan_update(
        tmp_path / "new",
        launch_agent_path=launch,
        rollback_path=rollback,
    )
    applied = apply_switch(
        update,
        proposal_sha256=update["proposal_sha256"],
        activate=False,
        rollback_path=rollback,
    )

    assert applied["active_executable"] == str(new)
    assert plistlib.loads(launch.read_bytes())["ProgramArguments"][0] == str(new)
    assert rollback.exists()
    assert Path(str(rollback) + ".metadata.json").exists()
    assert Path(str(rollback) + ".state.sqlite3").exists()
    assert applied["candidate_health"]["source_state_db"] == str(state_db)

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
    assert restored["database"]["restore_applied"] is False


def test_schema_rollback_requires_coordinated_daemon_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_hub.v2.release._candidate_health",
        lambda _after, *, source_state_db: {
            "success": True,
            "source_state_db": str(source_state_db),
        },
    )
    old = _executable(tmp_path / "old")
    _executable(tmp_path / "new")
    launch = tmp_path / "LaunchAgents/agent.plist"
    rollback = tmp_path / "rollback/agent.plist"
    state_db = tmp_path / "state/state.sqlite3"
    HubStore(state_db)
    _plist(launch, old, state_db)
    update = plan_update(
        tmp_path / "new",
        launch_agent_path=launch,
        rollback_path=rollback,
    )
    apply_switch(
        update,
        proposal_sha256=update["proposal_sha256"],
        activate=False,
        rollback_path=rollback,
    )
    with sqlite3.connect(state_db) as connection:
        connection.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")

    proposal = plan_rollback(
        launch_agent_path=launch,
        rollback_path=rollback,
    )
    assert proposal["database_restore_required"] is True
    with pytest.raises(HubV2Error) as error:
        apply_switch(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            activate=False,
            rollback_path=rollback,
        )
    assert error.value.code == "rollback_requires_activation"


def test_missing_live_database_requires_snapshot_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_hub.v2.release._candidate_health",
        lambda _after, *, source_state_db: {
            "success": True,
            "source_state_db": str(source_state_db),
        },
    )
    old = _executable(tmp_path / "old")
    _executable(tmp_path / "new")
    launch = tmp_path / "LaunchAgents/agent.plist"
    rollback = tmp_path / "rollback/agent.plist"
    state_db = tmp_path / "state/state.sqlite3"
    HubStore(state_db)
    _plist(launch, old, state_db)
    update = plan_update(
        tmp_path / "new",
        launch_agent_path=launch,
        rollback_path=rollback,
    )
    apply_switch(
        update,
        proposal_sha256=update["proposal_sha256"],
        activate=False,
        rollback_path=rollback,
    )
    state_db.unlink()

    proposal = plan_rollback(
        launch_agent_path=launch,
        rollback_path=rollback,
    )

    assert proposal["database_restore_required"] is True
    with pytest.raises(HubV2Error) as error:
        apply_switch(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            activate=False,
            rollback_path=rollback,
        )
    assert error.value.code == "rollback_requires_activation"


def test_database_restore_removes_incompatible_wal_sidecars(tmp_path):
    source = tmp_path / "source/state.sqlite3"
    destination = tmp_path / "destination/state.sqlite3"
    HubStore(source)
    HubStore(destination)
    wal = Path(f"{destination}-wal")
    shm = Path(f"{destination}-shm")
    wal.write_bytes(b"stale wal")
    shm.write_bytes(b"stale shm")

    restored = _restore_database_snapshot(source, destination)

    assert restored["schema_version"] == 7
    assert destination.exists()
    assert not wal.exists()
    assert not shm.exists()


def test_release_plan_rejects_public_field_tampering(tmp_path):
    old = _executable(tmp_path / "old")
    _executable(tmp_path / "new")
    launch = tmp_path / "LaunchAgents/agent.plist"
    state_db = tmp_path / "state/state.sqlite3"
    HubStore(state_db)
    _plist(launch, old, state_db)
    proposal = plan_update(tmp_path / "new", launch_agent_path=launch)
    proposal["rollback_slot"] = str(tmp_path / "unreviewed/rollback.plist")

    with pytest.raises(HubV2Error) as error:
        apply_switch(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            activate=False,
            rollback_path=proposal["rollback_slot"],
        )
    assert error.value.code == "proposal_digest_conflict"


def test_release_apply_rejects_candidate_binary_drift(tmp_path):
    old = _executable(tmp_path / "old")
    candidate = _executable(tmp_path / "new")
    launch = tmp_path / "LaunchAgents/agent.plist"
    state_db = tmp_path / "state/state.sqlite3"
    HubStore(state_db)
    _plist(launch, old, state_db)
    proposal = plan_update(tmp_path / "new", launch_agent_path=launch)
    candidate.write_text("#!/bin/sh\nexit 1\n")

    with pytest.raises(HubV2Error) as error:
        apply_switch(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            activate=False,
        )

    assert error.value.code == "release_candidate_conflict"


def test_schema_restore_refuses_to_run_when_daemon_cannot_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_hub.v2.release._candidate_health",
        lambda _after, *, source_state_db: {"success": True},
    )
    monkeypatch.setattr("agent_hub.v2.release._wait_installed_stopped", lambda _before: False)
    monkeypatch.setattr(
        "agent_hub.v2.release.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )
    old = _executable(tmp_path / "old")
    _executable(tmp_path / "new")
    launch = tmp_path / "LaunchAgents/agent.plist"
    rollback = tmp_path / "rollback/agent.plist"
    state_db = tmp_path / "state/state.sqlite3"
    HubStore(state_db)
    _plist(launch, old, state_db)
    update = plan_update(
        tmp_path / "new",
        launch_agent_path=launch,
        rollback_path=rollback,
    )
    apply_switch(
        update,
        proposal_sha256=update["proposal_sha256"],
        activate=False,
        rollback_path=rollback,
    )
    with sqlite3.connect(state_db) as connection:
        connection.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
    proposal = plan_rollback(launch_agent_path=launch, rollback_path=rollback)

    with pytest.raises(HubV2Error) as error:
        apply_switch(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            activate=True,
            rollback_path=rollback,
        )

    assert error.value.code == "release_daemon_stop_failed"
    assert plistlib.loads(launch.read_bytes())["ProgramArguments"][0] == str(
        tmp_path / "new/bin/agent-hubd"
    )
    assert (
        sqlite3.connect(state_db)
        .execute("SELECT value FROM meta WHERE key = 'schema_version'")
        .fetchone()[0]
        == "8"
    )


def test_restore_exception_recovers_previous_database_and_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_hub.v2.release._candidate_health",
        lambda _after, *, source_state_db: {"success": True},
    )
    monkeypatch.setattr("agent_hub.v2.release._wait_installed_stopped", lambda _payload: True)
    monkeypatch.setattr("agent_hub.v2.release._wait_installed_health", lambda _payload: True)
    monkeypatch.setattr(
        "agent_hub.v2.release.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    old = _executable(tmp_path / "old")
    new = _executable(tmp_path / "new")
    launch = tmp_path / "LaunchAgents/agent.plist"
    rollback = tmp_path / "rollback/agent.plist"
    rollback_db = Path(str(rollback) + ".state.sqlite3")
    state_db = tmp_path / "state/state.sqlite3"
    HubStore(state_db)
    _plist(launch, old, state_db)
    update = plan_update(
        tmp_path / "new",
        launch_agent_path=launch,
        rollback_path=rollback,
    )
    apply_switch(
        update,
        proposal_sha256=update["proposal_sha256"],
        activate=False,
        rollback_path=rollback,
    )
    with sqlite3.connect(state_db) as connection:
        connection.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
    proposal = plan_rollback(launch_agent_path=launch, rollback_path=rollback)
    real_restore = _restore_database_snapshot

    def fail_planned_restore(source, destination):
        if source == rollback_db:
            raise HubV2Error(
                "fixture_restore_failed",
                "The fixture restore failed.",
                scope="release",
            )
        return real_restore(source, destination)

    monkeypatch.setattr(
        "agent_hub.v2.release._restore_database_snapshot",
        fail_planned_restore,
    )

    with pytest.raises(HubV2Error) as error:
        apply_switch(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            activate=True,
            rollback_path=rollback,
        )

    assert error.value.code == "release_activation_failed"
    assert error.value.safe_details == {
        "failure_stage": "database_restore",
        "recovery_stage": "complete",
    }
    assert plistlib.loads(launch.read_bytes())["ProgramArguments"][0] == str(new)
    assert (
        sqlite3.connect(state_db)
        .execute("SELECT value FROM meta WHERE key = 'schema_version'")
        .fetchone()[0]
        == "8"
    )
    assert not Path(str(rollback) + ".emergency.state.sqlite3").exists()


def test_candidate_bootstrap_failure_recovers_previous_install(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_hub.v2.release._candidate_health",
        lambda _after, *, source_state_db: {"success": True},
    )
    monkeypatch.setattr("agent_hub.v2.release._wait_installed_stopped", lambda _payload: True)
    monkeypatch.setattr("agent_hub.v2.release._wait_installed_health", lambda _payload: True)
    returncodes = iter((0, 1, 0, 0))
    monkeypatch.setattr(
        "agent_hub.v2.release.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=next(returncodes)),
    )
    old = _executable(tmp_path / "old")
    new = _executable(tmp_path / "new")
    launch = tmp_path / "LaunchAgents/agent.plist"
    rollback = tmp_path / "rollback/agent.plist"
    state_db = tmp_path / "state/state.sqlite3"
    HubStore(state_db)
    _plist(launch, old, state_db)
    update = plan_update(
        tmp_path / "new",
        launch_agent_path=launch,
        rollback_path=rollback,
    )
    apply_switch(
        update,
        proposal_sha256=update["proposal_sha256"],
        activate=False,
        rollback_path=rollback,
    )
    with sqlite3.connect(state_db) as connection:
        connection.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
    proposal = plan_rollback(launch_agent_path=launch, rollback_path=rollback)

    with pytest.raises(HubV2Error) as error:
        apply_switch(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            activate=True,
            rollback_path=rollback,
        )

    assert error.value.code == "release_activation_failed"
    assert error.value.safe_details == {
        "failure_stage": "candidate_bootstrap",
        "recovery_stage": "complete",
    }
    assert plistlib.loads(launch.read_bytes())["ProgramArguments"][0] == str(new)
    assert (
        sqlite3.connect(state_db)
        .execute("SELECT value FROM meta WHERE key = 'schema_version'")
        .fetchone()[0]
        == "8"
    )


def test_failed_recovery_is_reported_without_claiming_rollback(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_hub.v2.release._candidate_health",
        lambda _after, *, source_state_db: {"success": True},
    )
    monkeypatch.setattr("agent_hub.v2.release._wait_installed_stopped", lambda _payload: True)
    monkeypatch.setattr(
        "agent_hub.v2.release.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )
    old = _executable(tmp_path / "old")
    new = _executable(tmp_path / "new")
    launch = tmp_path / "LaunchAgents/agent.plist"
    rollback = tmp_path / "rollback/agent.plist"
    rollback_db = Path(str(rollback) + ".state.sqlite3")
    state_db = tmp_path / "state/state.sqlite3"
    HubStore(state_db)
    _plist(launch, old, state_db)
    update = plan_update(
        tmp_path / "new",
        launch_agent_path=launch,
        rollback_path=rollback,
    )
    apply_switch(
        update,
        proposal_sha256=update["proposal_sha256"],
        activate=False,
        rollback_path=rollback,
    )
    with sqlite3.connect(state_db) as connection:
        connection.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
    proposal = plan_rollback(launch_agent_path=launch, rollback_path=rollback)
    real_restore = _restore_database_snapshot

    def fail_planned_restore(source, destination):
        if source == rollback_db:
            raise HubV2Error(
                "fixture_restore_failed",
                "The fixture restore failed.",
                scope="release",
            )
        return real_restore(source, destination)

    monkeypatch.setattr(
        "agent_hub.v2.release._restore_database_snapshot",
        fail_planned_restore,
    )

    with pytest.raises(HubV2Error) as error:
        apply_switch(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            activate=True,
            rollback_path=rollback,
        )

    assert error.value.code == "release_recovery_failed"
    assert error.value.retryable is False
    assert error.value.safe_details == {
        "failure_stage": "database_restore",
        "recovery_stage": "daemon_restart",
        "emergency_snapshot_preserved": True,
    }
    assert error.value.next_action == {
        "type": "local_cli",
        "command": "agent-hub doctor",
    }
    assert plistlib.loads(launch.read_bytes())["ProgramArguments"][0] == str(new)
    assert (
        sqlite3.connect(state_db)
        .execute("SELECT value FROM meta WHERE key = 'schema_version'")
        .fetchone()[0]
        == "8"
    )
    emergency = Path(str(rollback) + ".emergency.state.sqlite3")
    assert (
        sqlite3.connect(emergency)
        .execute("SELECT value FROM meta WHERE key = 'schema_version'")
        .fetchone()[0]
        == "8"
    )

    rollback_files = {
        path: path.read_bytes()
        for path in (
            rollback,
            Path(str(rollback) + ".metadata.json"),
            Path(str(rollback) + ".state.sqlite3"),
        )
    }
    _executable(tmp_path / "third")
    blocked_update = plan_update(
        tmp_path / "third",
        launch_agent_path=launch,
        rollback_path=rollback,
    )
    with pytest.raises(HubV2Error) as pending:
        apply_switch(
            blocked_update,
            proposal_sha256=blocked_update["proposal_sha256"],
            activate=True,
            rollback_path=rollback,
        )

    assert pending.value.code == "release_recovery_pending"
    assert all(path.read_bytes() == content for path, content in rollback_files.items())
