"""Digest-fenced local repair planning and application."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Mapping

from .contracts import canonical_json
from .errors import HubV2Error
from .store import HubStore


def _file_sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _state_sha(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = sha256()
    for candidate in (path, Path(f"{path}-wal")):
        if candidate.exists():
            digest.update(candidate.name.encode("utf-8"))
            digest.update(candidate.read_bytes())
    return digest.hexdigest()


def _integrity(path: Path) -> str:
    if not path.exists():
        return "missing"
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return str(connection.execute("PRAGMA quick_check").fetchone()[0])
    except sqlite3.Error:
        return "unreadable"
    finally:
        connection.close()


def plan_repair(state_db: str | Path) -> dict[str, Any]:
    path = Path(state_db).expanduser().resolve(strict=False)
    integrity = _integrity(path)
    actions: list[dict[str, Any]] = []
    if integrity not in {"ok", "missing"}:
        backups = sorted(
            (path.parent / "backups").glob("*.sqlite3"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        valid = next((item for item in backups if _integrity(item) == "ok"), None)
        if valid is not None:
            actions.append(
                {
                    "type": "restore_store_backup",
                    "backup_path": str(valid),
                    "backup_sha256": _file_sha(valid),
                }
            )
    elif integrity == "ok":
        actions.extend(
            [
                {"type": "aggregate_routing_history"},
                {"type": "prune_expired_artifacts"},
            ]
        )
    proposal = {
        "schema": "agent_hub_repair_plan_v1",
        "state_db": str(path),
        "before_sha256": _state_sha(path),
        "integrity": integrity,
        "actions": actions,
    }
    proposal["proposal_sha256"] = sha256(
        canonical_json(proposal).encode("utf-8")
    ).hexdigest()
    return proposal


def apply_repair(
    proposal: Mapping[str, Any],
    *,
    proposal_sha256: str,
) -> dict[str, Any]:
    unsigned = dict(proposal)
    embedded = str(unsigned.pop("proposal_sha256", ""))
    calculated = sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    if embedded != calculated or embedded != proposal_sha256:
        raise HubV2Error(
            "proposal_digest_conflict",
            "The repair proposal digest does not match.",
            scope="repair",
        )
    path = Path(str(proposal.get("state_db") or "")).expanduser().resolve(strict=False)
    current_sha = _state_sha(path)
    if current_sha != proposal.get("before_sha256"):
        raise HubV2Error(
            "repair_target_conflict",
            "The store changed after repair planning.",
            scope="repair",
            retryable=True,
        )
    results: list[dict[str, Any]] = []
    for action in proposal.get("actions") or []:
        action_type = str(action.get("type") or "")
        if action_type == "restore_store_backup":
            backup = Path(str(action.get("backup_path") or "")).resolve(strict=True)
            if _file_sha(backup) != action.get("backup_sha256") or _integrity(backup) != "ok":
                raise HubV2Error(
                    "repair_source_conflict",
                    "The selected backup changed or is not healthy.",
                    scope="repair",
                )
            safety = path.parent / "backups" / (
                f"pre-repair-{sha256(os.urandom(32)).hexdigest()[:12]}.sqlite3"
            )
            safety.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.exists():
                shutil.copy2(path, safety)
                os.chmod(safety, 0o600)
            for companion in (Path(f"{path}-wal"), Path(f"{path}-shm")):
                try:
                    companion.unlink()
                except FileNotFoundError:
                    pass
            shutil.copy2(backup, path)
            os.chmod(path, 0o600)
            results.append({"type": action_type, "restored": True})
        elif action_type == "aggregate_routing_history":
            deleted = HubStore(path).prune_routing_details()
            results.append({"type": action_type, "detail_rows_aggregated": deleted})
        elif action_type == "prune_expired_artifacts":
            results.append({"type": action_type, **HubStore(path).prune_expired_artifacts()})
        else:
            raise HubV2Error(
                "unsupported_repair_action",
                "The repair plan contains an unsupported action.",
                scope="repair",
            )
    return {
        "schema": "agent_hub_repair_result_v1",
        "success": True,
        "actions": results,
        "integrity": _integrity(path),
    }
