"""Read-only inspection and import planning for v1 JSON run files."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import stat
from typing import Any

from .contracts import RUN_SCHEMA
from .errors import HubV2Error
from .store import HubStore

MAX_V1_RUN_BYTES = 8 * 1024 * 1024
MAX_V1_RUNS = 2_000


def _safe_regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_nlink == 1
        and info.st_size <= MAX_V1_RUN_BYTES
    )


def plan_v1_import(source_dir: str | Path) -> dict[str, Any]:
    source = Path(source_dir).expanduser()
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise HubV2Error(
            "v1_source_unavailable",
            "The v1 run directory does not exist.",
            scope="migration",
        ) from exc
    if not source.is_dir():
        raise HubV2Error(
            "v1_source_unavailable",
            "The v1 run source must be a directory.",
            scope="migration",
        )
    entries: list[dict[str, Any]] = []
    skipped = 0
    for path in sorted(source.glob("*.json"))[:MAX_V1_RUNS]:
        if not _safe_regular_file(path):
            skipped += 1
            continue
        try:
            raw = path.read_bytes()
            parsed = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            skipped += 1
            continue
        if not isinstance(parsed, dict):
            skipped += 1
            continue
        run_id = parsed.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            skipped += 1
            continue
        entries.append(
            {
                "source_name": path.name,
                "source_sha256": sha256(raw).hexdigest(),
                "source_bytes": len(raw),
                "run_id": run_id[:128],
                "run_kind": str(parsed.get("run_kind") or "unknown")[:64],
                "status": str(parsed.get("status") or "unknown")[:64],
                "source_schema_version": parsed.get("state_schema_version"),
                "target_schema": RUN_SCHEMA,
                "mode": "archive_metadata_only",
            }
        )
    plan = {
        "schema": "agent_hub_v1_import_plan_v1",
        "source_dir": str(source),
        "read_only": True,
        "entries": entries,
        "skipped": skipped,
    }
    plan["plan_sha256"] = sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return plan


def apply_v1_import(
    store: HubStore,
    *,
    plan: dict[str, Any],
    plan_sha256: str,
) -> dict[str, Any]:
    if plan.get("plan_sha256") != plan_sha256:
        raise HubV2Error(
            "proposal_digest_conflict",
            "The v1 import plan digest does not match.",
            scope="migration",
        )
    source = Path(str(plan.get("source_dir") or "")).expanduser()
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise HubV2Error(
            "v1_source_unavailable",
            "The v1 run directory does not exist.",
            scope="migration",
        ) from exc
    receipts = []
    for entry in plan.get("entries", []):
        if not isinstance(entry, dict):
            raise HubV2Error(
                "invalid_import_entry",
                "The v1 import plan contains an invalid entry.",
                scope="migration",
            )
        path = source / str(entry.get("source_name") or "")
        if not _safe_regular_file(path):
            raise HubV2Error(
                "v1_source_changed",
                "A v1 source run changed after planning.",
                scope="migration",
                retryable=True,
            )
        if sha256(path.read_bytes()).hexdigest() != entry.get("source_sha256"):
            raise HubV2Error(
                "v1_source_changed",
                "A v1 source run changed after planning.",
                scope="migration",
                retryable=True,
            )
        receipts.append(store.record_legacy_import(entry))
    return {
        "schema": "agent_hub_v1_import_result_v1",
        "success": True,
        "source_dir": str(source),
        "imported": len(receipts),
        "receipts": receipts,
        "source_files_modified": False,
    }
