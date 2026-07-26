"""Transactional SQLite WAL store for Agent Hub v2.

The store is deliberately the only writer of run revisions and event cursors.
Provider output and prompts belong in encrypted artifacts, not event rows.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping

from .contracts import (
    ARTIFACT_SCHEMA,
    EVENT_SCHEMA,
    ROUTING_DECISION_SCHEMA,
    RUN_SCHEMA,
    RUN_STATUSES,
    canonical_json,
    canonical_project_root,
    digest_json,
    require_identifier,
    require_non_negative_int,
    safe_usage,
)
from .errors import HubV2Error

STORE_SCHEMA_VERSION = 3
DEFAULT_STATE_DIR = Path("~/.agent-hub").expanduser()
DEFAULT_DB_NAME = "state.sqlite3"
MAX_EVENT_LIMIT = 100
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_SAFE_STRING = 256
_SAFE_EVENT_FIELDS = frozenset(
    {
        "provider",
        "model",
        "capability",
        "step_id",
        "reason_code",
        "error_code",
        "retryable",
        "elapsed_ms",
        "prompt_chars",
        "prompt_sha256",
        "result_chars",
        "result_sha256",
        "usage",
        "previous_status",
        "routing_mode",
    }
)


@dataclass(frozen=True)
class RunClaim:
    run_id: str
    claim_token: str
    revision: int
    lease_expires_at: float
    run: dict[str, Any]


def _safe_state_dir(path: Path) -> Path:
    expanded = Path(os.path.abspath(path.expanduser()))
    if ".." in expanded.parts:
        raise HubV2Error(
            "unsafe_state_path",
            "The state directory must not contain '..'.",
            scope="store",
        )
    filesystem_root = Path(expanded.anchor).resolve()
    if expanded.resolve(strict=False) in {filesystem_root, Path.home().resolve()}:
        raise HubV2Error(
            "unsafe_state_path",
            "The state directory is too broad.",
            scope="store",
        )
    current = Path(expanded.anchor)
    for part in expanded.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise HubV2Error(
                "unsafe_state_path",
                "The state directory must not contain symlinks.",
                scope="store",
            )
        if not stat.S_ISDIR(info.st_mode):
            raise HubV2Error(
                "unsafe_state_path",
                "A state path component is not a directory.",
                scope="store",
            )
    expanded.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(expanded, 0o700)
    return expanded.resolve(strict=True)


def _json_object(value: str | bytes | None) -> dict[str, Any]:
    if value is None:
        return {}
    parsed = json.loads(value)
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_list(value: str | bytes | None) -> list[Any]:
    if value is None:
        return []
    parsed = json.loads(value)
    return list(parsed) if isinstance(parsed, list) else []


def _safe_event_details(details: Mapping[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {}
    safe: dict[str, Any] = {}
    for key, value in list(details.items())[:32]:
        if key not in _SAFE_EVENT_FIELDS:
            continue
        if key == "usage":
            usage = safe_usage(value)
            if usage:
                safe[key] = usage
        elif key in {"retryable"}:
            if isinstance(value, bool):
                safe[key] = value
        elif key in {"elapsed_ms", "prompt_chars", "result_chars"}:
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                safe[key] = value
        elif key in {"prompt_sha256", "result_sha256"}:
            text = str(value or "")
            if len(text) == 64 and all(char in "0123456789abcdef" for char in text):
                safe[key] = text
        else:
            text = str(value or "")[:MAX_SAFE_STRING]
            safe[key] = text if text else "redacted"
    return safe


class HubStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        requested = (
            Path(path).expanduser()
            if path is not None
            else DEFAULT_STATE_DIR / DEFAULT_DB_NAME
        )
        directory = _safe_state_dir(requested.parent)
        self.path = directory / requested.name
        self._clock = clock
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection

    @contextmanager
    def _transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _migration_backup_if_needed(self) -> Path | None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        source = sqlite3.connect(self.path)
        source.row_factory = sqlite3.Row
        try:
            integrity = str(source.execute("PRAGMA quick_check").fetchone()[0])
            if integrity != "ok":
                raise HubV2Error(
                    "store_integrity_failed",
                    "The local state database failed its integrity check.",
                    scope="store",
                    safe_details={"integrity": integrity[:128]},
                )
            has_meta = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
            ).fetchone()
            if has_meta is None:
                raise HubV2Error(
                    "unsupported_store_schema",
                    "The existing database is not an Agent Hub v2 store.",
                    scope="store",
                )
            row = source.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            current = int(row["value"]) if row is not None else 0
            if current > STORE_SCHEMA_VERSION:
                raise HubV2Error(
                    "unsupported_store_schema",
                    "The local state database is newer than this runtime.",
                    scope="store",
                    safe_details={"current": current, "supported": STORE_SCHEMA_VERSION},
                )
            if current == STORE_SCHEMA_VERSION:
                return None
            backup_dir = _safe_state_dir(self.path.parent / "backups")
            target = backup_dir / (
                f"pre-migration-v{current}-to-v{STORE_SCHEMA_VERSION}-"
                f"{int(self._clock())}-{secrets.token_hex(4)}.sqlite3"
            )
            output = sqlite3.connect(target)
            try:
                source.backup(output)
            finally:
                output.close()
            os.chmod(target, 0o600)
            return target
        finally:
            source.close()

    def _restore_migration_backup(self, backup: Path) -> None:
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
        shutil.copy2(backup, self.path)
        os.chmod(self.path, 0o600)

    def _initialize(self) -> None:
        existed = self.path.exists()
        migration_backup = self._migration_backup_if_needed()
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    schema_name TEXT NOT NULL,
                    project_root TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 0),
                    plan_sha256 TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    policy_revision INTEGER NOT NULL CHECK (policy_revision >= 0),
                    routing_mode TEXT NOT NULL,
                    parent_run_id TEXT,
                    replan_count INTEGER NOT NULL DEFAULT 0 CHECK (replan_count >= 0),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    lease_token_sha256 TEXT,
                    lease_expires_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    archived_at REAL
                );

                CREATE TABLE IF NOT EXISTS steps (
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    step_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 0),
                    provider TEXT,
                    model TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
                    input_artifact_ids TEXT NOT NULL DEFAULT '[]',
                    output_artifact_ids TEXT NOT NULL DEFAULT '[]',
                    checkpoint_state TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (run_id, step_id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_name TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    run_revision INTEGER NOT NULL CHECK (run_revision >= 0),
                    event_type TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run_cursor
                    ON events(run_id, cursor);

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    schema_name TEXT NOT NULL,
                    run_id TEXT REFERENCES runs(run_id) ON DELETE SET NULL,
                    producer_step_id TEXT,
                    content_sha256 TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    sensitivity TEXT NOT NULL,
                    encrypted INTEGER NOT NULL CHECK (encrypted IN (0, 1)),
                    content BLOB,
                    source_refs_json TEXT NOT NULL,
                    verification_json TEXT NOT NULL,
                    retention TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    delete_after REAL,
                    export_count INTEGER NOT NULL DEFAULT 0 CHECK (export_count >= 0)
                );

                CREATE TABLE IF NOT EXISTS artifact_exports (
                    export_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id)
                        ON DELETE CASCADE,
                    destination_alias TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    exported_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provenance_edges (
                    source_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id)
                        ON DELETE CASCADE,
                    target_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id)
                        ON DELETE CASCADE,
                    relation TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(source_artifact_id, target_artifact_id, relation)
                );

                CREATE TABLE IF NOT EXISTS feedback (
                    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    step_id TEXT NOT NULL DEFAULT '',
                    expected_revision INTEGER NOT NULL CHECK (expected_revision >= 0),
                    outcome TEXT NOT NULL,
                    rating INTEGER,
                    signal_weight REAL NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(run_id, step_id, expected_revision, outcome)
                );

                CREATE TABLE IF NOT EXISTS routing_decisions (
                    decision_id TEXT PRIMARY KEY,
                    schema_name TEXT NOT NULL,
                    run_id TEXT REFERENCES runs(run_id) ON DELETE SET NULL,
                    step_id TEXT,
                    routing_mode TEXT NOT NULL,
                    selected_provider TEXT,
                    planner_provider TEXT,
                    candidates_json TEXT NOT NULL,
                    score_json TEXT NOT NULL,
                    sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
                    policy_revision INTEGER NOT NULL CHECK (policy_revision >= 0),
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS routing_samples (
                    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    context_sha256 TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT,
                    capability TEXT NOT NULL,
                    success INTEGER NOT NULL CHECK (success IN (0, 1)),
                    quality REAL,
                    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
                    total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
                    signal_weight REAL NOT NULL CHECK (signal_weight > 0),
                    recorded_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS routing_samples_context_provider
                    ON routing_samples(context_sha256, provider, recorded_at);

                CREATE TABLE IF NOT EXISTS routing_daily_aggregates (
                    day INTEGER NOT NULL,
                    context_sha256 TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    capability TEXT NOT NULL,
                    sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
                    success_weight REAL NOT NULL,
                    failure_weight REAL NOT NULL,
                    quality_total REAL NOT NULL,
                    quality_weight REAL NOT NULL,
                    latency_total REAL NOT NULL,
                    latency_weight REAL NOT NULL,
                    tokens_total REAL NOT NULL,
                    tokens_weight REAL NOT NULL,
                    PRIMARY KEY(day, context_sha256, provider, model)
                );

                CREATE TABLE IF NOT EXISTS context_documents (
                    document_id TEXT PRIMARY KEY,
                    project_identity TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    path_alias TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    chars INTEGER NOT NULL CHECK (chars >= 0),
                    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
                    indexed_at REAL NOT NULL,
                    UNIQUE(project_identity, namespace, path_alias)
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS context_fts USING fts5(
                    document_id UNINDEXED,
                    project_identity UNINDEXED,
                    namespace UNINDEXED,
                    path_alias,
                    content,
                    tokenize = 'unicode61'
                );

                CREATE TABLE IF NOT EXISTS legacy_imports (
                    source_sha256 TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    legacy_run_id TEXT NOT NULL,
                    legacy_run_kind TEXT NOT NULL,
                    legacy_status TEXT NOT NULL,
                    source_schema_version INTEGER,
                    imported_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provider_health (
                    provider TEXT PRIMARY KEY,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0
                        CHECK (consecutive_failures >= 0),
                    circuit_open_until REAL,
                    last_error_code TEXT,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provider_verifications (
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    generation_state TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    verified_at REAL NOT NULL,
                    PRIMARY KEY(provider, model)
                );
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if current is None:
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(STORE_SCHEMA_VERSION),),
                )
            elif int(current["value"]) > STORE_SCHEMA_VERSION:
                raise HubV2Error(
                    "unsupported_store_schema",
                    "The local state database needs a supported migration.",
                    scope="store",
                    safe_details={
                        "current": int(current["value"]),
                        "supported": STORE_SCHEMA_VERSION,
                    },
                )
            elif int(current["value"]) < STORE_SCHEMA_VERSION:
                connection.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (str(STORE_SCHEMA_VERSION),),
                )
            connection.execute("COMMIT")
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if integrity != "ok":
                raise HubV2Error(
                    "store_migration_failed",
                    "The migrated database failed its integrity check.",
                    scope="store",
                    safe_details={"integrity": integrity[:128]},
                )
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()
            if migration_backup is not None:
                self._restore_migration_backup(migration_backup)
            raise
        finally:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        os.chmod(self.path, 0o600)
        if not existed:
            self._checkpoint()

    def _checkpoint(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            connection.close()

    def health(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            schema_version = int(
                connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()[0]
            )
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        finally:
            connection.close()
        return {
            "schema": "agent_hub_store_health_v1",
            "ok": integrity == "ok" and schema_version == STORE_SCHEMA_VERSION,
            "integrity": integrity,
            "schema_version": schema_version,
            "journal_mode": journal_mode,
            "path": str(self.path),
        }

    def backup(self, destination: str | Path | None = None) -> dict[str, Any]:
        if destination is None:
            backup_dir = _safe_state_dir(self.path.parent / "backups")
            target = backup_dir / f"state-{int(self._clock())}.sqlite3"
        else:
            target = Path(destination).expanduser()
            _safe_state_dir(target.parent)
        if target.exists():
            raise HubV2Error(
                "backup_exists",
                "The backup target already exists.",
                scope="store",
            )
        source = self._connect()
        try:
            output = sqlite3.connect(target)
            try:
                source.backup(output)
            finally:
                output.close()
        finally:
            source.close()
        os.chmod(target, 0o600)
        return {
            "schema": "agent_hub_store_backup_v1",
            "path": str(target.resolve()),
            "bytes": target.stat().st_size,
            "sha256": sha256(target.read_bytes()).hexdigest(),
        }

    @staticmethod
    def _public_run(row: sqlite3.Row, steps: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema": row["schema_name"],
            "run_id": row["run_id"],
            "project_root": row["project_root"],
            "status": row["status"],
            "revision": row["revision"],
            "plan_sha256": row["plan_sha256"],
            "policy_revision": row["policy_revision"],
            "routing_mode": row["routing_mode"],
            "parent_run_id": row["parent_run_id"],
            "replan_count": row["replan_count"],
            "lease_active": bool(
                row["lease_token_sha256"]
                and row["lease_expires_at"]
                and row["lease_expires_at"] > time.time()
            ),
            "lease_expires_at": row["lease_expires_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "steps": steps,
        }

    @staticmethod
    def _public_step(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "step_id": row["step_id"],
            "capability": row["capability"],
            "status": row["status"],
            "revision": row["revision"],
            "provider": row["provider"],
            "model": row["model"],
            "attempt": row["attempt"],
            "input_artifact_ids": _json_list(row["input_artifact_ids"]),
            "output_artifact_ids": _json_list(row["output_artifact_ids"]),
            "checkpoint": _json_object(row["checkpoint_state"]),
            "updated_at": row["updated_at"],
        }

    def _load_run(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> tuple[sqlite3.Row, list[dict[str, Any]]]:
        run_id = require_identifier(run_id, field="run_id")
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise HubV2Error("run_not_found", "The requested run was not found.", scope="run")
        step_rows = connection.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        return row, [self._public_step(step) for step in step_rows]

    def get_run(self, run_id: str, *, project_root: str | None = None) -> dict[str, Any]:
        connection = self._connect()
        try:
            row, steps = self._load_run(connection, run_id)
        finally:
            connection.close()
        if project_root is not None and row["project_root"] != canonical_project_root(project_root):
            raise HubV2Error(
                "project_scope_mismatch",
                "The run belongs to a different project.",
                scope="run",
            )
        return self._public_run(row, steps)

    def get_run_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        key = require_identifier(idempotency_key, field="idempotency_key")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT run_id FROM runs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            run_row, steps = self._load_run(connection, row["run_id"])
            return self._public_run(run_row, steps)
        finally:
            connection.close()

    def get_plan(self, run_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row, _ = self._load_run(connection, run_id)
            return _json_object(row["plan_json"])
        finally:
            connection.close()

    def create_run(
        self,
        *,
        plan: Mapping[str, Any],
        project_root: str,
        idempotency_key: str,
        parent_run_id: str | None = None,
    ) -> dict[str, Any]:
        key = require_identifier(idempotency_key, field="idempotency_key")
        root = canonical_project_root(project_root)
        plan_sha256 = str(plan.get("plan_sha256") or digest_json(plan))
        if len(plan_sha256) != 64:
            raise HubV2Error("invalid_plan", "plan_sha256 is invalid.", scope="run")
        steps = list(plan.get("steps") or [])
        if not steps:
            raise HubV2Error("invalid_plan", "The plan has no steps.", scope="run")
        now = self._clock()
        run_id = secrets.token_hex(8)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT run_id FROM runs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                row, public_steps = self._load_run(connection, existing["run_id"])
                return self._public_run(row, public_steps)
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, schema_name, project_root, status, revision,
                    plan_sha256, plan_json, policy_revision, routing_mode,
                    parent_run_id, idempotency_key, created_at, updated_at
                ) VALUES(?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    RUN_SCHEMA,
                    root,
                    plan_sha256,
                    canonical_json(plan),
                    int(plan.get("policy_revision", 0)),
                    str(plan.get("routing_mode") or "shadow"),
                    parent_run_id,
                    key,
                    now,
                    now,
                ),
            )
            for step in steps:
                connection.execute(
                    """
                    INSERT INTO steps(
                        run_id, step_id, capability, status, revision, updated_at
                    ) VALUES(?, ?, ?, 'queued', 0, ?)
                    """,
                    (
                        run_id,
                        require_identifier(step["id"], field="step_id"),
                        str(step["capability"]),
                        now,
                    ),
                )
            self._append_event_tx(
                connection,
                run_id=run_id,
                run_revision=0,
                event_type="run_created",
                occurred_at=now,
                details={"routing_mode": str(plan.get("routing_mode") or "shadow")},
            )
            row, public_steps = self._load_run(connection, run_id)
            return self._public_run(row, public_steps)

    def _append_event_tx(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        run_revision: int,
        event_type: str,
        occurred_at: float,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        event = require_identifier(event_type, field="event_type")
        cursor = connection.execute(
            """
            INSERT INTO events(
                schema_name, run_id, run_revision, event_type, occurred_at, details_json
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                EVENT_SCHEMA,
                run_id,
                run_revision,
                event,
                occurred_at,
                canonical_json(_safe_event_details(details)),
            ),
        ).lastrowid
        return int(cursor)

    def events(
        self,
        run_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 50,
        project_root: str | None = None,
    ) -> dict[str, Any]:
        after = require_non_negative_int(after_cursor, field="after_cursor")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_EVENT_LIMIT:
            raise HubV2Error(
                "invalid_request",
                f"limit must be between 1 and {MAX_EVENT_LIMIT}.",
                scope="event",
            )
        connection = self._connect()
        try:
            row, _ = self._load_run(connection, run_id)
            if (
                project_root is not None
                and row["project_root"] != canonical_project_root(project_root)
            ):
                raise HubV2Error(
                    "project_scope_mismatch",
                    "The run belongs to a different project.",
                    scope="event",
                )
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE run_id = ? AND cursor > ?
                ORDER BY cursor
                LIMIT ?
                """,
                (run_id, after, limit + 1),
            ).fetchall()
        finally:
            connection.close()
        has_more = len(rows) > limit
        page = rows[:limit]
        events = [
            {
                "schema": item["schema_name"],
                "cursor": item["cursor"],
                "run_id": item["run_id"],
                "run_revision": item["run_revision"],
                "type": item["event_type"],
                "occurred_at": item["occurred_at"],
                "details": _json_object(item["details_json"]),
            }
            for item in page
        ]
        return {
            "schema": "agent_hub_event_page_v1",
            "events": events,
            "next_cursor": events[-1]["cursor"] if events else after,
            "has_more": has_more,
        }

    def claim_run(
        self,
        run_id: str,
        *,
        expected_revision: int,
        lease_seconds: float = 60.0,
    ) -> RunClaim:
        expected = require_non_negative_int(expected_revision, field="expected_revision")
        if lease_seconds < 1.0 or lease_seconds > 3600.0:
            raise HubV2Error(
                "invalid_request",
                "lease_seconds must be between 1 and 3600.",
                scope="run",
            )
        now = self._clock()
        token = secrets.token_hex(32)
        token_sha = sha256(token.encode("ascii")).hexdigest()
        expires = now + lease_seconds
        with self._transaction() as connection:
            row, _ = self._load_run(connection, run_id)
            if row["revision"] != expected:
                raise HubV2Error(
                    "revision_conflict",
                    "The run revision changed.",
                    scope="run",
                    retryable=True,
                    safe_details={"expected": expected, "current": row["revision"]},
                )
            if row["status"] in {
                "completed",
                "failed",
                "cancelled",
                "archived",
                "outcome_unknown",
            }:
                raise HubV2Error(
                    "run_not_claimable",
                    "The run is already terminal.",
                    scope="run",
                )
            if row["lease_token_sha256"] and row["lease_expires_at"] > now:
                raise HubV2Error(
                    "lease_active",
                    "Another worker currently owns this run.",
                    scope="run",
                    retryable=True,
                    safe_details={"retry_after_seconds": row["lease_expires_at"] - now},
                )
            next_revision = expected + 1
            connection.execute(
                """
                UPDATE runs
                SET status = 'running', revision = ?, lease_token_sha256 = ?,
                    lease_expires_at = ?, updated_at = ?
                WHERE run_id = ? AND revision = ?
                """,
                (next_revision, token_sha, expires, now, run_id, expected),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                run_revision=next_revision,
                event_type="run_claimed",
                occurred_at=now,
            )
            claimed_row, steps = self._load_run(connection, run_id)
            return RunClaim(
                run_id=run_id,
                claim_token=token,
                revision=next_revision,
                lease_expires_at=expires,
                run=self._public_run(claimed_row, steps),
            )

    def finalize_claim(
        self,
        run_id: str,
        *,
        claim_token: str,
        expected_revision: int,
        status: str,
        event_type: str,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        expected = require_non_negative_int(expected_revision, field="expected_revision")
        if status not in RUN_STATUSES:
            raise HubV2Error("invalid_request", "run status is not supported.", scope="run")
        token_sha = sha256(str(claim_token).encode("ascii")).hexdigest()
        now = self._clock()
        with self._transaction() as connection:
            row, _ = self._load_run(connection, run_id)
            if row["revision"] != expected:
                raise HubV2Error(
                    "revision_conflict",
                    "The run revision changed.",
                    scope="run",
                    retryable=True,
                    safe_details={"expected": expected, "current": row["revision"]},
                )
            if row["lease_token_sha256"] != token_sha:
                raise HubV2Error(
                    "lease_lost",
                    "The continuation lease is no longer valid.",
                    scope="run",
                    retryable=True,
                )
            next_revision = expected + 1
            connection.execute(
                """
                UPDATE runs
                SET status = ?, revision = ?, lease_token_sha256 = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ? AND revision = ?
                """,
                (status, next_revision, now, run_id, expected),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                run_revision=next_revision,
                event_type=event_type,
                occurred_at=now,
                details=details,
            )
            updated, steps = self._load_run(connection, run_id)
            return self._public_run(updated, steps)

    def update_step(
        self,
        run_id: str,
        *,
        step_id: str,
        expected_run_revision: int,
        status: str,
        provider: str | None = None,
        model: str | None = None,
        output_artifact_ids: list[str] | None = None,
        checkpoint: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        expected = require_non_negative_int(
            expected_run_revision,
            field="expected_run_revision",
        )
        identifier = require_identifier(step_id, field="step_id")
        now = self._clock()
        with self._transaction() as connection:
            run_row, _ = self._load_run(connection, run_id)
            if run_row["revision"] != expected:
                raise HubV2Error(
                    "revision_conflict",
                    "The run revision changed.",
                    scope="run",
                    retryable=True,
                    safe_details={"expected": expected, "current": run_row["revision"]},
                )
            row = connection.execute(
                "SELECT * FROM steps WHERE run_id = ? AND step_id = ?",
                (run_id, identifier),
            ).fetchone()
            if row is None:
                raise HubV2Error("step_not_found", "The step was not found.", scope="run")
            if status not in {
                "queued",
                "running",
                "completed",
                "failed",
                "cancelled",
                "outcome_unknown",
            }:
                raise HubV2Error(
                    "invalid_request",
                    "The step status is not supported.",
                    scope="run",
                )
            attempt_delta = int(
                status == "running"
                or (row["status"] == "queued" and status == "completed")
            )
            next_revision = expected + 1
            connection.execute(
                """
                UPDATE steps
                SET status = ?, revision = revision + 1, provider = ?, model = ?,
                    attempt = attempt + ?, output_artifact_ids = ?,
                    checkpoint_state = ?, updated_at = ?
                WHERE run_id = ? AND step_id = ?
                """,
                (
                    status,
                    provider,
                    model,
                    attempt_delta,
                    canonical_json(output_artifact_ids or []),
                    canonical_json(dict(checkpoint or {})),
                    now,
                    run_id,
                    identifier,
                ),
            )
            connection.execute(
                "UPDATE runs SET revision = ?, updated_at = ? WHERE run_id = ?",
                (next_revision, now, run_id),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                run_revision=next_revision,
                event_type="step_updated",
                occurred_at=now,
                details={
                    "step_id": identifier,
                    "provider": provider,
                    "model": model,
                    "reason_code": status,
                },
            )
            updated, steps = self._load_run(connection, run_id)
            return self._public_run(updated, steps)

    def cancel_run(self, run_id: str, *, expected_revision: int) -> dict[str, Any]:
        expected = require_non_negative_int(expected_revision, field="expected_revision")
        now = self._clock()
        with self._transaction() as connection:
            row, _ = self._load_run(connection, run_id)
            if row["revision"] != expected:
                raise HubV2Error(
                    "revision_conflict",
                    "The run revision changed.",
                    scope="run",
                    retryable=True,
                    safe_details={"expected": expected, "current": row["revision"]},
                )
            if row["status"] in {"completed", "failed", "cancelled", "archived"}:
                raise HubV2Error(
                    "run_not_cancellable",
                    "The run is already terminal.",
                    scope="run",
                )
            next_revision = expected + 1
            connection.execute(
                """
                UPDATE runs
                SET status = 'cancelled', revision = ?, lease_token_sha256 = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ? AND revision = ?
                """,
                (next_revision, now, run_id, expected),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                run_revision=next_revision,
                event_type="run_cancelled",
                occurred_at=now,
            )
            updated, steps = self._load_run(connection, run_id)
            return self._public_run(updated, steps)

    def replace_pending_plan(
        self,
        run_id: str,
        *,
        expected_revision: int,
        candidate_plan: Mapping[str, Any],
        reason_code: str,
    ) -> dict[str, Any]:
        expected = require_non_negative_int(expected_revision, field="expected_revision")
        candidate_steps = list(candidate_plan.get("steps") or [])
        candidate_by_id = {
            str(step.get("id") or ""): dict(step)
            for step in candidate_steps
            if isinstance(step, Mapping)
        }
        now = self._clock()
        with self._transaction() as connection:
            row, current_steps = self._load_run(connection, run_id)
            if row["revision"] != expected:
                raise HubV2Error(
                    "revision_conflict",
                    "The run revision changed.",
                    scope="run",
                    retryable=True,
                    safe_details={"expected": expected, "current": row["revision"]},
                )
            if row["replan_count"] >= 1:
                raise HubV2Error(
                    "replan_budget_exhausted",
                    "The automatic replan budget is exhausted.",
                    scope="planner",
                )
            if row["status"] not in {"paused", "failed"}:
                raise HubV2Error(
                    "run_not_replannable",
                    "Only paused or failed runs can be replanned.",
                    scope="planner",
                )
            old_plan = _json_object(row["plan_json"])
            old_by_id = {
                str(step.get("id") or ""): dict(step)
                for step in old_plan.get("steps", [])
                if isinstance(step, Mapping)
            }
            completed_ids = {
                step["step_id"] for step in current_steps if step["status"] == "completed"
            }
            for step_id in completed_ids:
                if step_id not in candidate_by_id or candidate_by_id[step_id] != old_by_id.get(
                    step_id
                ):
                    raise HubV2Error(
                        "completed_step_changed",
                        "A replan cannot alter completed steps.",
                        scope="planner",
                    )
            connection.execute(
                "DELETE FROM steps WHERE run_id = ? AND status != 'completed'",
                (run_id,),
            )
            for step in candidate_steps:
                if step["id"] in completed_ids:
                    continue
                connection.execute(
                    """
                    INSERT INTO steps(
                        run_id, step_id, capability, status, revision, updated_at
                    ) VALUES(?, ?, ?, 'queued', 0, ?)
                    """,
                    (run_id, step["id"], step["capability"], now),
                )
            next_revision = expected + 1
            plan_sha = str(candidate_plan.get("plan_sha256") or digest_json(candidate_plan))
            connection.execute(
                """
                UPDATE runs
                SET status = 'queued', revision = ?, plan_sha256 = ?, plan_json = ?,
                    replan_count = replan_count + 1, updated_at = ?
                WHERE run_id = ? AND revision = ?
                """,
                (
                    next_revision,
                    plan_sha,
                    canonical_json(candidate_plan),
                    now,
                    run_id,
                    expected,
                ),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                run_revision=next_revision,
                event_type="run_replanned",
                occurred_at=now,
                details={"reason_code": reason_code},
            )
            updated, steps = self._load_run(connection, run_id)
            return self._public_run(updated, steps)

    def put_artifact(
        self,
        *,
        content: bytes | None,
        media_type: str,
        sensitivity: str,
        encrypted: bool,
        run_id: str | None = None,
        producer_step_id: str | None = None,
        source_refs: list[str] | None = None,
        verification: Mapping[str, Any] | None = None,
        retention: str = "durable_private",
        delete_after: float | None = None,
        content_sha256: str | None = None,
    ) -> dict[str, Any]:
        if content is not None and len(content) > MAX_ARTIFACT_BYTES:
            raise HubV2Error(
                "artifact_too_large",
                "The artifact exceeds the local size limit.",
                scope="artifact",
                safe_details={"maximum_bytes": MAX_ARTIFACT_BYTES},
            )
        if content is not None and not encrypted:
            raise HubV2Error(
                "artifact_encryption_required",
                "Durable artifact content must be encrypted before storage.",
                scope="artifact",
            )
        digest = content_sha256
        if digest is None:
            if content is None:
                raise HubV2Error(
                    "invalid_artifact",
                    "content_sha256 is required when content is omitted.",
                    scope="artifact",
                )
            digest = sha256(content).hexdigest()
        artifact_id = f"art_{secrets.token_hex(12)}"
        now = self._clock()
        with self._transaction() as connection:
            if run_id is not None:
                self._load_run(connection, run_id)
            normalized_refs = list(dict.fromkeys(source_refs or []))
            for source_id in normalized_refs:
                require_identifier(source_id, field="source_ref")
                if connection.execute(
                    "SELECT 1 FROM artifacts WHERE artifact_id = ?",
                    (source_id,),
                ).fetchone() is None:
                    raise HubV2Error(
                        "artifact_source_not_found",
                        "An artifact source reference was not found.",
                        scope="artifact",
                    )
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, schema_name, run_id, producer_step_id,
                    content_sha256, media_type, sensitivity, encrypted, content,
                    source_refs_json, verification_json, retention,
                    created_at, delete_after
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    ARTIFACT_SCHEMA,
                    run_id,
                    producer_step_id,
                    digest,
                    str(media_type)[:128],
                    str(sensitivity)[:32],
                    int(encrypted),
                    content,
                    canonical_json(normalized_refs),
                    canonical_json(dict(verification or {})),
                    retention,
                    now,
                    delete_after,
                ),
            )
            for source_id in normalized_refs:
                connection.execute(
                    """
                    INSERT INTO provenance_edges(
                        source_artifact_id, target_artifact_id, relation, created_at
                    ) VALUES(?, ?, 'derived_from', ?)
                    """,
                    (source_id, artifact_id, now),
                )
        return self.get_artifact(artifact_id, include_content=False)

    def get_artifact(
        self,
        artifact_id: str,
        *,
        include_content: bool = False,
    ) -> dict[str, Any]:
        identifier = require_identifier(artifact_id, field="artifact_id")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?",
                (identifier,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise HubV2Error(
                "artifact_not_found",
                "The requested artifact was not found.",
                scope="artifact",
            )
        result = {
            "schema": row["schema_name"],
            "artifact_id": row["artifact_id"],
            "run_id": row["run_id"],
            "producer_step_id": row["producer_step_id"],
            "content_sha256": row["content_sha256"],
            "media_type": row["media_type"],
            "sensitivity": row["sensitivity"],
            "encrypted": bool(row["encrypted"]),
            "source_refs": _json_list(row["source_refs_json"]),
            "verification": _json_object(row["verification_json"]),
            "retention": row["retention"],
            "created_at": row["created_at"],
            "delete_after": row["delete_after"],
            "export_count": row["export_count"],
        }
        if include_content:
            result["content"] = bytes(row["content"]) if row["content"] is not None else None
        connection = self._connect()
        try:
            result["provenance"] = {
                "sources": [
                    item["source_artifact_id"]
                    for item in connection.execute(
                        """
                        SELECT source_artifact_id FROM provenance_edges
                        WHERE target_artifact_id = ? ORDER BY source_artifact_id
                        """,
                        (identifier,),
                    ).fetchall()
                ],
                "derived": [
                    item["target_artifact_id"]
                    for item in connection.execute(
                        """
                        SELECT target_artifact_id FROM provenance_edges
                        WHERE source_artifact_id = ? ORDER BY target_artifact_id
                        """,
                        (identifier,),
                    ).fetchall()
                ],
            }
            result["exports"] = [
                {
                    "export_id": item["export_id"],
                    "destination_alias": item["destination_alias"],
                    "content_sha256": item["content_sha256"],
                    "exported_at": item["exported_at"],
                }
                for item in connection.execute(
                    """
                    SELECT export_id, destination_alias, content_sha256, exported_at
                    FROM artifact_exports
                    WHERE artifact_id = ? ORDER BY exported_at DESC
                    """,
                    (identifier,),
                ).fetchall()
            ]
        finally:
            connection.close()
        return result

    def record_artifact_export(
        self,
        *,
        artifact_id: str,
        destination_alias: str,
        content_sha256: str,
    ) -> dict[str, Any]:
        identifier = require_identifier(artifact_id, field="artifact_id")
        export_id = f"export_{secrets.token_hex(12)}"
        now = self._clock()
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM artifacts WHERE artifact_id = ?",
                (identifier,),
            ).fetchone() is None:
                raise HubV2Error(
                    "artifact_not_found",
                    "The requested artifact was not found.",
                    scope="artifact",
                )
            connection.execute(
                """
                INSERT INTO artifact_exports(
                    export_id, artifact_id, destination_alias,
                    content_sha256, exported_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    export_id,
                    identifier,
                    str(destination_alias)[:512],
                    content_sha256,
                    now,
                ),
            )
            connection.execute(
                "UPDATE artifacts SET export_count = export_count + 1 WHERE artifact_id = ?",
                (identifier,),
            )
        return {
            "schema": "agent_hub_artifact_export_v1",
            "export_id": export_id,
            "artifact_id": identifier,
            "destination_alias": str(destination_alias)[:512],
            "content_sha256": content_sha256,
            "exported_at": now,
        }

    def prune_expired_artifacts(self) -> dict[str, Any]:
        now = self._clock()
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM artifacts WHERE delete_after IS NOT NULL AND delete_after <= ?",
                (now,),
            )
        return {
            "schema": "agent_hub_artifact_retention_v1",
            "deleted_count": max(0, int(cursor.rowcount)),
            "evaluated_at": now,
        }

    def record_feedback(
        self,
        *,
        run_id: str,
        expected_revision: int,
        outcome: str,
        rating: int | None = None,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        if outcome not in {"accepted", "partial", "rejected", "verified", "failed"}:
            raise HubV2Error(
                "invalid_feedback",
                "feedback outcome is not supported.",
                scope="feedback",
            )
        if rating is not None and (
            isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5
        ):
            raise HubV2Error(
                "invalid_feedback",
                "rating must be between 1 and 5.",
                scope="feedback",
            )
        expected = require_non_negative_int(expected_revision, field="expected_revision")
        weight = 5.0 if rating is not None else (3.0 if outcome in {"verified", "failed"} else 2.0)
        step_key = step_id or ""
        now = self._clock()
        with self._transaction() as connection:
            row, _ = self._load_run(connection, run_id)
            if row["revision"] != expected:
                raise HubV2Error(
                    "revision_conflict",
                    "The run revision changed.",
                    scope="feedback",
                    retryable=True,
                    safe_details={"expected": expected, "current": row["revision"]},
                )
            try:
                feedback_id = connection.execute(
                    """
                    INSERT INTO feedback(
                        run_id, step_id, expected_revision, outcome,
                        rating, signal_weight, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, step_key, expected, outcome, rating, weight, now),
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                raise HubV2Error(
                    "feedback_conflict",
                    "This feedback signal was already recorded.",
                    scope="feedback",
                ) from exc
            self._append_event_tx(
                connection,
                run_id=run_id,
                run_revision=expected,
                event_type="feedback_recorded",
                occurred_at=now,
                details={"step_id": step_id, "reason_code": outcome},
            )
        return {
            "schema": "agent_hub_feedback_v1",
            "feedback_id": int(feedback_id),
            "run_id": run_id,
            "step_id": step_id,
            "outcome": outcome,
            "rating": rating,
            "signal_weight": weight,
            "created_at": now,
        }

    def record_routing_decision(
        self,
        *,
        run_id: str | None,
        step_id: str | None,
        routing_mode: str,
        selected_provider: str | None,
        planner_provider: str | None,
        candidates: list[Mapping[str, Any]],
        scores: Mapping[str, Any],
        sample_count: int,
        policy_revision: int,
    ) -> dict[str, Any]:
        decision_id = f"route_{secrets.token_hex(12)}"
        now = self._clock()
        with self._transaction() as connection:
            if run_id is not None:
                self._load_run(connection, run_id)
            connection.execute(
                """
                INSERT INTO routing_decisions(
                    decision_id, schema_name, run_id, step_id, routing_mode,
                    selected_provider, planner_provider, candidates_json,
                    score_json, sample_count, policy_revision, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    ROUTING_DECISION_SCHEMA,
                    run_id,
                    step_id,
                    routing_mode,
                    selected_provider,
                    planner_provider,
                    canonical_json(candidates),
                    canonical_json(scores),
                    sample_count,
                    policy_revision,
                    now,
                ),
            )
        return {
            "schema": ROUTING_DECISION_SCHEMA,
            "decision_id": decision_id,
            "run_id": run_id,
            "step_id": step_id,
            "routing_mode": routing_mode,
            "selected_provider": selected_provider,
            "planner_provider": planner_provider,
            "candidates": list(candidates),
            "scores": dict(scores),
            "sample_count": sample_count,
            "policy_revision": policy_revision,
            "created_at": now,
        }

    def record_routing_sample(
        self,
        *,
        context: Mapping[str, Any],
        provider: str,
        model: str | None,
        capability: str,
        success: bool,
        quality: float | None,
        latency_ms: int | None,
        total_tokens: int | None,
        signal_weight: float,
    ) -> dict[str, Any]:
        if quality is not None and not 0.0 <= quality <= 1.0:
            raise HubV2Error(
                "invalid_routing_sample",
                "quality must be between 0 and 1.",
                scope="routing",
            )
        for field, value in (("latency_ms", latency_ms), ("total_tokens", total_tokens)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise HubV2Error(
                    "invalid_routing_sample",
                    f"{field} must be a non-negative integer.",
                    scope="routing",
                )
        if signal_weight <= 0:
            raise HubV2Error(
                "invalid_routing_sample",
                "signal_weight must be positive.",
                scope="routing",
            )
        context_json = canonical_json(context)
        context_sha = sha256(context_json.encode("utf-8")).hexdigest()
        now = self._clock()
        with self._transaction() as connection:
            sample_id = connection.execute(
                """
                INSERT INTO routing_samples(
                    context_sha256, context_json, provider, model, capability,
                    success, quality, latency_ms, total_tokens, signal_weight,
                    recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context_sha,
                    context_json,
                    provider,
                    model,
                    capability,
                    int(success),
                    quality,
                    latency_ms,
                    total_tokens,
                    signal_weight,
                    now,
                ),
            ).lastrowid
        return {
            "schema": "agent_hub_routing_sample_v1",
            "sample_id": int(sample_id),
            "context_sha256": context_sha,
            "provider": provider,
            "model": model,
            "capability": capability,
            "recorded_at": now,
        }

    def routing_statistics(
        self,
        *,
        context: Mapping[str, Any],
        provider: str,
        half_life_days: float = 30.0,
        detail_days: float = 90.0,
    ) -> dict[str, Any]:
        context_json = canonical_json(context)
        context_sha = sha256(context_json.encode("utf-8")).hexdigest()
        now = self._clock()
        cutoff = now - detail_days * 86400.0
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM routing_samples
                WHERE context_sha256 = ? AND provider = ? AND recorded_at >= ?
                ORDER BY recorded_at
                """,
                (context_sha, provider, cutoff),
            ).fetchall()
            aggregate_rows = connection.execute(
                """
                SELECT * FROM routing_daily_aggregates
                WHERE context_sha256 = ? AND provider = ?
                  AND day >= ? AND day < ?
                ORDER BY day
                """,
                (
                    context_sha,
                    provider,
                    int((now - 365.0 * 86400.0) // 86400),
                    int(cutoff // 86400),
                ),
            ).fetchall()
        finally:
            connection.close()
        half_life_seconds = max(1.0, half_life_days * 86400.0)
        weighted_success = 1.0
        weighted_failure = 1.0
        quality_total = 0.0
        quality_weight = 0.0
        latency_total = 0.0
        latency_weight = 0.0
        tokens_total = 0.0
        tokens_weight = 0.0
        sample_count = 0
        for row in rows:
            age = max(0.0, now - float(row["recorded_at"]))
            decay = 0.5 ** (age / half_life_seconds)
            weight = float(row["signal_weight"]) * decay
            if row["success"]:
                weighted_success += weight
            else:
                weighted_failure += weight
            if row["quality"] is not None:
                quality_total += float(row["quality"]) * weight
                quality_weight += weight
            if row["latency_ms"] is not None:
                latency_total += float(row["latency_ms"]) * weight
                latency_weight += weight
            if row["total_tokens"] is not None:
                tokens_total += float(row["total_tokens"]) * weight
                tokens_weight += weight
            sample_count += 1
        for row in aggregate_rows:
            recorded_at = (int(row["day"]) + 0.5) * 86400.0
            age = max(0.0, now - recorded_at)
            decay = 0.5 ** (age / half_life_seconds)
            weighted_success += float(row["success_weight"]) * decay
            weighted_failure += float(row["failure_weight"]) * decay
            quality_total += float(row["quality_total"]) * decay
            quality_weight += float(row["quality_weight"]) * decay
            latency_total += float(row["latency_total"]) * decay
            latency_weight += float(row["latency_weight"]) * decay
            tokens_total += float(row["tokens_total"]) * decay
            tokens_weight += float(row["tokens_weight"]) * decay
            sample_count += int(row["sample_count"])
        observed_total = max(0.0, weighted_success + weighted_failure - 2.0)
        observed_failure = max(0.0, weighted_failure - 1.0)
        return {
            "schema": "agent_hub_routing_statistics_v1",
            "context_sha256": context_sha,
            "provider": provider,
            "sample_count": sample_count,
            "reliability": weighted_success / (weighted_success + weighted_failure),
            "failure_rate": (
                observed_failure / observed_total if observed_total else None
            ),
            "quality": quality_total / quality_weight if quality_weight else 0.5,
            "latency_ms": latency_total / latency_weight if latency_weight else None,
            "total_tokens": tokens_total / tokens_weight if tokens_weight else None,
        }

    def prune_routing_details(self, *, retention_days: float = 90.0) -> int:
        now = self._clock()
        cutoff = now - retention_days * 86400.0
        aggregate_cutoff_day = int((now - 365.0 * 86400.0) // 86400)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO routing_daily_aggregates(
                    day, context_sha256, context_json, provider, model, capability,
                    sample_count, success_weight, failure_weight,
                    quality_total, quality_weight, latency_total, latency_weight,
                    tokens_total, tokens_weight
                )
                SELECT
                    CAST(recorded_at / 86400 AS INTEGER),
                    context_sha256, context_json, provider, COALESCE(model, ''),
                    capability, COUNT(*),
                    SUM(CASE WHEN success = 1 THEN signal_weight ELSE 0 END),
                    SUM(CASE WHEN success = 0 THEN signal_weight ELSE 0 END),
                    SUM(CASE WHEN quality IS NOT NULL
                        THEN quality * signal_weight ELSE 0 END),
                    SUM(CASE WHEN quality IS NOT NULL THEN signal_weight ELSE 0 END),
                    SUM(CASE WHEN latency_ms IS NOT NULL
                        THEN latency_ms * signal_weight ELSE 0 END),
                    SUM(CASE WHEN latency_ms IS NOT NULL THEN signal_weight ELSE 0 END),
                    SUM(CASE WHEN total_tokens IS NOT NULL
                        THEN total_tokens * signal_weight ELSE 0 END),
                    SUM(CASE WHEN total_tokens IS NOT NULL THEN signal_weight ELSE 0 END)
                FROM routing_samples
                WHERE recorded_at < ?
                GROUP BY CAST(recorded_at / 86400 AS INTEGER),
                    context_sha256, context_json, provider, COALESCE(model, ''),
                    capability
                ON CONFLICT(day, context_sha256, provider, model) DO UPDATE SET
                    sample_count = sample_count + excluded.sample_count,
                    success_weight = success_weight + excluded.success_weight,
                    failure_weight = failure_weight + excluded.failure_weight,
                    quality_total = quality_total + excluded.quality_total,
                    quality_weight = quality_weight + excluded.quality_weight,
                    latency_total = latency_total + excluded.latency_total,
                    latency_weight = latency_weight + excluded.latency_weight,
                    tokens_total = tokens_total + excluded.tokens_total,
                    tokens_weight = tokens_weight + excluded.tokens_weight
                """,
                (cutoff,),
            )
            cursor = connection.execute(
                "DELETE FROM routing_samples WHERE recorded_at < ?",
                (cutoff,),
            )
            connection.execute(
                "DELETE FROM routing_daily_aggregates WHERE day < ?",
                (aggregate_cutoff_day,),
            )
            return max(0, int(cursor.rowcount))

    def index_context_document(
        self,
        *,
        project_identity: str,
        namespace: str,
        path_alias: str,
        content: str,
        complete: bool,
    ) -> dict[str, Any]:
        document_identity = canonical_json(
            {
                "project_identity": project_identity,
                "namespace": namespace,
                "path_alias": path_alias,
            }
        )
        document_id = f"doc_{sha256(document_identity.encode('utf-8')).hexdigest()[:24]}"
        content_sha = sha256(content.encode("utf-8")).hexdigest()
        now = self._clock()
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM context_fts WHERE document_id = ?",
                (document_id,),
            )
            connection.execute(
                """
                INSERT INTO context_documents(
                    document_id, project_identity, namespace, path_alias,
                    content_sha256, chars, complete, indexed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    content_sha256 = excluded.content_sha256,
                    chars = excluded.chars,
                    complete = excluded.complete,
                    indexed_at = excluded.indexed_at
                """,
                (
                    document_id,
                    project_identity,
                    namespace,
                    path_alias,
                    content_sha,
                    len(content),
                    int(complete),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO context_fts(
                    document_id, project_identity, namespace, path_alias, content
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (document_id, project_identity, namespace, path_alias, content),
            )
        return {
            "schema": "agent_hub_context_document_v1",
            "document_id": document_id,
            "path": path_alias,
            "namespace": namespace,
            "content_sha256": content_sha,
            "chars": len(content),
            "complete": complete,
            "indexed_at": now,
        }

    def search_context(
        self,
        *,
        project_identity: str,
        query: str,
        namespace: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise HubV2Error(
                "invalid_request",
                "context search limit must be between 1 and 50.",
                scope="context",
            )
        tokens = [
            "".join(char for char in token if char.isalnum() or char == "_")
            for token in query.split()
        ]
        tokens = [token for token in tokens if token]
        if not tokens:
            return []
        match = " OR ".join(f'"{token}"' for token in tokens[:20])
        connection = self._connect()
        try:
            if namespace:
                rows = connection.execute(
                    """
                    SELECT f.document_id, f.path_alias, f.namespace,
                           snippet(context_fts, 4, '', '', ' … ', 40) AS excerpt,
                           bm25(context_fts) AS rank,
                           d.content_sha256, d.complete, d.indexed_at
                    FROM context_fts AS f
                    JOIN context_documents AS d ON d.document_id = f.document_id
                    WHERE context_fts MATCH ?
                      AND f.project_identity = ?
                      AND f.namespace = ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (match, project_identity, namespace, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT f.document_id, f.path_alias, f.namespace,
                           snippet(context_fts, 4, '', '', ' … ', 40) AS excerpt,
                           bm25(context_fts) AS rank,
                           d.content_sha256, d.complete, d.indexed_at
                    FROM context_fts AS f
                    JOIN context_documents AS d ON d.document_id = f.document_id
                    WHERE context_fts MATCH ?
                      AND f.project_identity = ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (match, project_identity, limit),
                ).fetchall()
        finally:
            connection.close()
        return [
            {
                "schema": "agent_hub_context_match_v1",
                "document_id": row["document_id"],
                "path": row["path_alias"],
                "namespace": row["namespace"],
                "excerpt": row["excerpt"],
                "rank": row["rank"],
                "content_sha256": row["content_sha256"],
                "complete": bool(row["complete"]),
                "indexed_at": row["indexed_at"],
            }
            for row in rows
        ]

    def record_legacy_import(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        source_sha = str(entry.get("source_sha256") or "")
        if len(source_sha) != 64 or any(
            char not in "0123456789abcdef" for char in source_sha
        ):
            raise HubV2Error(
                "invalid_import_entry",
                "The legacy source digest is invalid.",
                scope="migration",
            )
        now = self._clock()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO legacy_imports(
                    source_sha256, source_name, legacy_run_id, legacy_run_kind,
                    legacy_status, source_schema_version, imported_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_sha,
                    str(entry.get("source_name") or "")[:256],
                    str(entry.get("run_id") or "")[:128],
                    str(entry.get("run_kind") or "unknown")[:64],
                    str(entry.get("status") or "unknown")[:64],
                    entry.get("source_schema_version")
                    if isinstance(entry.get("source_schema_version"), int)
                    else None,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM legacy_imports WHERE source_sha256 = ?",
                (source_sha,),
            ).fetchone()
        return {
            "schema": "agent_hub_v1_import_receipt_v1",
            "source_sha256": row["source_sha256"],
            "source_name": row["source_name"],
            "legacy_run_id": row["legacy_run_id"],
            "legacy_run_kind": row["legacy_run_kind"],
            "legacy_status": row["legacy_status"],
            "imported_at": row["imported_at"],
            "mode": "archive_metadata_only",
        }

    def record_provider_outcome(
        self,
        *,
        provider: str,
        success: bool,
        error_code: str | None = None,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> dict[str, Any]:
        now = self._clock()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM provider_health WHERE provider = ?",
                (provider,),
            ).fetchone()
            failures = int(existing["consecutive_failures"]) if existing else 0
            if success:
                failures = 0
                open_until = None
                error = None
            else:
                failures += 1
                open_until = (
                    now + cooldown_seconds
                    if failures >= failure_threshold
                    else existing["circuit_open_until"]
                    if existing
                    else None
                )
                error = str(error_code or "provider_failure")[:128]
            connection.execute(
                """
                INSERT INTO provider_health(
                    provider, consecutive_failures, circuit_open_until,
                    last_error_code, updated_at
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    consecutive_failures = excluded.consecutive_failures,
                    circuit_open_until = excluded.circuit_open_until,
                    last_error_code = excluded.last_error_code,
                    updated_at = excluded.updated_at
                """,
                (provider, failures, open_until, error, now),
            )
        return {
            "schema": "agent_hub_provider_health_v1",
            "provider": provider,
            "consecutive_failures": failures,
            "circuit_open": bool(open_until and open_until > now),
            "circuit_open_until": open_until,
            "last_error_code": error,
            "updated_at": now,
        }

    def record_generation_verification(
        self,
        *,
        provider: str,
        model: str,
        generation_state: str,
        reason_code: str,
    ) -> dict[str, Any]:
        if generation_state not in {"verified", "failed"}:
            raise HubV2Error(
                "invalid_generation_state",
                "The generation verification state is invalid.",
                scope="provider",
            )
        provider_id = require_identifier(provider, field="provider")
        model_id = str(model)[:256]
        if not model_id:
            raise HubV2Error(
                "invalid_model",
                "A model is required for generation verification.",
                scope="provider",
            )
        now = self._clock()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO provider_verifications(
                    provider, model, generation_state, reason_code, verified_at
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(provider, model) DO UPDATE SET
                    generation_state = excluded.generation_state,
                    reason_code = excluded.reason_code,
                    verified_at = excluded.verified_at
                """,
                (
                    provider_id,
                    model_id,
                    generation_state,
                    str(reason_code)[:128],
                    now,
                ),
            )
        return {
            "schema": "agent_hub_generation_verification_v1",
            "provider": provider_id,
            "model": model_id,
            "generation_state": generation_state,
            "reason_code": str(reason_code)[:128],
            "verified_at": now,
        }

    def generation_verification(
        self,
        *,
        provider: str,
        model: str | None = None,
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            if model:
                row = connection.execute(
                    """
                    SELECT * FROM provider_verifications
                    WHERE provider = ? AND model = ?
                    """,
                    (provider, model),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM provider_verifications
                    WHERE provider = ? ORDER BY verified_at DESC LIMIT 1
                    """,
                    (provider,),
                ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return {
            "provider": row["provider"],
            "model": row["model"],
            "generation_state": row["generation_state"],
            "reason_code": row["reason_code"],
            "verified_at": row["verified_at"],
        }

    def provider_health(self, provider: str) -> dict[str, Any]:
        now = self._clock()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM provider_health WHERE provider = ?",
                (provider,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return {
                "schema": "agent_hub_provider_health_v1",
                "provider": provider,
                "consecutive_failures": 0,
                "circuit_open": False,
                "circuit_open_until": None,
                "last_error_code": None,
                "updated_at": None,
            }
        return {
            "schema": "agent_hub_provider_health_v1",
            "provider": provider,
            "consecutive_failures": row["consecutive_failures"],
            "circuit_open": bool(
                row["circuit_open_until"] and row["circuit_open_until"] > now
            ),
            "circuit_open_until": row["circuit_open_until"],
            "last_error_code": row["last_error_code"],
            "updated_at": row["updated_at"],
        }

    def recover_expired_leases(self) -> dict[str, Any]:
        now = self._clock()
        retryable: list[dict[str, Any]] = []
        unknown: list[dict[str, Any]] = []
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT run_id, revision FROM runs
                WHERE lease_token_sha256 IS NOT NULL
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                  AND status = 'running'
                """,
                (now,),
            ).fetchall()
            for row in rows:
                running_steps = connection.execute(
                    """
                    SELECT step_id, checkpoint_state FROM steps
                    WHERE run_id = ? AND status = 'running'
                    """,
                    (row["run_id"],),
                ).fetchall()
                ambiguous = any(
                    not bool(_json_object(step["checkpoint_state"]).get("retry_safe"))
                    for step in running_steps
                )
                next_status = "outcome_unknown" if ambiguous else "queued"
                for step in running_steps:
                    checkpoint = _json_object(step["checkpoint_state"])
                    connection.execute(
                        """
                        UPDATE steps
                        SET status = ?, revision = revision + 1,
                            checkpoint_state = ?, updated_at = ?
                        WHERE run_id = ? AND step_id = ?
                        """,
                        (
                            "outcome_unknown" if ambiguous else "queued",
                            canonical_json(
                                {
                                    **checkpoint,
                                    "recovery_reason": (
                                        "external_outcome_unknown"
                                        if ambiguous
                                        else "retry_safe_local_step"
                                    ),
                                }
                            ),
                            now,
                            row["run_id"],
                            step["step_id"],
                        ),
                    )
                next_revision = int(row["revision"]) + 1
                connection.execute(
                    """
                    UPDATE runs
                    SET status = ?, revision = ?, lease_token_sha256 = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE run_id = ? AND revision = ?
                    """,
                    (
                        next_status,
                        next_revision,
                        now,
                        row["run_id"],
                        row["revision"],
                    ),
                )
                self._append_event_tx(
                    connection,
                    run_id=row["run_id"],
                    run_revision=next_revision,
                    event_type=(
                        "lease_outcome_unknown" if ambiguous else "lease_recovered"
                    ),
                    occurred_at=now,
                    details={
                        "reason_code": (
                            "external_outcome_unknown"
                            if ambiguous
                            else "retry_safe_local_step"
                        ),
                        "retryable": not ambiguous,
                    },
                )
                item = {"run_id": row["run_id"], "revision": next_revision}
                (unknown if ambiguous else retryable).append(item)
        return {
            "schema": "agent_hub_lease_recovery_v1",
            "retryable_runs": retryable,
            "outcome_unknown_runs": unknown,
            "count": len(retryable) + len(unknown),
        }


def disposable_store() -> HubStore:
    """Convenience for manual diagnostics; tests should pass an explicit tmp path."""

    root = Path(tempfile.mkdtemp(prefix="agent-hub-v2-store-"))
    return HubStore(root / DEFAULT_DB_NAME)
