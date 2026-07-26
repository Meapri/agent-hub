"""Atomic LaunchAgent executable switching with a local rollback slot."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import stat
import subprocess
import tempfile
import time
from typing import Any, Mapping
from urllib.parse import quote

from .contracts import canonical_json
from .daemon import HubDaemonClient
from .errors import HubV2Error
from .setup import LAUNCH_AGENT_LABEL, LAUNCH_AGENT_NAME

ROLLBACK_DIR = Path("~/.agent-hub/rollback").expanduser()
ROLLBACK_PLIST = ROLLBACK_DIR / LAUNCH_AGENT_NAME


def _safe_file(path: Path, *, executable: bool = False) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise HubV2Error(
            "release_target_unavailable",
            "The release target does not exist.",
            scope="release",
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise HubV2Error(
            "unsafe_release_target",
            "The release target must be a safe regular file.",
            scope="release",
        )
    if executable and not os.access(path, os.X_OK):
        raise HubV2Error(
            "release_target_not_executable",
            "The candidate daemon is not executable.",
            scope="release",
        )
    return path.read_bytes()


def _launch_path(value: str | Path | None) -> Path:
    return (
        Path(value).expanduser()
        if value
        else Path("~/Library/LaunchAgents").expanduser() / LAUNCH_AGENT_NAME
    )


def _argument_value(arguments: list[str], flag: str) -> str | None:
    if flag not in arguments:
        return None
    index = arguments.index(flag)
    if index + 1 >= len(arguments):
        raise HubV2Error(
            "invalid_launch_agent",
            "The LaunchAgent has an incomplete daemon argument.",
            scope="release",
        )
    return str(arguments[index + 1])


def _state_db_path(payload: Mapping[str, Any]) -> Path:
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list):
        raise HubV2Error(
            "invalid_launch_agent",
            "The LaunchAgent has no daemon command.",
            scope="release",
        )
    value = _argument_value([str(item) for item in arguments], "--state-db")
    return Path(value or "~/.agent-hub/state.sqlite3").expanduser()


def _database_schema(path: Path) -> int | None:
    if not path.exists():
        return None
    _safe_file(path)
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    except sqlite3.Error as exc:
        raise HubV2Error(
            "release_state_invalid",
            "The installed Agent Hub database is unreadable.",
            scope="release",
        ) from exc
    finally:
        if connection is not None:
            connection.close()
    if integrity != "ok" or row is None:
        raise HubV2Error(
            "release_state_invalid",
            "The installed Agent Hub database failed its integrity check.",
            scope="release",
        )
    return int(row[0])


def _database_snapshot(source: Path, destination: Path) -> dict[str, Any]:
    schema_version = _database_schema(source)
    if schema_version is None:
        return {
            "present": False,
            "schema_version": None,
            "path": str(destination),
            "sha256": None,
        }
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=".state-snapshot.",
        dir=destination.parent,
    )
    os.close(descriptor)
    temp = Path(temp_name)
    temp.unlink()
    source_uri = f"file:{quote(str(source.resolve()))}?mode=ro"
    try:
        source_connection = sqlite3.connect(source_uri, uri=True)
        output = sqlite3.connect(temp)
        try:
            source_connection.backup(output)
            integrity = str(output.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            output.close()
            source_connection.close()
        if integrity != "ok":
            raise HubV2Error(
                "release_state_invalid",
                "The Agent Hub database snapshot failed its integrity check.",
                scope="release",
            )
        os.chmod(temp, 0o600)
        os.replace(temp, destination)
    finally:
        if temp.exists():
            temp.unlink()
    return {
        "present": True,
        "schema_version": schema_version,
        "path": str(destination),
        "sha256": sha256(destination.read_bytes()).hexdigest(),
    }


def _restore_database_snapshot(source: Path, destination: Path) -> dict[str, Any]:
    temporary = destination.with_name(f".{destination.name}.restore-{os.getpid()}-{time.time_ns()}")
    snapshot = _database_snapshot(source, temporary)
    if not snapshot["present"]:
        raise HubV2Error(
            "rollback_state_unavailable",
            "The database rollback snapshot is unavailable.",
            scope="release",
        )
    try:
        for path in (
            destination,
            Path(f"{destination}-wal"),
            Path(f"{destination}-shm"),
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        **snapshot,
        "path": str(destination),
        "sha256": sha256(destination.read_bytes()).hexdigest(),
    }


def _rollback_paths(rollback_path: str | Path) -> tuple[Path, Path, Path]:
    rollback = Path(rollback_path).expanduser()
    metadata = rollback.with_name(rollback.name + ".metadata.json")
    database = rollback.with_name(rollback.name + ".state.sqlite3")
    return rollback, metadata, database


def _file_set_snapshot(paths: tuple[Path, ...]) -> dict[Path, bytes | None]:
    return {path: (_safe_file(path) if path.exists() else None) for path in paths}


def _restore_file_set(snapshot: Mapping[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        _atomic_write(path, content)
        if path.name.endswith(".sqlite3"):
            os.chmod(path, 0o600)


def _proposal_digest(proposal: Mapping[str, Any]) -> str:
    public = {
        key: value
        for key, value in proposal.items()
        if key != "proposal_sha256" and not str(key).startswith("_")
    }
    return sha256(canonical_json(public).encode("utf-8")).hexdigest()


def _proposal(
    *,
    mode: str,
    launch_path: Path,
    before: bytes,
    after: bytes,
    previous_executable: str,
    proposed_executable: str,
    proposed_executable_sha256: str,
    state_db_path: Path,
    state_db_schema: int | None,
    rollback_path: Path,
    rollback_db_path: Path,
    rollback_db_schema: int | None = None,
    rollback_db_sha256: str | None = None,
    database_restore_required: bool = False,
) -> dict[str, Any]:
    proposal = {
        "schema": "agent_hub_release_switch_plan_v1",
        "mode": mode,
        "launch_agent_path": str(launch_path),
        "before_sha256": sha256(before).hexdigest(),
        "after_sha256": sha256(after).hexdigest(),
        "previous_executable": previous_executable,
        "proposed_executable": proposed_executable,
        "proposed_executable_sha256": proposed_executable_sha256,
        "health_check": "temporary_socket_ping_with_state_copy",
        "rollback_slot": str(rollback_path),
        "state_db_path": str(state_db_path),
        "state_db_schema": state_db_schema,
        "rollback_db_path": str(rollback_db_path),
        "rollback_db_schema": rollback_db_schema,
        "rollback_db_sha256": rollback_db_sha256,
        "database_restore_required": database_restore_required,
    }
    proposal["proposal_sha256"] = _proposal_digest(proposal)
    proposal["_before"] = before
    proposal["_after"] = after
    return proposal


def plan_update(
    candidate_root: str | Path,
    *,
    launch_agent_path: str | Path | None = None,
    rollback_path: str | Path = ROLLBACK_PLIST,
) -> dict[str, Any]:
    launch_path = _launch_path(launch_agent_path)
    before = _safe_file(launch_path)
    try:
        current = plistlib.loads(before)
    except plistlib.InvalidFileException as exc:
        raise HubV2Error(
            "invalid_launch_agent",
            "The installed LaunchAgent is invalid.",
            scope="release",
        ) from exc
    arguments = current.get("ProgramArguments")
    if not isinstance(arguments, list) or not arguments:
        raise HubV2Error(
            "invalid_launch_agent",
            "The installed LaunchAgent has no daemon command.",
            scope="release",
        )
    candidate = Path(candidate_root).expanduser().resolve(strict=True) / "bin/agent-hubd"
    candidate_content = _safe_file(candidate, executable=True)
    previous = str(arguments[0])
    state_db = _state_db_path(current)
    state_schema = _database_schema(state_db)
    rollback, _, rollback_db = _rollback_paths(rollback_path)
    updated = dict(current)
    updated["ProgramArguments"] = [str(candidate), *arguments[1:]]
    after = plistlib.dumps(updated, fmt=plistlib.FMT_XML, sort_keys=True)
    return _proposal(
        mode="update",
        launch_path=launch_path,
        before=before,
        after=after,
        previous_executable=previous,
        proposed_executable=str(candidate),
        proposed_executable_sha256=sha256(candidate_content).hexdigest(),
        state_db_path=state_db,
        state_db_schema=state_schema,
        rollback_path=rollback,
        rollback_db_path=rollback_db,
    )


def plan_rollback(
    *,
    launch_agent_path: str | Path | None = None,
    rollback_path: str | Path = ROLLBACK_PLIST,
) -> dict[str, Any]:
    launch_path = _launch_path(launch_agent_path)
    before = _safe_file(launch_path)
    after = _safe_file(Path(rollback_path).expanduser())
    try:
        current = plistlib.loads(before)
        rollback = plistlib.loads(after)
        current_args = current["ProgramArguments"]
        rollback_args = rollback["ProgramArguments"]
    except (plistlib.InvalidFileException, KeyError, TypeError) as exc:
        raise HubV2Error(
            "invalid_launch_agent",
            "The current or rollback LaunchAgent is invalid.",
            scope="release",
        ) from exc
    rollback_executable = _safe_file(Path(str(rollback_args[0])), executable=True)
    current_state_db = _state_db_path(current)
    rollback_state_db = _state_db_path(rollback)
    if current_state_db != rollback_state_db:
        raise HubV2Error(
            "rollback_state_conflict",
            "The rollback slot targets a different Agent Hub database.",
            scope="release",
        )
    rollback_path, metadata_path, rollback_db_path = _rollback_paths(rollback_path)
    try:
        metadata = json.loads(_safe_file(metadata_path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HubV2Error(
            "rollback_state_unavailable",
            "The rollback database metadata is invalid.",
            scope="release",
        ) from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("state_db_path") != str(current_state_db)
        or metadata.get("rollback_db_path") != str(rollback_db_path)
    ):
        raise HubV2Error(
            "rollback_state_unavailable",
            "The rollback database metadata does not match the installed state.",
            scope="release",
        )
    rollback_schema = metadata.get("schema_version")
    if rollback_schema is not None:
        rollback_schema = int(rollback_schema)
        snapshot = _safe_file(rollback_db_path)
        if _database_schema(rollback_db_path) != rollback_schema or sha256(
            snapshot
        ).hexdigest() != metadata.get("sha256"):
            raise HubV2Error(
                "rollback_state_unavailable",
                "The rollback database snapshot is unavailable or incompatible.",
                scope="release",
            )
    current_schema = _database_schema(current_state_db)
    restore_required = rollback_schema is not None and (
        current_schema is None or current_schema > rollback_schema
    )
    return _proposal(
        mode="rollback",
        launch_path=launch_path,
        before=before,
        after=after,
        previous_executable=str(current_args[0]),
        proposed_executable=str(rollback_args[0]),
        proposed_executable_sha256=sha256(rollback_executable).hexdigest(),
        state_db_path=current_state_db,
        state_db_schema=current_schema,
        rollback_path=rollback_path,
        rollback_db_path=rollback_db_path,
        rollback_db_schema=rollback_schema,
        rollback_db_sha256=(str(metadata.get("sha256")) if rollback_schema is not None else None),
        database_restore_required=restore_required,
    )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".release.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _replace_argument(arguments: list[str], flag: str, value: str) -> list[str]:
    updated = list(arguments)
    if flag in updated:
        index = updated.index(flag)
        if index + 1 >= len(updated):
            raise HubV2Error(
                "invalid_launch_agent",
                "The LaunchAgent has an incomplete daemon argument.",
                scope="release",
            )
        updated[index + 1] = value
    else:
        updated.extend([flag, value])
    return updated


def _candidate_health(
    after: bytes,
    *,
    source_state_db: Path,
) -> dict[str, Any]:
    try:
        payload = plistlib.loads(after)
        arguments = [str(item) for item in payload["ProgramArguments"]]
    except (plistlib.InvalidFileException, KeyError, TypeError) as exc:
        raise HubV2Error(
            "invalid_launch_agent",
            "The candidate LaunchAgent is invalid.",
            scope="release",
        ) from exc
    with tempfile.TemporaryDirectory(prefix="ah-health-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        socket_path = root / "s"
        state_db = root / "d.sqlite3"
        source_schema = _database_schema(source_state_db)
        if source_schema is not None:
            _database_snapshot(source_state_db, state_db)
        arguments = _replace_argument(arguments, "--socket", str(socket_path))
        arguments = _replace_argument(arguments, "--state-db", str(state_db))
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                if socket_path.exists():
                    try:
                        response = HubDaemonClient(socket_path).request(
                            "ping",
                            timeout=1.0,
                        )
                    except HubV2Error:
                        pass
                    else:
                        if response.get("success") is True:
                            return {
                                "schema": "agent_hub_candidate_health_v1",
                                "success": True,
                                "check": "temporary_socket_ping_with_state_copy",
                                "source_schema_version": source_schema,
                                "candidate_schema_version": _database_schema(state_db),
                            }
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
        raise HubV2Error(
            "candidate_health_check_failed",
            "The candidate daemon failed its isolated socket health check.",
            scope="release",
        )


def _installed_socket(after: bytes) -> Path:
    payload = plistlib.loads(after)
    arguments = [str(item) for item in payload["ProgramArguments"]]
    if "--socket" not in arguments:
        return Path("~/.agent-hub/run/agent-hub.sock").expanduser()
    index = arguments.index("--socket")
    if index + 1 >= len(arguments):
        raise HubV2Error(
            "invalid_launch_agent",
            "The LaunchAgent has an incomplete socket argument.",
            scope="release",
        )
    return Path(arguments[index + 1]).expanduser()


def _wait_installed_health(after: bytes) -> bool:
    socket_path = _installed_socket(after)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            response = HubDaemonClient(socket_path).request("ping", timeout=1.0)
        except HubV2Error:
            time.sleep(0.1)
            continue
        return response.get("success") is True
    return False


def _wait_installed_stopped(before: bytes, *, timeout: float = 5.0) -> bool:
    socket_path = _installed_socket(before)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = HubDaemonClient(socket_path).request("ping", timeout=0.5)
        except HubV2Error:
            return True
        if response.get("success") is not True:
            return True
        time.sleep(0.1)
    return False


def _remove_database_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _database_files_exist(path: Path) -> bool:
    return any(candidate.exists() for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")))


def _emergency_database_path(rollback_path: Path) -> Path:
    return rollback_path.with_name(rollback_path.name + ".emergency.state.sqlite3")


def _stop_launch_agent(payload: bytes) -> tuple[bool, int | None]:
    returncode: int | None = None
    try:
        result = subprocess.run(
            [
                "/bin/launchctl",
                "bootout",
                f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        returncode = result.returncode
    except (OSError, subprocess.SubprocessError):
        pass
    if returncode != 0:
        try:
            loaded = subprocess.run(
                [
                    "/bin/launchctl",
                    "print",
                    f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15.0,
            )
        except (OSError, subprocess.SubprocessError):
            return False, returncode
        if loaded.returncode == 0:
            return False, returncode
    try:
        return _wait_installed_stopped(payload), returncode
    except Exception:  # noqa: BLE001 - this is a release recovery boundary
        return False, returncode


def _start_launch_agent(launch_path: Path, payload: bytes) -> tuple[bool, int | None]:
    try:
        result = subprocess.run(
            [
                "/bin/launchctl",
                "bootstrap",
                f"gui/{os.getuid()}",
                str(launch_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False, None
    if result.returncode != 0:
        return False, result.returncode
    try:
        return _wait_installed_health(payload), result.returncode
    except Exception:  # noqa: BLE001 - this is a release recovery boundary
        return False, result.returncode


def _recover_previous_install(
    *,
    launch_path: Path,
    previous_payload: bytes,
    candidate_payload: bytes,
    state_db: Path,
    emergency_db: Path,
    emergency_snapshot: Mapping[str, Any],
    candidate_may_be_running: bool,
) -> tuple[bool, str]:
    if candidate_may_be_running:
        try:
            stopped, _ = _stop_launch_agent(candidate_payload)
        except Exception:  # noqa: BLE001 - report the recovery stage, not raw errors
            stopped = False
        if not stopped:
            try:
                _atomic_write(launch_path, previous_payload)
            except OSError:
                pass
            return False, "candidate_stop"
    try:
        _atomic_write(launch_path, previous_payload)
    except OSError:
        return False, "launch_agent_restore"
    try:
        if emergency_snapshot["present"]:
            _restore_database_snapshot(emergency_db, state_db)
        else:
            _remove_database_files(state_db)
    except Exception:  # noqa: BLE001 - preserve a safe recovery-stage contract
        return False, "database_restore"
    active, _ = _start_launch_agent(launch_path, previous_payload)
    if not active:
        return False, "daemon_restart"
    return True, "complete"


def apply_switch(
    proposal: Mapping[str, Any],
    *,
    proposal_sha256: str,
    activate: bool = True,
    rollback_path: str | Path = ROLLBACK_PLIST,
) -> dict[str, Any]:
    if (
        proposal.get("proposal_sha256") != proposal_sha256
        or _proposal_digest(proposal) != proposal_sha256
    ):
        raise HubV2Error(
            "proposal_digest_conflict",
            "The release proposal digest does not match.",
            scope="release",
        )
    before = proposal.get("_before")
    after = proposal.get("_after")
    if not isinstance(before, bytes) or not isinstance(after, bytes):
        raise HubV2Error(
            "invalid_release_plan",
            "The release plan is not an in-process reviewed plan.",
            scope="release",
        )
    launch_path = Path(str(proposal.get("launch_agent_path") or "")).expanduser()
    current = _safe_file(launch_path)
    if sha256(current).hexdigest() != proposal.get("before_sha256"):
        raise HubV2Error(
            "release_target_conflict",
            "The LaunchAgent changed after planning.",
            scope="release",
            retryable=True,
        )
    rollback, rollback_metadata, rollback_database = _rollback_paths(rollback_path)
    if str(rollback) != str(proposal.get("rollback_slot") or ""):
        raise HubV2Error(
            "rollback_slot_conflict",
            "The selected rollback slot differs from the reviewed release plan.",
            scope="release",
        )
    try:
        current_payload = plistlib.loads(current)
    except plistlib.InvalidFileException as exc:
        raise HubV2Error(
            "invalid_launch_agent",
            "The installed LaunchAgent is invalid.",
            scope="release",
        ) from exc
    state_db = _state_db_path(current_payload)
    if str(state_db) != str(proposal.get("state_db_path") or ""):
        raise HubV2Error(
            "release_state_conflict",
            "The LaunchAgent database path changed after planning.",
            scope="release",
            retryable=True,
        )
    current_schema = _database_schema(state_db)
    if current_schema != proposal.get("state_db_schema"):
        raise HubV2Error(
            "release_state_conflict",
            "The Agent Hub database schema changed after planning.",
            scope="release",
            retryable=True,
        )
    proposed_executable = Path(str(proposal.get("proposed_executable") or ""))
    proposed_content = _safe_file(proposed_executable, executable=True)
    if sha256(proposed_content).hexdigest() != proposal.get("proposed_executable_sha256"):
        raise HubV2Error(
            "release_candidate_conflict",
            "The candidate daemon changed after release planning.",
            scope="release",
            retryable=True,
        )
    restore_required = bool(proposal.get("database_restore_required"))
    if proposal.get("mode") == "rollback" and proposal.get("rollback_db_sha256"):
        rollback_content = _safe_file(rollback_database)
        if sha256(rollback_content).hexdigest() != proposal.get("rollback_db_sha256"):
            raise HubV2Error(
                "release_state_conflict",
                "The rollback database changed after planning.",
                scope="release",
                retryable=True,
            )
    if proposal.get("mode") == "rollback" and restore_required and not activate:
        raise HubV2Error(
            "rollback_requires_activation",
            "A schema rollback must stop the daemon before restoring its database.",
            scope="release",
        )
    emergency_db = _emergency_database_path(rollback)
    if _database_files_exist(emergency_db):
        raise HubV2Error(
            "release_recovery_pending",
            "A preserved emergency database must be reviewed before another release switch.",
            scope="release",
            next_action={
                "type": "local_cli",
                "command": "agent-hub doctor",
            },
        )
    health_source = (
        rollback_database
        if proposal.get("mode") == "rollback" and proposal.get("rollback_db_schema") is not None
        else state_db
    )
    health = _candidate_health(after, source_state_db=health_source)
    rollback_snapshot: dict[str, Any] | None = None
    previous_rollback_slot = (
        _file_set_snapshot((rollback, rollback_metadata, rollback_database))
        if proposal.get("mode") == "update"
        else None
    )
    if proposal.get("mode") == "update":
        try:
            rollback_snapshot = _database_snapshot(state_db, rollback_database)
            metadata = {
                "schema": "agent_hub_release_rollback_metadata_v1",
                "state_db_path": str(state_db),
                "rollback_db_path": str(rollback_database),
                "present": rollback_snapshot["present"],
                "schema_version": rollback_snapshot["schema_version"],
                "sha256": rollback_snapshot["sha256"],
                "created_at": time.time(),
            }
            _atomic_write(
                rollback_metadata,
                json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
            _atomic_write(rollback, before)
        except Exception:
            if previous_rollback_slot is not None:
                _restore_file_set(previous_rollback_slot)
            raise
    activation = {"attempted": False, "success": False}
    emergency_snapshot = _database_snapshot(state_db, emergency_db)
    preserve_emergency = False
    try:
        _atomic_write(launch_path, after)
        if activate:
            stopped, stop_returncode = _stop_launch_agent(before)
            if not stopped:
                _atomic_write(launch_path, before)
                raise HubV2Error(
                    "release_daemon_stop_failed",
                    "The running daemon could not be stopped; no database restore was attempted.",
                    scope="release",
                    retryable=True,
                    safe_details={"returncode": stop_returncode},
                )
            failure_stage: str | None = None
            candidate_may_be_running = False
            activation_returncode: int | None = None
            try:
                if proposal.get("mode") == "rollback" and restore_required:
                    _restore_database_snapshot(rollback_database, state_db)
                candidate_may_be_running = True
                active, activation_returncode = _start_launch_agent(launch_path, after)
                if not active:
                    failure_stage = (
                        "candidate_bootstrap" if activation_returncode != 0 else "candidate_health"
                    )
            except Exception:  # noqa: BLE001 - all activation failures must compensate
                active = False
                failure_stage = (
                    "candidate_bootstrap" if candidate_may_be_running else "database_restore"
                )
            activation = {
                "attempted": True,
                "success": active,
                "returncode": activation_returncode,
            }
            if not active:
                recovered, recovery_stage = _recover_previous_install(
                    launch_path=launch_path,
                    previous_payload=before,
                    candidate_payload=after,
                    state_db=state_db,
                    emergency_db=emergency_db,
                    emergency_snapshot=emergency_snapshot,
                    candidate_may_be_running=candidate_may_be_running,
                )
                if not recovered:
                    preserve_emergency = bool(emergency_snapshot["present"])
                    raise HubV2Error(
                        "release_recovery_failed",
                        "The release failed and the previous installation could not be fully restarted.",
                        scope="release",
                        retryable=False,
                        safe_details={
                            "failure_stage": failure_stage,
                            "recovery_stage": recovery_stage,
                            "emergency_snapshot_preserved": preserve_emergency,
                        },
                        next_action={
                            "type": "local_cli",
                            "command": "agent-hub doctor",
                        },
                    )
                raise HubV2Error(
                    "release_activation_failed",
                    "The release failed; the previous executable, database, and daemon were restored.",
                    scope="release",
                    retryable=True,
                    safe_details={
                        "failure_stage": failure_stage,
                        "recovery_stage": recovery_stage,
                    },
                )
    except Exception:
        if previous_rollback_slot is not None:
            _restore_file_set(previous_rollback_slot)
        raise
    finally:
        if not preserve_emergency:
            _remove_database_files(emergency_db)
    rollback_available = rollback.exists() and rollback_metadata.exists()
    if rollback_snapshot and rollback_snapshot["present"]:
        rollback_available = rollback_available and rollback_database.exists()
    return {
        "schema": "agent_hub_release_switch_result_v1",
        "success": True,
        "mode": proposal["mode"],
        "previous_executable": proposal["previous_executable"],
        "active_executable": proposal["proposed_executable"],
        "launch_agent_sha256": sha256(after).hexdigest(),
        "activation": activation,
        "candidate_health": health,
        "database": {
            "path": str(state_db),
            "schema_before": current_schema,
            "restore_applied": bool(
                proposal.get("mode") == "rollback" and restore_required and activate
            ),
        },
        "rollback_available": rollback_available,
    }
