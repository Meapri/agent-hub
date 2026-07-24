"""File-backed run store so supervised runs survive an MCP process restart.

In-process memory stays the fast path; this mirrors each run to disk so
`orchestrate_get_run` / `orchestrate_continue_recipe` keep working after the
stdio server is relaunched. Zero dependencies — plain JSON files.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import json
import os
import re
import secrets
import stat
import tempfile
from threading import Lock
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

_ENV_DIR = "ORCHESTRATE_CODEX_STATE_DIR"
RUN_ID_PATTERN = r"^[0-9a-f]{12}$"
_RUN_ID_RE = re.compile(RUN_ID_PATTERN)
CLAIM_TOKEN_PATTERN = r"^[0-9a-f]{32}$"
_CLAIM_TOKEN_RE = re.compile(CLAIM_TOKEN_PATTERN)
_LOCAL_LOCKS: Dict[str, Lock] = {}
_LOCAL_LOCKS_GUARD = Lock()
RUN_SUMMARY_SCHEMA = "run_summary_v1"
MAX_SUMMARY_SCAN = 2_000
MAX_SUMMARY_LIMIT = 100
MAX_SUMMARY_STATE_BYTES = 8 * 1024 * 1024


class RunStoreError(RuntimeError):
    """Base class for strict run-store failures."""


class RunAlreadyExists(RunStoreError):
    pass


class RunNotFound(RunStoreError):
    pass


class RunRevisionConflict(RunStoreError):
    def __init__(self, *, expected: int, current: int) -> None:
        self.expected = expected
        self.current = current
        super().__init__(f"run revision conflict: expected {expected}, current {current}")


class RunLeaseActive(RunStoreError):
    def __init__(self, *, current_revision: int, retry_after_seconds: float) -> None:
        self.current_revision = current_revision
        self.retry_after_seconds = max(0.0, retry_after_seconds)
        super().__init__("run already has an active continuation lease")


class RunLeaseLost(RunStoreError):
    pass


class RunPersistenceError(RunStoreError):
    pass


@dataclass(frozen=True)
class RunClaim:
    run_id: str
    token: str
    base_revision: int
    expires_at: float
    state: Dict[str, Any]


def validate_run_id(run_id: Any) -> str:
    """Return a server-shaped run id or reject it before any path access."""

    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("run_id must be exactly 12 lowercase hexadecimal characters")
    return run_id


def validate_claim_token(token: Any) -> str:
    if not isinstance(token, str) or _CLAIM_TOKEN_RE.fullmatch(token) is None:
        raise ValueError("claim_token must be exactly 32 lowercase hexadecimal characters")
    return token


def _trusted_system_alias(path: Path, path_stat: os.stat_result) -> bool:
    try:
        parent_stat = path.parent.stat(follow_symlinks=False)
    except OSError:
        return False
    return bool(
        path_stat.st_uid == 0
        and parent_stat.st_uid == 0
        and not parent_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def _canonical_state_path(base: Path) -> Path:
    expanded = base.expanduser()
    if ".." in expanded.parts:
        raise ValueError(f"run state directory must not contain '..': {expanded}")
    requested = Path(os.path.abspath(expanded))
    current = Path(requested.anchor)
    parts = list(requested.parts[1:])
    for index, part in enumerate(parts):
        candidate = current / part
        try:
            current_stat = candidate.lstat()
        except FileNotFoundError:
            current = candidate.joinpath(*parts[index + 1 :])
            break
        if stat.S_ISLNK(current_stat.st_mode):
            if not _trusted_system_alias(candidate, current_stat):
                raise ValueError(
                    f"run state directory must not contain user-controlled symlinks: {requested}"
                )
            current = candidate.resolve(strict=True)
        else:
            current = candidate
    return current


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_prepared_state_dir(base: Path) -> tuple[Path, int]:
    canonical = _canonical_state_path(base)
    filesystem_root = Path(canonical.anchor).resolve()
    if canonical in {filesystem_root, Path.home().resolve()}:
        raise ValueError(f"run state directory is too broad and is blocked: {canonical}")
    flags = _directory_open_flags()
    directory_fd = os.open(canonical.anchor, flags)
    try:
        for part in canonical.parts[1:]:
            try:
                next_fd = os.open(
                    part,
                    flags,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(
                    part,
                    flags,
                    dir_fd=directory_fd,
                )
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                os.close(next_fd)
                raise ValueError(f"run state directory component is not a directory: {canonical}")
            os.close(directory_fd)
            directory_fd = next_fd
        os.fchmod(directory_fd, 0o700)
        return canonical, directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _open_state_dir() -> tuple[Path, int]:
    override = os.environ.get(_ENV_DIR)
    base = (
        Path(override).expanduser()
        if override
        else Path.home().resolve() / ".orchestrate_codex" / "runs"
    )
    try:
        return _open_prepared_state_dir(base)
    except (OSError, ValueError):
        if override:
            raise
        # Home not writable (sandboxes) — fall back to a temp dir.
        user_id = str(getattr(os, "getuid", lambda: "user")())
        fallback = Path(tempfile.gettempdir()).resolve() / f"orchestrate_codex_runs_{user_id}"
        return _open_prepared_state_dir(fallback)


def _prepare_state_dir(base: Path) -> Path:
    root, fd = _open_prepared_state_dir(base)
    os.close(fd)
    return root


def state_dir() -> Path:
    root, fd = _open_state_dir()
    os.close(fd)
    return root


def _filename(run_id: Any) -> str:
    return f"{validate_run_id(run_id)}.json"


def _path(run_id: str) -> Path:
    root = state_dir()
    candidate = root / _filename(run_id)
    if candidate.parent.resolve() != root:
        raise ValueError("run state path escapes the configured state directory")
    return candidate


def _local_run_lock(run_id: str) -> Lock:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(run_id, Lock())


def _state_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _current_revision(state: Dict[str, Any]) -> int:
    raw = state.get("store_revision", 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise RunPersistenceError("store_revision must be a non-negative integer")
    return raw


def _read_state_from_dir(
    directory_fd: int,
    run_id: str,
    *,
    strict_owner: bool,
) -> Dict[str, Any]:
    filename = _filename(run_id)
    file_fd: Optional[int] = None
    try:
        file_fd = os.open(
            filename,
            _state_open_flags(),
            dir_fd=directory_fd,
        )
    except FileNotFoundError as exc:
        raise RunNotFound(f"run state not found: {run_id}") from exc
    except OSError as exc:
        raise RunPersistenceError(f"could not open run state: {run_id}") from exc
    try:
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise RunPersistenceError(f"run state is not a regular file: {run_id}")
        if file_stat.st_nlink != 1:
            raise RunPersistenceError(f"run state must not be hard-linked: {run_id}")
        current_uid = getattr(os, "getuid", lambda: file_stat.st_uid)()
        if strict_owner and file_stat.st_uid != current_uid:
            raise RunPersistenceError(f"run state has an unexpected owner: {run_id}")
        if file_stat.st_uid == current_uid:
            os.fchmod(file_fd, 0o600)
        with os.fdopen(file_fd, "r", encoding="utf-8") as handle:
            file_fd = None
            parsed = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, RunStoreError):
            raise
        raise RunPersistenceError(f"could not read run state: {run_id}") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
    if not isinstance(parsed, dict) or parsed.get("run_id") != run_id:
        raise RunPersistenceError(f"run state identity mismatch: {run_id}")
    return parsed


def _write_state_to_dir(
    directory_fd: int,
    state: Dict[str, Any],
    *,
    require_absent: bool = False,
) -> Dict[str, Any]:
    run_id = validate_run_id(state.get("run_id"))
    target_name = _filename(run_id)
    try:
        target_stat = os.stat(
            target_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        target_stat = None
    except OSError as exc:
        raise RunPersistenceError(f"could not inspect run state: {run_id}") from exc
    if target_stat is not None:
        if require_absent:
            raise RunAlreadyExists(f"run state already exists: {run_id}")
        if not stat.S_ISREG(target_stat.st_mode):
            raise RunPersistenceError(f"refusing to replace non-regular run state: {run_id}")

    create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        create_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        create_flags |= os.O_NOFOLLOW
    temp_name: Optional[str] = None
    temp_fd: Optional[int] = None
    try:
        for _attempt in range(10):
            temp_name = f".{run_id}.{secrets.token_hex(8)}.tmp"
            try:
                temp_fd = os.open(
                    temp_name,
                    create_flags,
                    0o600,
                    dir_fd=directory_fd,
                )
                break
            except FileExistsError:
                continue
        if temp_fd is None:
            raise RunPersistenceError("could not allocate an exclusive run-state temp file")
        os.fchmod(temp_fd, 0o600)
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            temp_fd = None
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temp_name,
            target_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temp_name = None
        os.fsync(directory_fd)
    except RunStoreError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise RunPersistenceError(f"could not persist run state: {run_id}") from exc
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except OSError:
                pass
    return dict(state)


@contextmanager
def _locked_state_dir(run_id: str) -> Iterator[int]:
    validated = validate_run_id(run_id)
    local_lock = _local_run_lock(validated)
    local_lock.acquire()
    directory_fd: Optional[int] = None
    lock_fd: Optional[int] = None
    try:
        try:
            _root, directory_fd = _open_state_dir()
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            lock_name = f".{validated}.lock"
            lock_fd = os.open(
                lock_name,
                flags,
                0o600,
                dir_fd=directory_fd,
            )
            lock_stat = os.fstat(lock_fd)
            current_uid = getattr(os, "getuid", lambda: lock_stat.st_uid)()
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != current_uid
                or lock_stat.st_nlink != 1
            ):
                raise RunPersistenceError(
                    f"run-state lock is not a trusted regular file: {validated}"
                )
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except RunStoreError:
            raise
        except (OSError, ValueError) as exc:
            raise RunPersistenceError(f"could not prepare run-state lock: {validated}") from exc
        yield directory_fd
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        local_lock.release()


def create(state: Dict[str, Any]) -> Dict[str, Any]:
    """Create a revisioned state without overwriting an existing run."""

    run_id = validate_run_id(state.get("run_id"))
    prepared = dict(state)
    prepared["store_revision"] = _current_revision(prepared)
    prepared.pop("_lease", None)
    with _locked_state_dir(run_id) as directory_fd:
        return _write_state_to_dir(
            directory_fd,
            prepared,
            require_absent=True,
        )


def claim(
    run_id: str,
    *,
    expected_revision: int | None = None,
    lease_seconds: float = 320.0,
) -> RunClaim:
    """Persist a short-lived continuation lease under a brief file lock."""

    validated = validate_run_id(run_id)
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision must be a non-negative integer")
    load_strict(validated)
    with _locked_state_dir(validated) as directory_fd:
        state = _read_state_from_dir(
            directory_fd,
            validated,
            strict_owner=True,
        )
        now = time.time()
        revision = _current_revision(state)
        if expected_revision is not None and revision != expected_revision:
            raise RunRevisionConflict(
                expected=expected_revision,
                current=revision,
            )
        active = state.get("_lease")
        if isinstance(active, dict):
            try:
                expires_at = float(active.get("expires_at"))
            except (TypeError, ValueError) as exc:
                raise RunPersistenceError("run lease expiry is invalid") from exc
            if expires_at > now:
                raise RunLeaseActive(
                    current_revision=revision,
                    retry_after_seconds=expires_at - now,
                )
        token = secrets.token_hex(16)
        expires_at = now + max(1.0, float(lease_seconds))
        leased = {
            **state,
            "store_revision": revision,
            "_lease": {
                "token": token,
                "base_revision": revision,
                "expires_at": expires_at,
            },
        }
        _write_state_to_dir(directory_fd, leased)
    return RunClaim(
        run_id=validated,
        token=token,
        base_revision=revision,
        expires_at=expires_at,
        state=leased,
    )


def commit_claim(claim: RunClaim, state: Dict[str, Any]) -> Dict[str, Any]:
    """Commit a claimed state only if both revision and lease token still match."""

    run_id = validate_run_id(claim.run_id)
    if state.get("run_id") != run_id:
        raise RunPersistenceError("claimed state run_id changed")
    with _locked_state_dir(run_id) as directory_fd:
        current = _read_state_from_dir(
            directory_fd,
            run_id,
            strict_owner=True,
        )
        revision = _current_revision(current)
        active = current.get("_lease")
        if not isinstance(active, dict) or (
            active.get("token") != claim.token or active.get("base_revision") != claim.base_revision
        ):
            raise RunLeaseLost("run continuation lease was replaced or released")
        if revision != claim.base_revision:
            raise RunRevisionConflict(
                expected=claim.base_revision,
                current=revision,
            )
        committed = dict(state)
        committed.pop("_lease", None)
        committed["store_revision"] = revision + 1
        return _write_state_to_dir(directory_fd, committed)


def resume_claim(
    run_id: str,
    *,
    token: str,
    base_revision: int,
) -> RunClaim:
    """Reconstruct a claimed capability without exposing it through normal reads."""

    validated = validate_run_id(run_id)
    validated_token = validate_claim_token(token)
    if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 0:
        raise ValueError("base_revision must be a non-negative integer")
    with _locked_state_dir(validated) as directory_fd:
        current = _read_state_from_dir(
            directory_fd,
            validated,
            strict_owner=True,
        )
        revision = _current_revision(current)
        active = current.get("_lease")
        if (
            not isinstance(active, dict)
            or active.get("token") != validated_token
            or active.get("base_revision") != base_revision
            or revision != base_revision
        ):
            raise RunLeaseLost("run continuation lease was replaced or released")
        try:
            expires_at = float(active.get("expires_at"))
        except (TypeError, ValueError) as exc:
            raise RunPersistenceError("run lease expiry is invalid") from exc
        return RunClaim(
            run_id=validated,
            token=validated_token,
            base_revision=base_revision,
            expires_at=expires_at,
            state=current,
        )


def abort_claim(claim: RunClaim) -> Dict[str, Any]:
    """Release a matching lease without advancing the state revision."""

    run_id = validate_run_id(claim.run_id)
    with _locked_state_dir(run_id) as directory_fd:
        current = _read_state_from_dir(
            directory_fd,
            run_id,
            strict_owner=True,
        )
        revision = _current_revision(current)
        active = current.get("_lease")
        if (
            not isinstance(active, dict)
            or active.get("token") != claim.token
            or active.get("base_revision") != claim.base_revision
            or revision != claim.base_revision
        ):
            raise RunLeaseLost("run continuation lease was replaced or released")
        released = dict(current)
        released.pop("_lease", None)
        return _write_state_to_dir(directory_fd, released)


def load_strict(run_id: str) -> Dict[str, Any]:
    """Load authoritative state or raise a typed persistence error."""

    validated = validate_run_id(run_id)
    directory_fd: Optional[int] = None
    try:
        _root, directory_fd = _open_state_dir()
        return _read_state_from_dir(
            directory_fd,
            validated,
            strict_owner=True,
        )
    except RunStoreError:
        raise
    except (OSError, ValueError) as exc:
        raise RunPersistenceError(f"could not access authoritative run state: {validated}") from exc
    finally:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass


def save(state: Dict[str, Any]) -> None:
    run_id = validate_run_id(state.get("run_id"))
    target_name = _filename(run_id)
    directory_fd: Optional[int] = None
    temp_name: Optional[str] = None
    try:
        _root, directory_fd = _open_state_dir()
        try:
            target_stat = os.stat(
                target_name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            target_stat = None
        if target_stat is not None and stat.S_ISLNK(target_stat.st_mode):
            raise OSError(f"refusing to replace symlinked run state: {target_name}")
        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            create_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            create_flags |= os.O_NOFOLLOW
        temp_fd: Optional[int] = None
        for _attempt in range(10):
            temp_name = f".{run_id}.{secrets.token_hex(8)}.tmp"
            try:
                temp_fd = os.open(
                    temp_name,
                    create_flags,
                    0o600,
                    dir_fd=directory_fd,
                )
                break
            except FileExistsError:
                continue
        if temp_fd is None:
            raise OSError("could not allocate an exclusive run-state temp file")
        os.fchmod(temp_fd, 0o600)
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temp_name,
            target_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temp_name = None
    except (OSError, TypeError):
        pass  # persistence is best-effort; memory store still holds the run
    finally:
        if temp_name is not None and directory_fd is not None:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except OSError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)


def load(run_id: str) -> Optional[Dict[str, Any]]:
    validated = validate_run_id(run_id)
    filename = _filename(validated)
    flags = _state_open_flags()
    directory_fd: Optional[int] = None
    file_fd: Optional[int] = None
    try:
        _root, directory_fd = _open_state_dir()
        file_fd = os.open(filename, flags, dir_fd=directory_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            return None
        if file_stat.st_uid == getattr(os, "getuid", lambda: file_stat.st_uid)():
            os.fchmod(file_fd, 0o600)
        with os.fdopen(file_fd, "r", encoding="utf-8") as handle:
            file_fd = None
            parsed = json.load(handle)
        if not isinstance(parsed, dict) or parsed.get("run_id") != validated:
            return None
        return parsed
    except (OSError, ValueError, TypeError):
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def list_run_ids() -> List[str]:
    directory_fd: Optional[int] = None
    try:
        _root, directory_fd = _open_state_dir()
        run_ids: List[str] = []
        for name in os.listdir(directory_fd):
            if not name.endswith(".json"):
                continue
            try:
                file_stat = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                run_ids.append(validate_run_id(name.removesuffix(".json")))
            except (OSError, ValueError):
                continue
        return sorted(run_ids)
    except (OSError, ValueError):
        return []
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _summary_project_root(state: Dict[str, Any]) -> str:
    raw = state.get("project_root")
    if not raw and isinstance(state.get("options"), dict):
        raw = state["options"].get("project_root")
    value = str(raw or "").strip()
    if not value or not Path(value).is_absolute():
        return ""
    return os.path.realpath(value)


def _summary_current_stage(state: Dict[str, Any]) -> str | None:
    if state.get("run_kind") == "adaptive":
        plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
        completed = set(
            (state.get("results") or {}).keys()
            if isinstance(state.get("results"), dict)
            else ()
        )
        for step in plan.get("steps") or []:
            if isinstance(step, dict) and str(step.get("id") or "") not in completed:
                return str(step.get("id") or "") or None
        return None
    steps = state.get("steps") if isinstance(state.get("steps"), list) else []
    try:
        cursor = int(state.get("cursor") or 0)
    except (TypeError, ValueError):
        return None
    if 0 <= cursor < len(steps) and isinstance(steps[cursor], dict):
        return str(steps[cursor].get("id") or "") or None
    return None


def _summary_next_action_type(state: Dict[str, Any]) -> str:
    status = str(state.get("status") or "")
    if status == "completed":
        return "done"
    if status in {"failed", "cancelled", "archived"}:
        return "failed"
    if state.get("run_kind") == "adaptive":
        return "continue"
    steps = state.get("steps") if isinstance(state.get("steps"), list) else []
    try:
        cursor = int(state.get("cursor") or 0)
    except (TypeError, ValueError):
        return "continue"
    if 0 <= cursor < len(steps) and isinstance(steps[cursor], dict):
        return "call_tool" if steps[cursor].get("tool") else "local"
    return "continue"


def _run_summary(state: Dict[str, Any], *, now: float) -> Dict[str, Any]:
    lease = state.get("_lease") if isinstance(state.get("_lease"), dict) else {}
    try:
        lease_expires_at = float(lease.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        lease_expires_at = 0.0
    snapshot = (
        state.get("_handoff_snapshot")
        if isinstance(state.get("_handoff_snapshot"), dict)
        else {}
    )
    run_kind = str(
        state.get("run_kind") or ("fixed" if state.get("recipe_id") else "")
    )
    return {
        "schema": RUN_SUMMARY_SCHEMA,
        "run_id": validate_run_id(state.get("run_id")),
        "run_kind": run_kind,
        "workflow_id": str(state.get("workflow_id") or state.get("recipe_id") or ""),
        "project_root": _summary_project_root(state),
        "status": str(state.get("status") or ""),
        "store_revision": _current_revision(state),
        "created_at": float(state.get("created_at") or 0.0),
        "updated_at": float(
            state.get("updated_at") or state.get("created_at") or 0.0
        ),
        "current_stage": _summary_current_stage(state),
        "next_action_type": _summary_next_action_type(state),
        "lease_active": lease_expires_at > now,
        "handoff_sha256": snapshot.get("file_sha256"),
    }


def _project_cursor_digest(project_root: str) -> str:
    return sha256(project_root.encode("utf-8")).hexdigest()


def _encode_summary_cursor(project_root: str, summary: Dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "project": _project_cursor_digest(project_root),
            "updated_at": summary["updated_at"],
            "run_id": summary["run_id"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_summary_cursor(
    cursor: str | None,
    *,
    project_root: str,
) -> tuple[float, str] | None:
    if not cursor:
        return None
    value = str(cursor)
    if len(value) > 512:
        raise ValueError("cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        updated_at = float(parsed["updated_at"])
        run_id = validate_run_id(parsed["run_id"])
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("cursor is invalid") from exc
    if (
        not isinstance(parsed, dict)
        or parsed.get("v") != 1
        or parsed.get("project") != _project_cursor_digest(project_root)
    ):
        raise ValueError("cursor does not belong to this project")
    return updated_at, run_id


def list_run_summaries(
    project_root: str,
    *,
    run_kind: str | None = None,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    max_scan: int = MAX_SUMMARY_SCAN,
) -> Dict[str, Any]:
    """Return a bounded, exact-project projection without prompts or result bodies."""

    requested_root = Path(str(project_root)).expanduser()
    if not requested_root.is_absolute():
        raise ValueError("project_root must be an absolute canonical path")
    root = str(requested_root.resolve())
    if str(requested_root) != root:
        raise ValueError("project_root must be an absolute canonical path")
    page_limit = int(limit)
    scan_limit = int(max_scan)
    if not 1 <= page_limit <= MAX_SUMMARY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_SUMMARY_LIMIT}")
    if not 1 <= scan_limit <= 10_000:
        raise ValueError("max_scan must be between 1 and 10000")
    kind_filter = str(run_kind or "").strip().lower()
    if kind_filter and kind_filter not in {"fixed", "adaptive"}:
        raise ValueError("run_kind must be fixed or adaptive")
    status_filter = str(status or "").strip().lower()
    after = _decode_summary_cursor(cursor, project_root=root)

    directory_fd: Optional[int] = None
    names: List[str] = []
    truncated = False
    skipped = {"corrupt": 0, "oversize": 0, "unscoped": 0}
    try:
        _root, directory_fd = _open_state_dir()
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                name = entry.name
                if not name.endswith(".json"):
                    continue
                if len(names) >= scan_limit:
                    truncated = True
                    break
                try:
                    validate_run_id(name.removesuffix(".json"))
                    file_stat = entry.stat(follow_symlinks=False)
                except (OSError, ValueError):
                    skipped["corrupt"] += 1
                    continue
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_nlink != 1
                ):
                    skipped["corrupt"] += 1
                    continue
                if file_stat.st_size > MAX_SUMMARY_STATE_BYTES:
                    skipped["oversize"] += 1
                    continue
                names.append(name)

        summaries: List[Dict[str, Any]] = []
        now = time.time()
        for name in sorted(names):
            run_id = name.removesuffix(".json")
            try:
                state = _read_state_from_dir(
                    directory_fd,
                    run_id,
                    strict_owner=True,
                )
                summary = _run_summary(state, now=now)
            except (RunStoreError, TypeError, ValueError):
                skipped["corrupt"] += 1
                continue
            if not summary["project_root"]:
                skipped["unscoped"] += 1
                continue
            if summary["project_root"] != root:
                continue
            if kind_filter and summary["run_kind"] != kind_filter:
                continue
            if status_filter and summary["status"] != status_filter:
                continue
            summaries.append(summary)
    finally:
        if directory_fd is not None:
            os.close(directory_fd)

    summaries.sort(
        key=lambda item: (float(item["updated_at"]), str(item["run_id"])),
        reverse=True,
    )
    if after is not None:
        summaries = [
            item
            for item in summaries
            if (float(item["updated_at"]), str(item["run_id"])) < after
        ]
    has_more = len(summaries) > page_limit
    page = summaries[:page_limit]
    next_cursor = (
        _encode_summary_cursor(root, page[-1])
        if page and has_more
        else None
    )
    return {
        "schema": "run_summary_list_v1",
        "project_root": root,
        "runs": page,
        "next_cursor": next_cursor,
        "truncated": truncated,
        "scanned": len(names),
        "skipped": skipped,
    }


def delete(run_id: str) -> None:
    filename = _filename(run_id)
    directory_fd: Optional[int] = None
    try:
        _root, directory_fd = _open_state_dir()
        try:
            file_stat = os.stat(
                filename,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if stat.S_ISLNK(file_stat.st_mode):
            return
        os.unlink(filename, dir_fd=directory_fd)
    except OSError:
        pass
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
