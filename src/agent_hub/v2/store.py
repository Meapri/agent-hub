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
import re
import secrets
from statistics import median_low
import sqlite3
import stat
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping

from .contracts import (
    ARTIFACT_SCHEMA,
    EVENT_SCHEMA,
    MAX_STEP_TOKENS,
    RECONCILIATION_SCHEMA,
    RUN_SCHEMA,
    RUN_STATUSES,
    STEP_TOKEN_SOURCES,
    canonical_json,
    canonical_project_root,
    digest_json,
    require_identifier,
    require_non_negative_int,
    safe_usage,
    total_token_limit,
)
from .errors import HubV2Error
from .invariants import InvariantViolation, assert_required_schema
from .metrics import summarize_operation_metrics

STORE_SCHEMA_VERSION = 11
DEFAULT_STATE_DIR = Path("~/.agent-hub").expanduser()
DEFAULT_DB_NAME = "state.sqlite3"
MAX_EVENT_LIMIT = 100
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_SAFE_STRING = 256
# HubV2Error.code is a plain str field, so metric rows normalize it against this
# pattern before storing. Anything else becomes UNCLASSIFIED_ERROR_CODE, which keeps
# free-form text out of the content-free metrics surface.
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
UNCLASSIFIED_ERROR_CODE = "unclassified_error"
MAX_RUN_TOKEN_GRANT = 100_000_000
MAX_RUN_RECONCILIATIONS = 3
HANDOFF_SNAPSHOT_RETENTION = 50
MAX_HANDOFF_SNAPSHOT_CHARS = 128_000
RECONCILIATION_TTL_SECONDS = 900.0
MAX_RECONCILIATION_TTL_SECONDS = 3600.0


def _plan_token_budget(plan: Mapping[str, Any]) -> int:
    """Denormalize the sealed plan's total token budget onto the run row."""

    task = plan.get("task")
    constraints = task.get("constraints") if isinstance(task, Mapping) else None
    return int(total_token_limit(constraints if isinstance(constraints, Mapping) else {}))


def normalize_error_code(code: Any, *, success: bool) -> str | None:
    """Reduce a dispatch failure code to the fixed, content-free metric taxonomy.

    Success rows never carry a code and failure rows always do, so a NULL
    error_code can only mean the row was written before store schema 10.
    fullmatch (not match) is required because ``$`` also matches just before a
    trailing newline, and provider-supplied codes reach this function unvalidated.
    """

    if success:
        return None
    text = code if isinstance(code, str) else ""
    if _ERROR_CODE_PATTERN.fullmatch(text) is None:
        return UNCLASSIFIED_ERROR_CODE
    return text


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
        "correlation_id",
        "tokens_used",
        "tokens_budget",
        "tokens_granted",
        "tokens_remaining",
        "tokens_estimated_wave",
        "budget_used_percent",
        "wave_step_count",
        "estimate_source",
        "reconciliation_id",
        "verdict",
        "run_disposition",
        "step_count",
        "witness_sha256",
    }
)
_NON_NEGATIVE_EVENT_FIELDS = frozenset(
    {
        "elapsed_ms",
        "prompt_chars",
        "result_chars",
        "tokens_used",
        "tokens_budget",
        "tokens_granted",
        "tokens_remaining",
        "tokens_estimated_wave",
        "budget_used_percent",
        "wave_step_count",
        "prior_revision",
        "step_count",
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
        elif key in _NON_NEGATIVE_EVENT_FIELDS:
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                safe[key] = value
        elif key in {"prompt_sha256", "result_sha256", "prior_sha256", "witness_sha256"}:
            text = str(value or "")
            if len(text) == 64 and all(char in "0123456789abcdef" for char in text):
                safe[key] = text
        elif key == "prior_weight_fraction":
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0.0 <= float(value) <= 1.0
            ):
                safe[key] = round(float(value), 6)
        else:
            text = str(value or "")[:MAX_SAFE_STRING]
            safe[key] = text if text else "redacted"
    return safe


def _assert_required_schema(connection: sqlite3.Connection) -> None:
    # The column set lives in invariants so the migration and the post-test
    # checker cannot drift apart.
    try:
        assert_required_schema(connection)
    except InvariantViolation as exc:
        raise HubV2Error(
            "store_migration_failed",
            "The migrated database is missing required schema fields.",
            scope="store",
            safe_details={"detail": str(exc)[:128]},
        ) from exc


class HubStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        requested = (
            Path(path).expanduser() if path is not None else DEFAULT_STATE_DIR / DEFAULT_DB_NAME
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
        source = sqlite3.connect(self.path, timeout=10.0)
        source.row_factory = sqlite3.Row
        try:
            source.execute("PRAGMA busy_timeout = 10000")
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
            row = source.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            current = int(row["value"]) if row is not None else 0
            if current < 3:
                raise HubV2Error(
                    "unsupported_store_schema",
                    "The local state database is too old for a safe in-place migration.",
                    scope="store",
                    safe_details={"current": current, "minimum": 3},
                )
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
        except sqlite3.DatabaseError as exc:
            raise HubV2Error(
                "store_integrity_failed",
                "The local state database is unreadable.",
                scope="store",
            ) from exc
        finally:
            source.close()

    def _restore_migration_backup(self, backup: Path) -> None:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.restore-",
            dir=self.path.parent,
        )
        temp = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as output, backup.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        finally:
            if temp.exists():
                temp.unlink()
        for candidate in (Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass

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
                    token_budget_limit INTEGER NOT NULL DEFAULT 0
                        CHECK (token_budget_limit >= 0),
                    token_budget_grant INTEGER NOT NULL DEFAULT 0
                        CHECK (token_budget_grant >= 0),
                    reconciliation_count INTEGER NOT NULL DEFAULT 0
                        CHECK (reconciliation_count >= 0),
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
                    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
                    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
                    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
                    tokens_source TEXT NOT NULL DEFAULT 'unset'
                        CHECK (tokens_source IN
                            ('unset', 'reported', 'estimated', 'mixed', 'local')),
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

                -- Covers capability_token_estimate, which forecasts a wave's
                -- spend from what completed steps actually cost.
                CREATE INDEX IF NOT EXISTS steps_capability_provider_updated
                    ON steps(capability, provider, updated_at);

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

                CREATE TABLE IF NOT EXISTS egress_approvals (
                    manifest_sha256 TEXT PRIMARY KEY,
                    project_root TEXT NOT NULL,
                    policy_revision INTEGER NOT NULL CHECK (policy_revision >= 0),
                    destinations_json TEXT NOT NULL DEFAULT '[]',
                    entries_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS egress_reviews (
                    review_id TEXT PRIMARY KEY,
                    proposal_sha256 TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    project_root TEXT NOT NULL,
                    policy_revision INTEGER NOT NULL CHECK (policy_revision >= 0),
                    provider TEXT NOT NULL,
                    model TEXT,
                    destinations_json TEXT NOT NULL,
                    entries_json TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('pending', 'approved', 'rejected', 'consumed', 'expired')),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    decided_at REAL,
                    consumed_at REAL,
                    decision_source TEXT,
                    decision_settings_revision INTEGER
                        CHECK (
                            decision_settings_revision IS NULL
                            OR decision_settings_revision >= 0
                        )
                );
                CREATE INDEX IF NOT EXISTS egress_reviews_status_created
                    ON egress_reviews(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS egress_reviews_proposal
                    ON egress_reviews(proposal_sha256, created_at DESC);

                CREATE TABLE IF NOT EXISTS egress_settings (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    revision INTEGER NOT NULL CHECK (revision >= 0),
                    auto_approve INTEGER NOT NULL CHECK (auto_approve IN (0, 1)),
                    updated_at REAL NOT NULL
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

                CREATE TABLE IF NOT EXISTS operation_metrics (
                    metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    success INTEGER NOT NULL CHECK (success IN (0, 1)),
                    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
                    recorded_at REAL NOT NULL,
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS operation_metrics_recorded
                    ON operation_metrics(recorded_at, metric_id);

                CREATE TABLE IF NOT EXISTS run_reconciliations (
                    reconciliation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    base_revision INTEGER NOT NULL CHECK (base_revision >= 0),
                    witness_sha256 TEXT NOT NULL,
                    proposal_sha256 TEXT NOT NULL,
                    confirmation_sha256 TEXT NOT NULL,
                    run_disposition TEXT NOT NULL
                        CHECK (run_disposition IN ('resume', 'fail')),
                    resolutions_json TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('pending', 'applied', 'expired', 'superseded')),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    applied_at REAL
                );
                CREATE INDEX IF NOT EXISTS run_reconciliations_run
                    ON run_reconciliations(run_id, status, created_at);

                CREATE TABLE IF NOT EXISTS handoff_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    scope_identity TEXT NOT NULL,
                    scope_root TEXT NOT NULL,
                    target_alias TEXT NOT NULL,
                    last_project_root TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence >= 1),
                    origin TEXT NOT NULL CHECK (origin IN ('applied', 'observed')),
                    file_sha256 TEXT NOT NULL,
                    managed_sha256 TEXT NOT NULL,
                    previous_file_sha256 TEXT,
                    previous_managed_sha256 TEXT,
                    body TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    body_chars INTEGER NOT NULL CHECK (body_chars >= 0),
                    body_truncated INTEGER NOT NULL DEFAULT 0
                        CHECK (body_truncated IN (0, 1)),
                    redacted_lines INTEGER NOT NULL DEFAULT 0
                        CHECK (redacted_lines >= 0),
                    sections_json TEXT NOT NULL DEFAULT '{}',
                    next_step TEXT NOT NULL DEFAULT '',
                    created INTEGER NOT NULL DEFAULT 0 CHECK (created IN (0, 1)),
                    recorded_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS handoff_snapshots_target
                    ON handoff_snapshots(scope_identity, target_alias, sequence);
                CREATE INDEX IF NOT EXISTS handoff_snapshots_recorded
                    ON handoff_snapshots(recorded_at);
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
                connection.execute("DROP TABLE IF EXISTS legacy_imports")
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(egress_approvals)").fetchall()
                }
                if "destinations_json" not in columns:
                    connection.execute(
                        "ALTER TABLE egress_approvals "
                        "ADD COLUMN destinations_json TEXT NOT NULL DEFAULT '[]'"
                    )
                review_columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(egress_reviews)").fetchall()
                }
                if "decision_source" not in review_columns:
                    connection.execute("ALTER TABLE egress_reviews ADD COLUMN decision_source TEXT")
                if "decision_settings_revision" not in review_columns:
                    connection.execute(
                        "ALTER TABLE egress_reviews ADD COLUMN decision_settings_revision INTEGER"
                    )
                metric_columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(operation_metrics)").fetchall()
                }
                if "error_code" not in metric_columns:
                    # Rows written before schema 10 keep NULL, which summaries report
                    # separately from a recorded failure code.
                    connection.execute("ALTER TABLE operation_metrics ADD COLUMN error_code TEXT")
                run_columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(runs)").fetchall()
                }
                if "token_budget_limit" not in run_columns:
                    connection.execute(
                        "ALTER TABLE runs ADD COLUMN token_budget_limit "
                        "INTEGER NOT NULL DEFAULT 0 CHECK (token_budget_limit >= 0)"
                    )
                if "token_budget_grant" not in run_columns:
                    connection.execute(
                        "ALTER TABLE runs ADD COLUMN token_budget_grant "
                        "INTEGER NOT NULL DEFAULT 0 CHECK (token_budget_grant >= 0)"
                    )
                if "reconciliation_count" not in run_columns:
                    connection.execute(
                        "ALTER TABLE runs ADD COLUMN reconciliation_count "
                        "INTEGER NOT NULL DEFAULT 0 CHECK (reconciliation_count >= 0)"
                    )
                step_columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(steps)").fetchall()
                }
                for column in ("input_tokens", "output_tokens", "total_tokens"):
                    if column not in step_columns:
                        connection.execute(
                            f"ALTER TABLE steps ADD COLUMN {column} "
                            f"INTEGER NOT NULL DEFAULT 0 CHECK ({column} >= 0)"
                        )
                if "tokens_source" not in step_columns:
                    connection.execute(
                        "ALTER TABLE steps ADD COLUMN tokens_source TEXT NOT NULL "
                        "DEFAULT 'unset' CHECK (tokens_source IN "
                        "('unset', 'reported', 'estimated', 'mixed', 'local'))"
                    )
                # Sentinel backfill: a run with a zero budget is meaningless, so the
                # zero value is a safe idempotent marker for "not yet derived".
                for row in connection.execute(
                    "SELECT run_id, plan_json FROM runs WHERE token_budget_limit = 0"
                ).fetchall():
                    connection.execute(
                        "UPDATE runs SET token_budget_limit = ? WHERE run_id = ?",
                        (_plan_token_budget(_json_object(row["plan_json"])), row["run_id"]),
                    )
                # Schema 11 removed the routing learning layer. Drop its tables
                # rather than leaving them: they only ever held scores nothing
                # reads now, and a stale table invites a future reader to trust
                # numbers that stopped being maintained.
                for table in (
                    "routing_decisions",
                    "routing_samples",
                    "routing_daily_aggregates",
                ):
                    connection.execute(f"DROP TABLE IF EXISTS {table}")
                connection.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (str(STORE_SCHEMA_VERSION),),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO egress_settings(
                    singleton_id, revision, auto_approve, updated_at
                ) VALUES(1, 0, 0, ?)
                """,
                (self._clock(),),
            )
            _assert_required_schema(connection)
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

    def record_operation_metric(
        self,
        *,
        operation: str,
        success: bool,
        duration_ms: int,
        error_code: str | None = None,
    ) -> None:
        name = str(operation)
        if (
            not name.startswith("agent_hub_")
            or len(name) > 64
            or isinstance(duration_ms, bool)
            or duration_ms < 0
        ):
            return
        recorded_error = normalize_error_code(error_code, success=success)
        now = self._clock()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO operation_metrics(
                    operation, success, duration_ms, recorded_at, error_code
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (name, int(success), int(duration_ms), now, recorded_error),
            )
            connection.execute(
                "DELETE FROM operation_metrics WHERE recorded_at < ?",
                (now - (90 * 86400),),
            )
            connection.execute(
                """
                DELETE FROM operation_metrics
                WHERE metric_id IN (
                    SELECT metric_id FROM operation_metrics
                    ORDER BY metric_id DESC
                    LIMIT -1 OFFSET 20000
                )
                """
            )

    def operation_metrics(self, *, limit: int = 10_000) -> dict[str, Any]:
        bounded = min(max(int(limit), 1), 10_000)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT operation, success, duration_ms, error_code
                FROM operation_metrics
                ORDER BY metric_id DESC
                LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        finally:
            connection.close()
        return summarize_operation_metrics(rows)

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
    def _token_usage(row: sqlite3.Row, steps: list[dict[str, Any]]) -> dict[str, Any]:
        total = sum(int(step["total_tokens"]) for step in steps)
        limit = int(row["token_budget_limit"] or 0)
        grant = int(row["token_budget_grant"] or 0)
        budget = limit + grant
        return {
            "schema": "agent_hub_run_token_usage_v1",
            "input_tokens": sum(int(step["input_tokens"]) for step in steps),
            "output_tokens": sum(int(step["output_tokens"]) for step in steps),
            "total_tokens": total,
            "max_total_tokens": budget,
            "granted_tokens": grant,
            "remaining_tokens": max(0, budget - total),
            "budget_used_percent": (total * 100) // budget if budget else 0,
            "reported_step_count": sum(
                1 for step in steps if step["tokens_source"] in {"reported", "mixed"}
            ),
            "estimated_step_count": sum(
                1 for step in steps if step["tokens_source"] == "estimated"
            ),
            # `total > 0` keeps a run that has not spent anything yet (and a
            # local-only run with a zero budget) from being gated before its first wave.
            "exhausted": bool(total > 0 and total >= budget),
        }

    def _public_run(self, row: sqlite3.Row, steps: list[dict[str, Any]]) -> dict[str, Any]:
        retryable_failed_steps = [
            step["step_id"]
            for step in steps
            if step["status"] == "failed" and step["checkpoint"].get("retry_safe") is True
        ]
        outcome_unknown_steps = [
            step["step_id"] for step in steps if step["status"] == "outcome_unknown"
        ]
        token_usage = self._token_usage(row, steps)
        result = {
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
                and row["lease_expires_at"] > self._clock()
            ),
            "lease_expires_at": row["lease_expires_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "steps": steps,
            "retryable_failed_steps": retryable_failed_steps,
            "outcome_unknown_steps": outcome_unknown_steps,
            "reconciliation_count": row["reconciliation_count"],
            "token_usage": token_usage,
        }
        if outcome_unknown_steps:
            # Deliberately pre-fills the SAFE verdict only. A caller that executes
            # next_action verbatim can never trigger an external re-send; asking for
            # not_delivered requires consciously rewriting the arguments.
            result["next_action"] = {
                "type": "call_tool",
                "tool": "agent_hub_cancel",
                "arguments": {
                    "action": "prepare_reconcile",
                    "run_id": row["run_id"],
                    "expected_revision": row["revision"],
                    "resolutions": [
                        {"step_id": step_id, "verdict": "delivered_discarded"}
                        for step_id in outcome_unknown_steps
                    ],
                    "run_disposition": "fail",
                },
            }
        elif row["status"] == "paused":
            arguments: dict[str, Any] = {
                "run_id": row["run_id"],
                "expected_revision": row["revision"],
            }
            if token_usage["exhausted"]:
                # Offer one more budget of the size the sealed plan already declared.
                arguments["token_budget_grant"] = max(1, int(row["token_budget_limit"] or 0))
            if retryable_failed_steps:
                arguments["retry_failed_steps"] = retryable_failed_steps
            if len(arguments) > 2:
                result["next_action"] = {
                    "type": "call_tool",
                    "tool": "agent_hub_continue",
                    "arguments": arguments,
                }
        return result

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
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "tokens_source": str(row["tokens_source"] or "unset"),
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

    def get_run_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        expected_project_root: str | None = None,
        expected_request_plan_sha256: str | None = None,
    ) -> dict[str, Any] | None:
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
            stored_plan = _json_object(run_row["plan_json"])
            if (
                expected_project_root is not None
                and run_row["project_root"] != canonical_project_root(expected_project_root)
            ) or (
                expected_request_plan_sha256 is not None
                and stored_plan.get("request_plan_sha256") != expected_request_plan_sha256
            ):
                raise HubV2Error(
                    "idempotency_key_conflict",
                    "The idempotency key belongs to a different project or plan.",
                    scope="run",
                )
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
                existing_plan = _json_object(row["plan_json"])
                if row["project_root"] != root or existing_plan.get(
                    "request_plan_sha256"
                ) != plan.get("request_plan_sha256"):
                    raise HubV2Error(
                        "idempotency_key_conflict",
                        "The idempotency key belongs to a different project or plan.",
                        scope="run",
                    )
                return self._public_run(row, public_steps)
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, schema_name, project_root, status, revision,
                    plan_sha256, plan_json, policy_revision, routing_mode,
                    parent_run_id, idempotency_key, token_budget_limit,
                    created_at, updated_at
                ) VALUES(?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    _plan_token_budget(plan),
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

    def record_runtime_event(
        self,
        run_id: str,
        *,
        event_type: str,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        with self._transaction() as connection:
            row, _ = self._load_run(connection, run_id)
            return self._append_event_tx(
                connection,
                run_id=run_id,
                run_revision=int(row["revision"]),
                event_type=event_type,
                occurred_at=self._clock(),
                details=details,
            )

    def events(
        self,
        run_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 50,
        project_root: str | None = None,
    ) -> dict[str, Any]:
        after = require_non_negative_int(after_cursor, field="after_cursor")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_EVENT_LIMIT
        ):
            raise HubV2Error(
                "invalid_request",
                f"limit must be between 1 and {MAX_EVENT_LIMIT}.",
                scope="event",
            )
        connection = self._connect()
        try:
            row, _ = self._load_run(connection, run_id)
            if project_root is not None and row["project_root"] != canonical_project_root(
                project_root
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

    def renew_claim(
        self,
        run_id: str,
        *,
        claim_token: str,
        expected_revision: int,
        lease_seconds: float,
    ) -> float:
        expected = require_non_negative_int(expected_revision, field="expected_revision")
        duration = min(max(float(lease_seconds), 1.0), 3600.0)
        token_sha = sha256(claim_token.encode("ascii")).hexdigest()
        now = self._clock()
        expires_at = now + duration
        with self._transaction() as connection:
            row, _ = self._load_run(connection, run_id)
            if (
                row["revision"] != expected
                or row["status"] != "running"
                or row["lease_token_sha256"] != token_sha
            ):
                raise HubV2Error(
                    "lease_lost",
                    "The run lease cannot be renewed.",
                    scope="run",
                    retryable=True,
                )
            connection.execute(
                """
                UPDATE runs
                SET lease_expires_at = ?, updated_at = ?
                WHERE run_id = ? AND revision = ? AND lease_token_sha256 = ?
                """,
                (expires_at, now, run_id, expected, token_sha),
            )
        return expires_at

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
        input_artifact_ids: list[str] | None = None,
        output_artifact_ids: list[str] | None = None,
        checkpoint: Mapping[str, Any] | None = None,
        token_usage: Mapping[str, Any] | None = None,
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
                status == "running" or (row["status"] == "queued" and status == "completed")
            )
            next_input_artifacts = (
                input_artifact_ids
                if input_artifact_ids is not None
                else _json_list(row["input_artifact_ids"])
            )
            next_output_artifacts = (
                output_artifact_ids
                if output_artifact_ids is not None
                else _json_list(row["output_artifact_ids"])
            )
            next_provider = provider if provider is not None else row["provider"]
            next_model = model if model is not None else row["model"]
            next_checkpoint = {
                **_json_object(row["checkpoint_state"]),
                **dict(checkpoint or {}),
            }
            next_revision = expected + 1
            # Token deltas accumulate across attempts: a retried step really is
            # billed again, so the ledger has to be the sum of all attempts.
            delta_input = delta_output = delta_total = 0
            next_source = str(row["tokens_source"] or "unset")
            if token_usage is not None:
                delta_input = require_non_negative_int(
                    token_usage.get("input_tokens", 0), field="token_usage.input_tokens"
                )
                delta_output = require_non_negative_int(
                    token_usage.get("output_tokens", 0), field="token_usage.output_tokens"
                )
                delta_total = require_non_negative_int(
                    token_usage.get("total_tokens", 0), field="token_usage.total_tokens"
                )
                source = str(token_usage.get("source") or "unset")
                if (
                    source not in STEP_TOKEN_SOURCES
                    or max(delta_input, delta_output, delta_total) > MAX_STEP_TOKENS
                ):
                    raise HubV2Error(
                        "invalid_step_usage",
                        "The step token usage is not supported.",
                        scope="run",
                    )
                next_source = source if next_source in {"unset", source} else "mixed"
            connection.execute(
                """
                UPDATE steps
                SET status = ?, revision = revision + 1, provider = ?, model = ?,
                    attempt = attempt + ?, input_artifact_ids = ?,
                    output_artifact_ids = ?,
                    checkpoint_state = ?,
                    input_tokens = input_tokens + ?,
                    output_tokens = output_tokens + ?,
                    total_tokens = total_tokens + ?,
                    tokens_source = ?, updated_at = ?
                WHERE run_id = ? AND step_id = ?
                """,
                (
                    status,
                    next_provider,
                    next_model,
                    attempt_delta,
                    canonical_json(next_input_artifacts),
                    canonical_json(next_output_artifacts),
                    canonical_json(next_checkpoint),
                    delta_input,
                    delta_output,
                    delta_total,
                    next_source,
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
                    "provider": next_provider,
                    "model": next_model,
                    "reason_code": status,
                },
            )
            updated, steps = self._load_run(connection, run_id)
            return self._public_run(updated, steps)

    def reconcile_running_steps(
        self,
        run_id: str,
        *,
        claim_token: str,
        expected_revision: int,
        reason_code: str,
    ) -> dict[str, Any]:
        expected = require_non_negative_int(
            expected_revision,
            field="expected_revision",
        )
        token_sha = sha256(str(claim_token).encode("ascii")).hexdigest()
        now = self._clock()
        with self._transaction() as connection:
            row, _ = self._load_run(connection, run_id)
            if (
                row["revision"] != expected
                or row["status"] != "running"
                or row["lease_token_sha256"] != token_sha
            ):
                raise HubV2Error(
                    "lease_lost",
                    "Running steps cannot be reconciled without the active claim.",
                    scope="run",
                    retryable=True,
                )
            running = connection.execute(
                """
                SELECT step_id, checkpoint_state
                FROM steps
                WHERE run_id = ? AND status = 'running'
                ORDER BY rowid
                """,
                (run_id,),
            ).fetchall()
            if not running:
                current, steps = self._load_run(connection, run_id)
                return {
                    "run": self._public_run(current, steps),
                    "requeued_step_ids": [],
                    "outcome_unknown_step_ids": [],
                }
            requeued: list[str] = []
            unknown: list[str] = []
            for step in running:
                checkpoint = _json_object(step["checkpoint_state"])
                retry_safe = checkpoint.get("retry_safe") is True
                status = "queued" if retry_safe else "outcome_unknown"
                target = requeued if retry_safe else unknown
                target.append(str(step["step_id"]))
                connection.execute(
                    """
                    UPDATE steps
                    SET status = ?, revision = revision + 1,
                        checkpoint_state = ?, updated_at = ?
                    WHERE run_id = ? AND step_id = ?
                    """,
                    (
                        status,
                        canonical_json(
                            {
                                **checkpoint,
                                "phase": (
                                    "coordinator_retry_queued" if retry_safe else "outcome_unknown"
                                ),
                                "error_code": str(reason_code or "run_internal_error"),
                                "retry_safe": retry_safe,
                            }
                        ),
                        now,
                        run_id,
                        step["step_id"],
                    ),
                )
            next_revision = expected + 1
            connection.execute(
                "UPDATE runs SET revision = ?, updated_at = ? WHERE run_id = ?",
                (next_revision, now, run_id),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                run_revision=next_revision,
                event_type="running_steps_reconciled",
                occurred_at=now,
                details={
                    "reason_code": str(reason_code or "run_internal_error"),
                    "requeued_step_count": len(requeued),
                    "outcome_unknown_step_count": len(unknown),
                },
            )
            current, steps = self._load_run(connection, run_id)
            return {
                "run": self._public_run(current, steps),
                "requeued_step_ids": requeued,
                "outcome_unknown_step_ids": unknown,
            }

    def requeue_failed_steps(
        self,
        run_id: str,
        *,
        expected_revision: int,
        step_ids: list[str],
    ) -> dict[str, Any]:
        expected = require_non_negative_int(expected_revision, field="expected_revision")
        identifiers = [
            require_identifier(step_id, field="retry_failed_steps[]") for step_id in step_ids
        ]
        if not identifiers or len(identifiers) > 64 or len(set(identifiers)) != len(identifiers):
            raise HubV2Error(
                "invalid_request",
                "retry_failed_steps must contain 1..64 unique step ids.",
                scope="run",
            )
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
            if row["status"] != "paused" or row["lease_token_sha256"]:
                raise HubV2Error(
                    "run_not_retryable",
                    "Only an unclaimed paused run can retry failed steps.",
                    scope="run",
                )
            placeholders = ",".join("?" for _ in identifiers)
            selected = connection.execute(
                f"""
                SELECT * FROM steps
                WHERE run_id = ? AND step_id IN ({placeholders})
                """,
                (run_id, *identifiers),
            ).fetchall()
            selected_by_id = {str(item["step_id"]): item for item in selected}
            if set(selected_by_id) != set(identifiers):
                raise HubV2Error(
                    "step_not_found",
                    "A requested retry step was not found.",
                    scope="run",
                )
            for identifier in identifiers:
                step = selected_by_id[identifier]
                checkpoint = _json_object(step["checkpoint_state"])
                if step["status"] != "failed" or checkpoint.get("retry_safe") is not True:
                    raise HubV2Error(
                        "step_not_retryable",
                        "A requested step is not safe to retry.",
                        scope="run",
                        safe_details={"step_id": identifier},
                    )
            for identifier in identifiers:
                previous = _json_object(selected_by_id[identifier]["checkpoint_state"])
                connection.execute(
                    """
                    UPDATE steps
                    SET status = 'queued', revision = revision + 1,
                        provider = NULL, model = NULL,
                        input_artifact_ids = '[]', output_artifact_ids = '[]',
                        checkpoint_state = ?, updated_at = ?
                    WHERE run_id = ? AND step_id = ?
                    """,
                    (
                        canonical_json(
                            {
                                "phase": "retry_queued",
                                "retry_safe": True,
                                "previous_error_code": str(previous.get("error_code") or "unknown"),
                            }
                        ),
                        now,
                        run_id,
                        identifier,
                    ),
                )
            next_revision = expected + 1
            connection.execute(
                """
                UPDATE runs
                SET status = 'queued', revision = ?, updated_at = ?
                WHERE run_id = ? AND revision = ?
                """,
                (next_revision, now, run_id, expected),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                run_revision=next_revision,
                event_type="failed_steps_requeued",
                occurred_at=now,
                details={"reason_code": "explicit_retry", "retryable": True},
            )
            updated, steps = self._load_run(connection, run_id)
            return self._public_run(updated, steps)

    @staticmethod
    def _reconciliation_witness(
        *,
        run_id: str,
        base_revision: int,
        steps: list[Mapping[str, Any]],
    ) -> str:
        """Identity of the exact attempts being adjudicated.

        The run revision alone cannot say *which* attempt a human judged, so the
        witness pins each step's revision, attempt and request digest.
        """

        return digest_json(
            {
                "schema": "agent_hub_reconciliation_witness_v1",
                "run_id": run_id,
                "base_revision": base_revision,
                "steps": [
                    {
                        "step_id": str(step["step_id"]),
                        "step_revision": int(step["revision"]),
                        "attempt": int(step["attempt"]),
                        "provider": step["provider"],
                        "request_sha256": step["checkpoint"].get("request_sha256"),
                        "error_code": step["checkpoint"].get("error_code"),
                    }
                    for step in sorted(steps, key=lambda item: str(item["step_id"]))
                ],
            }
        )

    def prepare_run_reconciliation(
        self,
        run_id: str,
        *,
        expected_revision: int,
        resolutions: Mapping[str, Any],
        ttl_seconds: float = RECONCILIATION_TTL_SECONDS,
    ) -> dict[str, Any]:
        expected = require_non_negative_int(expected_revision, field="expected_revision")
        ttl = min(max(float(ttl_seconds), 60.0), MAX_RECONCILIATION_TTL_SECONDS)
        entries = list(resolutions["resolutions"])
        disposition = str(resolutions["run_disposition"])
        requested = {str(entry["step_id"]) for entry in entries}
        now = self._clock()
        with self._transaction() as connection:
            row, step_rows = self._load_run(connection, run_id)
            if row["revision"] != expected:
                raise HubV2Error(
                    "revision_conflict",
                    "The run revision changed.",
                    scope="run",
                    retryable=True,
                    safe_details={"expected": expected, "current": row["revision"]},
                )
            if row["status"] != "outcome_unknown" or row["lease_token_sha256"]:
                raise HubV2Error(
                    "run_not_reconcilable",
                    "Only an unclaimed outcome_unknown run can be reconciled.",
                    scope="run",
                    safe_details={"status": str(row["status"])},
                )
            if int(row["reconciliation_count"]) >= MAX_RUN_RECONCILIATIONS:
                raise HubV2Error(
                    "reconciliation_limit_exceeded",
                    "The run reached its human reconciliation limit.",
                    scope="run",
                    safe_details={"maximum": MAX_RUN_RECONCILIATIONS},
                )
            by_id = {str(item["step_id"]): item for item in step_rows}
            missing = sorted(requested - set(by_id))
            if missing:
                raise HubV2Error(
                    "step_not_found",
                    "A reconciled step does not exist in this run.",
                    scope="run",
                    safe_details={"step_id": missing[0]},
                )
            for step_id in sorted(requested):
                if by_id[step_id]["status"] != "outcome_unknown":
                    raise HubV2Error(
                        "step_not_reconcilable",
                        "Only an outcome_unknown step can be reconciled.",
                        scope="run",
                        safe_details={"step_id": step_id},
                    )
            ambiguous = {
                str(item["step_id"]) for item in step_rows if item["status"] == "outcome_unknown"
            }
            if ambiguous - requested:
                # Resuming with an unjudged ambiguous step left behind would run the
                # DAG on top of an unknown external outcome.
                raise HubV2Error(
                    "reconciliation_incomplete",
                    "Every outcome_unknown step must be judged together.",
                    scope="run",
                    safe_details={"step_id": sorted(ambiguous - requested)[0]},
                )
            if disposition == "resume" and not self._reconciliation_resumable(
                plan=_json_object(row["plan_json"]),
                steps=step_rows,
                entries=entries,
            ):
                raise HubV2Error(
                    "reconciliation_not_resumable",
                    "The reconciled run has no dependency-ready step left to execute.",
                    scope="run",
                )
            connection.execute(
                "UPDATE run_reconciliations SET status = 'superseded' "
                "WHERE run_id = ? AND status = 'pending'",
                (run_id,),
            )
            witness = self._reconciliation_witness(
                run_id=run_id,
                base_revision=expected,
                steps=[by_id[step_id] for step_id in sorted(requested)],
            )
            reconciliation_id = f"rec_{secrets.token_hex(12)}"
            resend = any(entry["verdict"] == "not_delivered" for entry in entries)
            # The phrase itself states what is being approved.
            phrase = ("resend-" if resend else "discard-") + secrets.token_hex(4)
            proposal = {
                "schema": RECONCILIATION_SCHEMA,
                "reconciliation_id": reconciliation_id,
                "run_id": run_id,
                "base_revision": expected,
                "run_disposition": disposition,
                "resolutions": entries,
                "witness_sha256": witness,
                "expires_at": now + ttl,
                "resend_requested": resend,
            }
            proposal["proposal_sha256"] = sha256(
                canonical_json(proposal).encode("utf-8")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO run_reconciliations(
                    reconciliation_id, run_id, base_revision, witness_sha256,
                    proposal_sha256, confirmation_sha256, run_disposition,
                    resolutions_json, status, created_at, expires_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    reconciliation_id,
                    run_id,
                    expected,
                    witness,
                    proposal["proposal_sha256"],
                    sha256(phrase.encode("ascii")).hexdigest(),
                    disposition,
                    canonical_json(entries),
                    now,
                    now + ttl,
                ),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                run_revision=int(row["revision"]),
                event_type="reconciliation_prepared",
                occurred_at=now,
                details={
                    "reconciliation_id": reconciliation_id,
                    "run_disposition": disposition,
                    "step_count": len(entries),
                    "witness_sha256": witness,
                    "reason_code": "resend_requested" if resend else "discard_only",
                },
            )
        return {
            **proposal,
            "confirmation_phrase": phrase,
            "confirmation_prompt": (
                "Re-send an external request that may already have been delivered."
                if resend
                else "Close the ambiguous steps without re-sending anything."
            ),
        }

    @staticmethod
    def _reconciliation_resumable(
        *,
        plan: Mapping[str, Any],
        steps: list[Mapping[str, Any]],
        entries: list[Mapping[str, Any]],
    ) -> bool:
        verdicts = {str(entry["step_id"]): str(entry["verdict"]) for entry in entries}
        after: dict[str, str] = {}
        for item in steps:
            step_id = str(item["step_id"])
            verdict = verdicts.get(step_id)
            if verdict == "not_delivered":
                after[step_id] = "queued"
            elif verdict == "delivered_recovered":
                after[step_id] = "completed"
            elif verdict == "delivered_discarded":
                after[step_id] = "failed"
            else:
                after[step_id] = str(item["status"])
        depends = {
            str(step["id"]): [str(dep) for dep in step.get("depends_on") or []]
            for step in plan.get("steps") or []
        }
        return any(
            status == "queued"
            and all(after.get(dep) == "completed" for dep in depends.get(step_id, []))
            for step_id, status in after.items()
        )

    def apply_run_reconciliation(
        self,
        run_id: str,
        *,
        expected_revision: int,
        proposal: Mapping[str, Any],
        proposal_sha256: str,
        confirmation_phrase: str,
        recovered_artifacts: Mapping[str, str],
    ) -> dict[str, Any]:
        expected = require_non_negative_int(expected_revision, field="expected_revision")
        unsigned = {key: value for key, value in proposal.items() if key != "proposal_sha256"}
        embedded = str(proposal.get("proposal_sha256") or "")
        if (
            embedded != sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
            or embedded != proposal_sha256
        ):
            raise HubV2Error(
                "proposal_digest_conflict",
                "The reconciliation proposal digest does not match.",
                scope="run",
            )
        # require_identifier first so a non-ASCII phrase never reaches compare_digest.
        phrase = require_identifier(confirmation_phrase, field="confirmation_phrase")
        now = self._clock()
        with self._transaction() as connection:
            record = connection.execute(
                "SELECT * FROM run_reconciliations WHERE reconciliation_id = ?",
                (str(proposal.get("reconciliation_id") or ""),),
            ).fetchone()
            if record is None or record["status"] != "pending":
                raise HubV2Error(
                    "reconciliation_not_found",
                    "The reconciliation proposal is no longer pending.",
                    scope="run",
                    safe_details={"status": str(record["status"]) if record else "missing"},
                )
            if record["proposal_sha256"] != proposal_sha256 or record["run_id"] != run_id:
                raise HubV2Error(
                    "proposal_digest_conflict",
                    "The reconciliation proposal digest does not match.",
                    scope="run",
                )
            if not secrets.compare_digest(
                sha256(phrase.encode("ascii")).hexdigest(),
                str(record["confirmation_sha256"]),
            ):
                raise HubV2Error(
                    "reconciliation_confirmation_mismatch",
                    "The confirmation phrase does not match the prepared reconciliation.",
                    scope="run",
                )
            if float(record["expires_at"]) <= now:
                connection.execute(
                    "UPDATE run_reconciliations SET status = 'expired' WHERE reconciliation_id = ?",
                    (record["reconciliation_id"],),
                )
                raise HubV2Error(
                    "reconciliation_expired",
                    "The reconciliation proposal expired. Prepare it again.",
                    scope="run",
                    retryable=True,
                    next_action={
                        "type": "call_tool",
                        "tool": "agent_hub_cancel",
                        "arguments": {"action": "prepare_reconcile", "run_id": run_id},
                    },
                )
            row, step_rows = self._load_run(connection, run_id)
            if row["revision"] != expected or int(record["base_revision"]) != expected:
                raise HubV2Error(
                    "revision_conflict",
                    "The run revision changed.",
                    scope="run",
                    retryable=True,
                    safe_details={"expected": expected, "current": row["revision"]},
                )
            if row["status"] != "outcome_unknown" or row["lease_token_sha256"]:
                raise HubV2Error(
                    "run_not_reconcilable",
                    "Only an unclaimed outcome_unknown run can be reconciled.",
                    scope="run",
                    safe_details={"status": str(row["status"])},
                )
            entries = [dict(item) for item in _json_list(record["resolutions_json"])]
            by_id = {str(item["step_id"]): item for item in step_rows}
            witness = self._reconciliation_witness(
                run_id=run_id,
                base_revision=expected,
                steps=[by_id[str(entry["step_id"])] for entry in entries],
            )
            if witness != str(record["witness_sha256"]):
                # The judged attempt is no longer the live attempt.
                raise HubV2Error(
                    "reconciliation_witness_conflict",
                    "The reconciled steps changed after preparation.",
                    scope="run",
                    retryable=True,
                )
            next_revision = expected + 1
            for entry in sorted(entries, key=lambda item: str(item["step_id"])):
                step_id = str(entry["step_id"])
                verdict = str(entry["verdict"])
                if verdict == "not_delivered":
                    status, checkpoint = (
                        "queued",
                        {
                            "phase": "reconciled_requeued",
                            "retry_safe": True,
                            "result_origin": "human_reconciliation",
                            "verdict": verdict,
                        },
                    )
                elif verdict == "delivered_recovered":
                    status, checkpoint = (
                        "completed",
                        {
                            "phase": "reconciled_recovered",
                            "retry_safe": False,
                            "result_origin": "human_reconciliation",
                            "verdict": verdict,
                            "result_sha256": str(entry.get("result_sha256") or ""),
                        },
                    )
                else:
                    status, checkpoint = (
                        "failed",
                        {
                            "phase": "reconciled_discarded",
                            "retry_safe": False,
                            "result_origin": "human_reconciliation",
                            "verdict": verdict,
                            "error_code": "delivered_discarded",
                        },
                    )
                artifact_id = recovered_artifacts.get(step_id)
                connection.execute(
                    """
                    UPDATE steps
                    SET status = ?, revision = revision + 1, checkpoint_state = ?,
                        output_artifact_ids = ?, updated_at = ?
                    WHERE run_id = ? AND step_id = ?
                    """,
                    (
                        status,
                        canonical_json({**by_id[step_id]["checkpoint"], **checkpoint}),
                        canonical_json([artifact_id] if artifact_id else []),
                        now,
                        run_id,
                        step_id,
                    ),
                )
            run_status = "queued" if record["run_disposition"] == "resume" else "failed"
            connection.execute(
                """
                UPDATE runs
                SET status = ?, revision = ?, reconciliation_count = reconciliation_count + 1,
                    updated_at = ?
                WHERE run_id = ? AND revision = ?
                """,
                (run_status, next_revision, now, run_id, expected),
            )
            connection.execute(
                "UPDATE run_reconciliations SET status = 'applied', applied_at = ? "
                "WHERE reconciliation_id = ?",
                (now, record["reconciliation_id"]),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                run_revision=next_revision,
                event_type="run_reconciled",
                occurred_at=now,
                details={
                    "reconciliation_id": str(record["reconciliation_id"]),
                    "run_disposition": str(record["run_disposition"]),
                    "step_count": len(entries),
                    "witness_sha256": witness,
                    "reason_code": "human_reconciliation",
                },
            )
            updated, steps = self._load_run(connection, run_id)
            return self._public_run(updated, steps)

    def grant_token_budget(
        self,
        run_id: str,
        *,
        expected_revision: int,
        additional_tokens: int,
    ) -> dict[str, Any]:
        """Add budget to a run whose sealed plan limit is already spent.

        The plan is fenced by plan_sha256, so the limit itself must stay
        untouched; the grant is a separate run-scoped addition.
        """

        expected = require_non_negative_int(expected_revision, field="expected_revision")
        additional = require_non_negative_int(additional_tokens, field="token_budget_grant")
        if additional < 1:
            raise HubV2Error(
                "invalid_request",
                "token_budget_grant must be at least 1.",
                scope="run",
            )
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
            if row["status"] not in {"queued", "paused"} or row["lease_token_sha256"]:
                raise HubV2Error(
                    "token_grant_not_allowed",
                    "Only an unclaimed queued or paused run can receive a token budget grant.",
                    scope="run",
                    safe_details={"status": str(row["status"])},
                )
            granted = int(row["token_budget_grant"] or 0) + additional
            if granted > MAX_RUN_TOKEN_GRANT:
                raise HubV2Error(
                    "token_grant_limit_exceeded",
                    "The run reached its maximum additional token budget.",
                    scope="run",
                    safe_details={
                        "tokens_granted": int(row["token_budget_grant"] or 0),
                        "tokens_requested": additional,
                    },
                )
            next_revision = expected + 1
            # Status stays as-is so a following requeue_failed_steps still passes.
            connection.execute(
                """
                UPDATE runs
                SET token_budget_grant = token_budget_grant + ?, revision = ?, updated_at = ?
                WHERE run_id = ? AND revision = ?
                """,
                (additional, next_revision, now, run_id, expected),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                run_revision=next_revision,
                event_type="run_token_budget_granted",
                occurred_at=now,
                details={
                    "reason_code": "explicit_grant",
                    "tokens_granted": additional,
                    "tokens_budget": int(row["token_budget_limit"] or 0) + granted,
                },
            )
            updated, steps = self._load_run(connection, run_id)
            return self._public_run(updated, steps)

    def record_handoff_snapshot(
        self,
        *,
        scope_identity: str,
        scope_root: str,
        target_alias: str,
        project_root: str,
        file_sha256: str,
        managed_sha256: str,
        body: str,
        sections: Mapping[str, Any],
        next_step: str,
        previous_file_sha256: str | None = None,
        previous_managed_sha256: str | None = None,
        redacted_lines: int = 0,
        created: bool = False,
    ) -> dict[str, Any]:
        """Store an applied HANDOFF packet so its history survives an overwrite."""

        truncated = len(body) > MAX_HANDOFF_SNAPSHOT_CHARS
        stored = body[:MAX_HANDOFF_SNAPSHOT_CHARS]
        now = self._clock()
        snapshot_id = f"hs_{secrets.token_hex(12)}"
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT MAX(sequence) AS latest FROM handoff_snapshots "
                "WHERE scope_identity = ? AND target_alias = ?",
                (scope_identity, target_alias),
            ).fetchone()
            sequence = int(row["latest"] or 0) + 1
            connection.execute(
                """
                INSERT INTO handoff_snapshots(
                    snapshot_id, scope_identity, scope_root, target_alias,
                    last_project_root, sequence, origin, file_sha256, managed_sha256,
                    previous_file_sha256, previous_managed_sha256, body, body_sha256,
                    body_chars, body_truncated, redacted_lines, sections_json,
                    next_step, created, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'applied', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    scope_identity,
                    scope_root,
                    target_alias,
                    project_root,
                    sequence,
                    file_sha256,
                    managed_sha256,
                    previous_file_sha256,
                    previous_managed_sha256,
                    stored,
                    sha256(stored.encode("utf-8")).hexdigest(),
                    len(stored),
                    int(truncated),
                    max(0, int(redacted_lines)),
                    canonical_json(dict(sections)),
                    str(next_step)[:MAX_SAFE_STRING],
                    int(bool(created)),
                    now,
                ),
            )
            connection.execute(
                """
                DELETE FROM handoff_snapshots
                WHERE scope_identity = ? AND target_alias = ? AND sequence <= ?
                """,
                (scope_identity, target_alias, sequence - HANDOFF_SNAPSHOT_RETENTION),
            )
        return {
            "snapshot_id": snapshot_id,
            "sequence": sequence,
            "body_truncated": truncated,
            "redacted_lines": max(0, int(redacted_lines)),
        }

    def handoff_history(
        self,
        *,
        scope_identity: str,
        target_alias: str,
        limit: int = 20,
        include_body: bool = False,
    ) -> dict[str, Any]:
        bounded = min(max(int(limit), 1), 5 if include_body else HANDOFF_SNAPSHOT_RETENTION)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM handoff_snapshots
                WHERE scope_identity = ? AND target_alias = ?
                ORDER BY sequence DESC LIMIT ?
                """,
                (scope_identity, target_alias, bounded),
            ).fetchall()
        finally:
            connection.close()
        return {
            "schema": "agent_hub_handoff_history_v1",
            "target_alias": target_alias,
            # Always reported so a caller can tell that older entries were pruned.
            "retention_limit": HANDOFF_SNAPSHOT_RETENTION,
            "snapshots": [self._public_handoff_snapshot(row, include_body) for row in rows],
        }

    def handoff_snapshot(
        self,
        *,
        scope_identity: str,
        target_alias: str,
        sequence: int | None = None,
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            if sequence is None:
                row = connection.execute(
                    "SELECT * FROM handoff_snapshots WHERE scope_identity = ? "
                    "AND target_alias = ? ORDER BY sequence DESC LIMIT 1",
                    (scope_identity, target_alias),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM handoff_snapshots WHERE scope_identity = ? "
                    "AND target_alias = ? AND sequence = ?",
                    (
                        scope_identity,
                        target_alias,
                        require_non_negative_int(sequence, field="sequence"),
                    ),
                ).fetchone()
        finally:
            connection.close()
        return self._public_handoff_snapshot(row, True) if row is not None else None

    @staticmethod
    def _public_handoff_snapshot(row: sqlite3.Row, include_body: bool) -> dict[str, Any]:
        payload = {
            "snapshot_id": row["snapshot_id"],
            "sequence": int(row["sequence"]),
            "origin": row["origin"],
            "file_sha256": row["file_sha256"],
            "managed_sha256": row["managed_sha256"],
            "previous_managed_sha256": row["previous_managed_sha256"],
            "body_sha256": row["body_sha256"],
            "body_chars": int(row["body_chars"]),
            "body_truncated": bool(row["body_truncated"]),
            "redacted_lines": int(row["redacted_lines"]),
            "sections": _json_object(row["sections_json"]),
            "next_step": row["next_step"],
            "created": bool(row["created"]),
            "recorded_at": row["recorded_at"],
        }
        if include_body:
            payload["body"] = row["body"]
        return payload

    def capability_token_estimate(
        self,
        *,
        capability: str,
        provider: str | None = None,
        lookback_days: float = 30.0,
        minimum_samples: int = 3,
    ) -> dict[str, Any]:
        """Median observed token spend for a capability, for pre-wave forecasting.

        Read from the step ledger rather than a parallel observation table. A
        completed step already records what the provider billed, under the
        capability and provider that produced it, and it is the same number the
        budget gate spends against -- so forecasting from anywhere else invites
        the forecast and the ledger to disagree.
        """

        cutoff = self._clock() - max(0.0, float(lookback_days)) * 86400.0
        connection = self._connect()
        try:

            def _totals(with_provider: bool) -> list[int]:
                sql = (
                    "SELECT total_tokens FROM steps "
                    "WHERE capability = ? AND status = 'completed' "
                    "AND total_tokens > 0 AND updated_at >= ?"
                )
                params: list[Any] = [str(capability), cutoff]
                if with_provider:
                    sql += " AND provider = ?"
                    params.append(str(provider))
                sql += " ORDER BY total_tokens"
                return [int(row["total_tokens"]) for row in connection.execute(sql, params)]

            source = "insufficient_samples"
            totals: list[int] = []
            if provider:
                candidate = _totals(True)
                if len(candidate) >= minimum_samples:
                    totals, source = candidate, "step_ledger_provider"
            if not totals:
                candidate = _totals(False)
                if len(candidate) >= minimum_samples:
                    totals, source = candidate, "step_ledger_capability"
        finally:
            connection.close()
        return {
            "schema": "agent_hub_token_estimate_v1",
            "capability": str(capability),
            "provider": str(provider) if provider else None,
            "sample_count": len(totals),
            # median_low keeps the estimate an observed integer, not an average.
            "median_total_tokens": median_low(totals) if totals else None,
            "source": source,
        }

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
            if row["status"] in {
                "completed",
                "failed",
                "cancelled",
                "archived",
                "outcome_unknown",
            }:
                raise HubV2Error(
                    "run_not_cancellable",
                    "The run is already terminal.",
                    scope="run",
                )
            next_revision = expected + 1
            cancelled_steps = 0
            for step in connection.execute(
                """
                SELECT step_id, checkpoint_state
                FROM steps
                WHERE run_id = ? AND status IN ('queued', 'running')
                """,
                (run_id,),
            ).fetchall():
                checkpoint = {
                    **_json_object(step["checkpoint_state"]),
                    "phase": "cancelled",
                    "retry_safe": False,
                    "reason_code": "user_cancelled",
                    "late_result_ignored": True,
                }
                connection.execute(
                    """
                    UPDATE steps
                    SET status = 'cancelled', revision = revision + 1,
                        checkpoint_state = ?, updated_at = ?
                    WHERE run_id = ? AND step_id = ?
                    """,
                    (
                        canonical_json(checkpoint),
                        now,
                        run_id,
                        step["step_id"],
                    ),
                )
                cancelled_steps += 1
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
                details={
                    "reason_code": "user_cancelled",
                    "cancelled_step_count": cancelled_steps,
                },
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
            spent_tokens = {
                str(row["step_id"]): (
                    int(row["input_tokens"]),
                    int(row["output_tokens"]),
                    int(row["total_tokens"]),
                    str(row["tokens_source"] or "unset"),
                )
                for row in connection.execute(
                    "SELECT step_id, input_tokens, output_tokens, total_tokens, tokens_source "
                    "FROM steps WHERE run_id = ? AND status != 'completed'",
                    (run_id,),
                ).fetchall()
            }
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
                        run_id, step_id, capability, status, revision, updated_at,
                        input_tokens, output_tokens, total_tokens, tokens_source
                    ) VALUES(?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        step["id"],
                        step["capability"],
                        now,
                        # Carry the spend across the replan. A replanned step is a
                        # new attempt, not a refund: dropping these to 0 would let
                        # auto-replan reset the budget ledger while keeping the limit.
                        *spent_tokens.get(str(step["id"]), (0, 0, 0, "unset")),
                    ),
                )
            next_revision = expected + 1
            plan_sha = str(candidate_plan.get("plan_sha256") or digest_json(candidate_plan))
            connection.execute(
                """
                UPDATE runs
                SET status = 'queued', revision = ?, plan_sha256 = ?, plan_json = ?,
                    replan_count = replan_count + 1, token_budget_limit = ?, updated_at = ?
                WHERE run_id = ? AND revision = ?
                """,
                (
                    next_revision,
                    plan_sha,
                    canonical_json(candidate_plan),
                    # Keep the column a pure derivative of the sealed plan. The
                    # user-supplied grant is deliberately preserved across replans.
                    _plan_token_budget(candidate_plan),
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
                if (
                    connection.execute(
                        "SELECT 1 FROM artifacts WHERE artifact_id = ?",
                        (source_id,),
                    ).fetchone()
                    is None
                ):
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
            if (
                connection.execute(
                    "SELECT 1 FROM artifacts WHERE artifact_id = ?",
                    (identifier,),
                ).fetchone()
                is None
            ):
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
            rows = connection.execute(
                """
                SELECT artifact_id, verification_json
                FROM artifacts
                WHERE delete_after IS NOT NULL AND delete_after <= ?
                  AND content IS NOT NULL
                ORDER BY artifact_id
                """,
                (now,),
            ).fetchall()
            for row in rows:
                verification = _json_object(row["verification_json"])
                verification["content_pruned"] = True
                verification["content_pruned_at"] = now
                connection.execute(
                    """
                    UPDATE artifacts
                    SET content = NULL,
                        verification_json = ?,
                        retention = 'metadata_only',
                        delete_after = NULL
                    WHERE artifact_id = ?
                    """,
                    (canonical_json(verification), row["artifact_id"]),
                )
        return {
            "schema": "agent_hub_artifact_retention_v2",
            "pruned_content_count": len(rows),
            "deleted_metadata_count": 0,
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

    def prune_context_documents(
        self,
        *,
        project_identity: str,
        namespace: str,
        keep_path_aliases: list[str],
    ) -> int:
        keep = set(keep_path_aliases)
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT document_id, path_alias
                FROM context_documents
                WHERE project_identity = ? AND namespace = ?
                """,
                (project_identity, namespace),
            ).fetchall()
            stale = [str(row["document_id"]) for row in rows if str(row["path_alias"]) not in keep]
            for document_id in stale:
                connection.execute(
                    "DELETE FROM context_fts WHERE document_id = ?",
                    (document_id,),
                )
                connection.execute(
                    "DELETE FROM context_documents WHERE document_id = ?",
                    (document_id,),
                )
        return len(stale)

    @staticmethod
    def _public_egress_review(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": "agent_hub_egress_review_v1",
            "review_id": str(row["review_id"]),
            "proposal_sha256": str(row["proposal_sha256"]),
            "manifest_sha256": str(row["manifest_sha256"]),
            "project_root": str(row["project_root"]),
            "policy_revision": int(row["policy_revision"]),
            "provider": str(row["provider"]),
            "model": str(row["model"]) if row["model"] is not None else None,
            "destinations": _json_list(row["destinations_json"]),
            "entries": _json_list(row["entries_json"]),
            "status": str(row["status"]),
            "created_at": float(row["created_at"]),
            "expires_at": float(row["expires_at"]),
            "decided_at": (float(row["decided_at"]) if row["decided_at"] is not None else None),
            "consumed_at": (float(row["consumed_at"]) if row["consumed_at"] is not None else None),
            "decision_source": (
                str(row["decision_source"]) if row["decision_source"] is not None else None
            ),
            "decision_settings_revision": (
                int(row["decision_settings_revision"])
                if row["decision_settings_revision"] is not None
                else None
            ),
        }

    def egress_settings(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT revision, auto_approve, updated_at FROM egress_settings "
                "WHERE singleton_id = 1"
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise HubV2Error(
                "store_migration_failed",
                "The global egress settings are unavailable.",
                scope="store",
            )
        return {
            "schema": "agent_hub_egress_settings_v1",
            "revision": int(row["revision"]),
            "auto_approve": bool(row["auto_approve"]),
            "updated_at": float(row["updated_at"]),
        }

    def update_egress_settings(
        self,
        *,
        auto_approve: bool,
        expected_revision: int,
    ) -> dict[str, Any]:
        if not isinstance(auto_approve, bool):
            raise HubV2Error(
                "invalid_request",
                "auto_approve must be a boolean.",
                scope="egress",
            )
        revision = require_non_negative_int(
            expected_revision,
            field="expected_revision",
        )
        now = self._clock()
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE egress_settings
                SET auto_approve = ?, revision = revision + 1, updated_at = ?
                WHERE singleton_id = 1 AND revision = ?
                """,
                (int(auto_approve), now, revision),
            )
            if updated.rowcount != 1:
                current = connection.execute(
                    "SELECT revision FROM egress_settings WHERE singleton_id = 1"
                ).fetchone()
                raise HubV2Error(
                    "egress_settings_revision_conflict",
                    "The global egress setting changed. Refresh and try again.",
                    scope="egress",
                    retryable=True,
                    safe_details={
                        "expected_revision": revision,
                        "current_revision": int(current["revision"]) if current else -1,
                    },
                )
            row = connection.execute(
                "SELECT revision, auto_approve, updated_at FROM egress_settings "
                "WHERE singleton_id = 1"
            ).fetchone()
        assert row is not None
        return {
            "schema": "agent_hub_egress_settings_v1",
            "revision": int(row["revision"]),
            "auto_approve": bool(row["auto_approve"]),
            "updated_at": float(row["updated_at"]),
        }

    def maybe_auto_approve_egress_review(self, review_id: str) -> dict[str, Any]:
        review = require_identifier(review_id, field="review_id")
        now = self._clock()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM egress_reviews WHERE review_id = ?",
                (review,),
            ).fetchone()
            if row is None:
                raise HubV2Error(
                    "egress_review_not_found",
                    "The egress review was not found.",
                    scope="egress",
                )
            if row["status"] != "pending":
                return self._public_egress_review(row)
            setting = connection.execute(
                "SELECT revision, auto_approve FROM egress_settings WHERE singleton_id = 1"
            ).fetchone()
            if setting is None or not bool(setting["auto_approve"]):
                return self._public_egress_review(row)
            if float(row["expires_at"]) <= now:
                connection.execute(
                    "UPDATE egress_reviews SET status = 'expired' WHERE review_id = ?",
                    (review,),
                )
                raise HubV2Error(
                    "egress_review_expired",
                    "The egress review expired. Prepare the plan again.",
                    scope="egress",
                    retryable=True,
                )
            connection.execute(
                """
                UPDATE egress_reviews
                SET status = 'approved', decided_at = ?,
                    decision_source = 'global_auto_approve',
                    decision_settings_revision = ?
                WHERE review_id = ? AND status = 'pending'
                """,
                (now, int(setting["revision"]), review),
            )
            updated = connection.execute(
                "SELECT * FROM egress_reviews WHERE review_id = ?",
                (review,),
            ).fetchone()
        assert updated is not None
        return self._public_egress_review(updated)

    @staticmethod
    def _require_sha256(value: Any, *, field: str) -> str:
        digest = str(value or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise HubV2Error(
                "invalid_egress_proposal",
                f"{field} must be a SHA-256 digest.",
                scope="egress",
            )
        return digest

    def prepare_egress_review(
        self,
        *,
        project_root: str,
        proposal: Mapping[str, Any],
        ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        if isinstance(ttl_seconds, bool) or not 60 <= ttl_seconds <= 3600:
            raise HubV2Error(
                "invalid_request",
                "The egress review TTL is outside the supported range.",
                scope="egress",
            )
        proposal_sha = self._require_sha256(
            proposal.get("proposal_sha256"),
            field="proposal_sha256",
        )
        manifest = proposal.get("manifest")
        if not isinstance(manifest, Mapping):
            raise HubV2Error(
                "invalid_egress_proposal",
                "The egress proposal has no manifest.",
                scope="egress",
            )
        manifest_sha = self._require_sha256(
            manifest.get("manifest_sha256"),
            field="manifest_sha256",
        )
        root = canonical_project_root(project_root)
        policy_revision = require_non_negative_int(
            manifest.get("policy_revision"),
            field="policy_revision",
        )
        destinations = list(
            dict.fromkeys(
                str(item)
                for item in manifest.get("destinations") or [proposal.get("provider")]
                if str(item or "")
            )
        )
        entries = []
        for raw in manifest.get("entries") or []:
            if not isinstance(raw, Mapping):
                continue
            entries.append(
                {
                    "kind": str(raw.get("kind") or "repository"),
                    "artifact_id": str(raw.get("artifact_id") or ""),
                    "path_alias": str(raw.get("path_alias") or ""),
                    "sha256": str(raw.get("sha256") or ""),
                    "chars": int(raw.get("chars") or 0),
                    "classification": str(raw.get("classification") or ""),
                }
            )
        now = self._clock()
        expires_at = now + float(ttl_seconds)
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE egress_reviews
                SET status = 'expired'
                WHERE status IN ('pending', 'approved') AND expires_at <= ?
                """,
                (now,),
            )
            existing = connection.execute(
                """
                SELECT * FROM egress_reviews
                WHERE proposal_sha256 = ? AND manifest_sha256 = ?
                  AND project_root = ? AND policy_revision = ?
                  AND status IN ('pending', 'approved') AND expires_at > ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (proposal_sha, manifest_sha, root, policy_revision, now),
            ).fetchone()
            if existing is not None:
                return self._public_egress_review(existing)
            review_id = f"egr_{secrets.token_hex(16)}"
            connection.execute(
                """
                INSERT INTO egress_reviews(
                    review_id, proposal_sha256, manifest_sha256, project_root,
                    policy_revision, provider, model, destinations_json, entries_json,
                    status, created_at, expires_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    review_id,
                    proposal_sha,
                    manifest_sha,
                    root,
                    policy_revision,
                    str(proposal.get("provider") or ""),
                    str(proposal.get("model")) if proposal.get("model") else None,
                    canonical_json(destinations),
                    canonical_json(entries),
                    now,
                    expires_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM egress_reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
        assert row is not None
        return self._public_egress_review(row)

    def list_egress_reviews(self) -> list[dict[str, Any]]:
        now = self._clock()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE egress_reviews
                SET status = 'expired'
                WHERE status IN ('pending', 'approved') AND expires_at <= ?
                """,
                (now,),
            )
            rows = connection.execute(
                """
                SELECT * FROM egress_reviews
                WHERE status IN ('pending', 'approved')
                ORDER BY created_at DESC
                LIMIT 100
                """
            ).fetchall()
        return [self._public_egress_review(row) for row in rows]

    def decide_egress_review(
        self,
        review_id: str,
        *,
        decision: str,
    ) -> dict[str, Any]:
        review = require_identifier(review_id, field="review_id")
        if decision not in {"approve", "reject"}:
            raise HubV2Error(
                "invalid_request",
                "The egress review decision is not supported.",
                scope="egress",
            )
        now = self._clock()
        target = "approved" if decision == "approve" else "rejected"
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM egress_reviews WHERE review_id = ?",
                (review,),
            ).fetchone()
            if row is None:
                raise HubV2Error(
                    "egress_review_not_found",
                    "The egress review was not found.",
                    scope="egress",
                )
            if float(row["expires_at"]) <= now:
                connection.execute(
                    "UPDATE egress_reviews SET status = 'expired' WHERE review_id = ?",
                    (review,),
                )
                raise HubV2Error(
                    "egress_review_expired",
                    "The egress review expired. Prepare the plan again.",
                    scope="egress",
                    retryable=True,
                    next_action={"type": "call_tool", "tool": "agent_hub_plan", "mode": "prepare"},
                )
            if row["status"] != "pending":
                raise HubV2Error(
                    "egress_review_already_decided",
                    "The egress review was already decided.",
                    scope="egress",
                )
            connection.execute(
                """
                UPDATE egress_reviews
                SET status = ?, decided_at = ?, decision_source = 'local_gui',
                    decision_settings_revision = NULL
                WHERE review_id = ? AND status = 'pending'
                """,
                (target, now, review),
            )
            updated = connection.execute(
                "SELECT * FROM egress_reviews WHERE review_id = ?",
                (review,),
            ).fetchone()
        assert updated is not None
        return self._public_egress_review(updated)

    def consume_egress_review(
        self,
        review_id: str,
        *,
        project_root: str,
        proposal_sha256: str,
        manifest_sha256: str,
        policy_revision: int,
    ) -> dict[str, Any]:
        if not str(review_id or ""):
            raise HubV2Error(
                "egress_human_approval_required",
                "Approve this egress request in the local Agent Hub GUI.",
                scope="egress",
                next_action={"type": "local_gui", "command": "agent-hub-connect"},
            )
        review = require_identifier(review_id, field="approval_request_id")
        proposal_sha = self._require_sha256(
            proposal_sha256,
            field="proposal_sha256",
        )
        manifest_sha = self._require_sha256(
            manifest_sha256,
            field="manifest_sha256",
        )
        root = canonical_project_root(project_root)
        revision = require_non_negative_int(policy_revision, field="policy_revision")
        now = self._clock()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM egress_reviews WHERE review_id = ?",
                (review,),
            ).fetchone()
            if row is None:
                raise HubV2Error(
                    "egress_human_approval_required",
                    "Approve this egress request in the local Agent Hub GUI.",
                    scope="egress",
                    next_action={"type": "local_gui", "command": "agent-hub-connect"},
                )
            if float(row["expires_at"]) <= now:
                connection.execute(
                    "UPDATE egress_reviews SET status = 'expired' WHERE review_id = ?",
                    (review,),
                )
                raise HubV2Error(
                    "egress_review_expired",
                    "The egress review expired. Prepare the plan again.",
                    scope="egress",
                    retryable=True,
                    next_action={"type": "call_tool", "tool": "agent_hub_plan", "mode": "prepare"},
                )
            if (
                row["proposal_sha256"] != proposal_sha
                or row["manifest_sha256"] != manifest_sha
                or row["project_root"] != root
                or int(row["policy_revision"]) != revision
            ):
                raise HubV2Error(
                    "egress_approval_conflict",
                    "The GUI approval belongs to different planner inputs.",
                    scope="egress",
                )
            if row["status"] == "consumed":
                raise HubV2Error(
                    "egress_review_already_consumed",
                    "The one-time egress approval was already consumed.",
                    scope="egress",
                )
            if row["status"] == "rejected":
                raise HubV2Error(
                    "egress_review_rejected",
                    "The local user rejected this egress request.",
                    scope="egress",
                )
            if row["status"] != "approved":
                raise HubV2Error(
                    "egress_human_approval_required",
                    "Approve this egress request in the local Agent Hub GUI.",
                    scope="egress",
                    next_action={"type": "local_gui", "command": "agent-hub-connect"},
                    safe_details={"review_status": str(row["status"])},
                )
            updated = connection.execute(
                """
                UPDATE egress_reviews
                SET status = 'consumed', consumed_at = ?
                WHERE review_id = ? AND status = 'approved'
                """,
                (now, review),
            )
            if updated.rowcount != 1:
                raise HubV2Error(
                    "egress_review_already_consumed",
                    "The one-time egress approval was already consumed.",
                    scope="egress",
                )
            consumed = connection.execute(
                "SELECT * FROM egress_reviews WHERE review_id = ?",
                (review,),
            ).fetchone()
        assert consumed is not None
        return self._public_egress_review(consumed)

    def record_egress_approval(
        self,
        *,
        project_root: str,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        digest = str(manifest.get("manifest_sha256") or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise HubV2Error(
                "invalid_egress_manifest",
                "The approved egress manifest digest is invalid.",
                scope="egress",
            )
        root = canonical_project_root(project_root)
        entries = []
        for raw in manifest.get("entries") or []:
            if not isinstance(raw, Mapping):
                continue
            entries.append(
                {
                    "kind": str(raw.get("kind") or "repository"),
                    "artifact_id": str(raw.get("artifact_id") or ""),
                    "path_alias": str(raw.get("path_alias") or ""),
                    "sha256": str(raw.get("sha256") or ""),
                    "source_sha256": str(raw.get("source_sha256") or ""),
                    "chars": int(raw.get("chars") or 0),
                    "classification": str(raw.get("classification") or ""),
                }
            )
        policy_revision = require_non_negative_int(
            manifest.get("policy_revision"),
            field="policy_revision",
        )
        destinations = list(
            dict.fromkeys(
                str(item)
                for item in manifest.get("destinations") or [manifest.get("provider")]
                if str(item or "")
            )
        )
        encoded = canonical_json(entries)
        encoded_destinations = canonical_json(destinations)
        created_at = self._clock()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM egress_approvals WHERE manifest_sha256 = ?",
                (digest,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["project_root"] != root
                    or int(existing["policy_revision"]) != policy_revision
                    or existing["destinations_json"] != encoded_destinations
                    or existing["entries_json"] != encoded
                ):
                    raise HubV2Error(
                        "egress_approval_conflict",
                        "The approved egress manifest conflicts with stored metadata.",
                        scope="egress",
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO egress_approvals(
                        manifest_sha256, project_root, policy_revision,
                        destinations_json, entries_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        digest,
                        root,
                        policy_revision,
                        encoded_destinations,
                        encoded,
                        created_at,
                    ),
                )
        return {
            "schema": "agent_hub_egress_approval_v1",
            "manifest_sha256": digest,
            "project_root": root,
            "policy_revision": policy_revision,
            "destinations": destinations,
            "entries": entries,
            "created_at": created_at if existing is None else float(existing["created_at"]),
        }

    def get_egress_approval(self, manifest_sha256: str) -> dict[str, Any] | None:
        digest = str(manifest_sha256 or "")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM egress_approvals WHERE manifest_sha256 = ?",
                (digest,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return {
            "schema": "agent_hub_egress_approval_v1",
            "manifest_sha256": row["manifest_sha256"],
            "project_root": row["project_root"],
            "policy_revision": int(row["policy_revision"]),
            "destinations": _json_list(row["destinations_json"]),
            "entries": _json_list(row["entries_json"]),
            "created_at": float(row["created_at"]),
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
                    SELECT f.document_id, f.path_alias, f.namespace, f.content,
                           snippet(context_fts, 4, '', '', ' … ', 40) AS excerpt,
                           bm25(context_fts) AS rank,
                           d.content_sha256, d.complete, d.indexed_at
                    FROM context_fts AS f
                    JOIN context_documents AS d ON d.document_id = f.document_id
                    WHERE context_fts MATCH ?
                      AND f.project_identity = ?
                      AND f.namespace = ?
                    ORDER BY rank, f.path_alias, f.document_id
                    LIMIT ?
                    """,
                    (match, project_identity, namespace, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT f.document_id, f.path_alias, f.namespace, f.content,
                           snippet(context_fts, 4, '', '', ' … ', 40) AS excerpt,
                           bm25(context_fts) AS rank,
                           d.content_sha256, d.complete, d.indexed_at
                    FROM context_fts AS f
                    JOIN context_documents AS d ON d.document_id = f.document_id
                    WHERE context_fts MATCH ?
                      AND f.project_identity = ?
                    ORDER BY rank, f.path_alias, f.document_id
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
                "content": row["content"],
                "excerpt": row["excerpt"],
                "rank": row["rank"],
                "content_sha256": row["content_sha256"],
                "complete": bool(row["complete"]),
                "indexed_at": row["indexed_at"],
            }
            for row in rows
        ]

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
            "circuit_open": bool(row["circuit_open_until"] and row["circuit_open_until"] > now),
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
                    event_type=("lease_outcome_unknown" if ambiguous else "lease_recovered"),
                    occurred_at=now,
                    details={
                        "reason_code": (
                            "external_outcome_unknown" if ambiguous else "retry_safe_local_step"
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
