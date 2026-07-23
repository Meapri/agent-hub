"""File-backed run store so supervised runs survive an MCP process restart.

In-process memory stays the fast path; this mirrors each run to disk so
`orchestrate_get_run` / `orchestrate_continue_recipe` keep working after the
stdio server is relaunched. Zero dependencies — plain JSON files.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENV_DIR = "ORCHESTRATE_CODEX_STATE_DIR"
RUN_ID_PATTERN = r"^[0-9a-f]{12}$"
_RUN_ID_RE = re.compile(RUN_ID_PATTERN)


def validate_run_id(run_id: Any) -> str:
    """Return a server-shaped run id or reject it before any path access."""

    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("run_id must be exactly 12 lowercase hexadecimal characters")
    return run_id


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
                raise ValueError(
                    f"run state directory component is not a directory: {canonical}"
                )
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
        fallback = (
            Path(tempfile.gettempdir()).resolve()
            / f"orchestrate_codex_runs_{user_id}"
        )
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
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_fd: Optional[int] = None
    file_fd: Optional[int] = None
    try:
        _root, directory_fd = _open_state_dir()
        file_fd = os.open(filename, flags, dir_fd=directory_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
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
