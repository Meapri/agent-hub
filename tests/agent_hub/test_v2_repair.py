from __future__ import annotations

from hashlib import sha256
import sqlite3

import pytest

from agent_hub.v2.contracts import canonical_json
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.repair import apply_repair, plan_repair
from agent_hub.v2.store import STORE_SCHEMA_VERSION, HubStore


def _healthy_store(path, *, schema_version: int = STORE_SCHEMA_VERSION):
    HubStore(path).health()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(schema_version),),
        )


def test_repair_ignores_newer_but_schema_incompatible_backup(tmp_path):
    state_db = tmp_path / "state.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    compatible = backups / "compatible.sqlite3"
    incompatible = backups / "newer-incompatible.sqlite3"
    _healthy_store(compatible)
    _healthy_store(incompatible, schema_version=STORE_SCHEMA_VERSION - 1)
    incompatible.touch()
    state_db.write_bytes(b"corrupt")

    proposal = plan_repair(state_db)

    assert proposal["required_schema_version"] == STORE_SCHEMA_VERSION
    assert proposal["actions"] == [
        {
            "type": "restore_store_backup",
            "backup_path": str(compatible),
            "backup_sha256": proposal["actions"][0]["backup_sha256"],
            "schema_version": STORE_SCHEMA_VERSION,
            "requires_daemon_stopped": True,
        }
    ]


def test_repair_rechecks_backup_schema_before_restore(tmp_path):
    state_db = tmp_path / "state.sqlite3"
    backup = tmp_path / "backups" / "compatible.sqlite3"
    backup.parent.mkdir()
    _healthy_store(backup)
    state_db.write_bytes(b"corrupt")
    proposal = plan_repair(state_db)

    with sqlite3.connect(backup) as connection:
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(STORE_SCHEMA_VERSION - 1),),
        )
    proposal["actions"][0]["backup_sha256"] = sha256(backup.read_bytes()).hexdigest()
    unsigned = {key: value for key, value in proposal.items() if key != "proposal_sha256"}
    proposal["proposal_sha256"] = sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()

    with pytest.raises(HubV2Error) as raised:
        apply_repair(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
        )

    assert raised.value.code == "repair_source_conflict"
    assert raised.value.safe_details == {
        "required_schema_version": STORE_SCHEMA_VERSION,
        "backup_schema_version": STORE_SCHEMA_VERSION - 1,
    }


def test_repair_refuses_store_replacement_while_daemon_is_active(tmp_path, monkeypatch):
    state_db = tmp_path / "state.sqlite3"
    backup = tmp_path / "backups" / "compatible.sqlite3"
    backup.parent.mkdir()
    _healthy_store(backup)
    state_db.write_bytes(b"corrupt")
    proposal = plan_repair(state_db)
    monkeypatch.setattr(
        "agent_hub.v2.repair._daemon_socket_active",
        lambda _path: True,
    )

    with pytest.raises(HubV2Error) as raised:
        apply_repair(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
        )

    assert raised.value.code == "repair_daemon_active"
    assert raised.value.retryable is True
