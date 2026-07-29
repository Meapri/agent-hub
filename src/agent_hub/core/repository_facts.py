"""Bounded repository file facts shared by durable document paths."""

from __future__ import annotations

import os
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


REPOSITORY_SKIP_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
REPOSITORY_SENSITIVE_PARTS = {
    ".aws",
    ".azure",
    ".gnupg",
    ".kube",
    ".ssh",
}
REPOSITORY_SENSITIVE_NAMES = {
    ".credentials.json",
    ".env",
    ".envrc",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "application_default_credentials.json",
    "auth.json",
    "client_secret.json",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "oauth-login-pending.json",
    "oauth-pending.json",
    "oauth-token.json",
    "oauth_client.json",
    "secret.json",
    "secrets.json",
    "service-account.json",
    "token.json",
    "tokens.json",
}
REPOSITORY_ALWAYS_SENSITIVE_STEMS = {
    "credential",
    "credentials",
    "oauth-token",
    "secret",
    "secrets",
}
REPOSITORY_SENSITIVE_CONFIG_STEMS = {
    "auth",
    "oauth-token",
    "token",
    "tokens",
}
REPOSITORY_SENSITIVE_CONFIG_SUFFIXES = {
    ".cfg",
    ".ini",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
}
MAX_REPOSITORY_PATH_BYTES = 16_384


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


_FD_EXEC_SCRIPT = "import os,sys;os.fchdir(int(sys.argv[1]));os.execvp(sys.argv[2], sys.argv[2:])"


def command_in_directory_fd(
    command: list[str],
    directory_fd: int,
) -> tuple[list[str], dict[str, Any]]:
    """Wrap an argv so a helper process executes from an inherited directory FD."""

    return (
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            _FD_EXEC_SCRIPT,
            str(directory_fd),
            *command,
        ],
        {
            "cwd": os.sep,
            "pass_fds": (directory_fd,),
        },
    )


def is_sensitive_repository_path(path: Path) -> bool:
    lowered_parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    normalized_name = name.lstrip(".")
    if name in REPOSITORY_SENSITIVE_NAMES or name.startswith(".env."):
        return True
    if any(
        normalized_name == stem or normalized_name.startswith(f"{stem}.")
        for stem in REPOSITORY_ALWAYS_SENSITIVE_STEMS
    ):
        return True
    if path.suffix.lower() in REPOSITORY_SENSITIVE_CONFIG_SUFFIXES and any(
        normalized_name.startswith(f"{stem}.") for stem in REPOSITORY_SENSITIVE_CONFIG_STEMS
    ):
        return True
    if name.endswith((".p12", ".pfx")):
        return True
    if name.endswith((".key", ".pem")) and "public" not in name:
        return True
    if any(part in REPOSITORY_SENSITIVE_PARTS for part in lowered_parts):
        return True
    return ".config/gcloud" in "/".join(lowered_parts)


def repository_relative_path_reason(path: Path) -> str:
    """Return the canonical rejection reason for a repository-relative path."""

    if path.is_absolute() or not path.parts or any(part in {"", ".."} for part in path.parts):
        return "outside_root"
    lowered = [part.lower() for part in path.parts]
    if any(part in REPOSITORY_SKIP_PARTS or part.endswith(".egg-info") for part in lowered):
        return "unsupported"
    if is_sensitive_repository_path(path):
        return "sensitive"
    return ""


@contextmanager
def repository_root_fd(root: Path) -> Iterator[int]:
    """Anchor repository reads to a no-follow directory descriptor."""

    resolved = root.expanduser()
    if not resolved.is_absolute() or ".." in resolved.parts:
        raise ValueError(f"project_root must be a canonical absolute path: {resolved}")
    flags = _directory_open_flags()
    current_fd = os.open(resolved.anchor, flags)
    try:
        for part in resolved.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        yield current_fd
    finally:
        os.close(current_fd)


def _open_repository_directory_fd(root_fd: int, relative: Path) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in relative.parts:
            if part in {"", "."}:
                continue
            next_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _repository_relative_file(path: Path, root: Path) -> tuple[Optional[Path], str]:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None, "outside_root"
    reason = repository_relative_path_reason(relative)
    if reason:
        return None, reason
    return relative, ""


def read_repository_bytes(
    path: Path,
    root: Path,
    *,
    root_fd: int,
    max_bytes: int,
) -> tuple[bytes, int, str]:
    """Read one regular repository file without following links or hard links."""

    relative, reason = _repository_relative_file(path, root)
    if relative is None:
        return b"", 0, reason
    current_fd = os.dup(root_fd)
    file_fd: Optional[int] = None
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(relative.parts[-1], _file_open_flags(), dir_fd=current_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            return b"", 0, "not_regular"
        if file_stat.st_nlink != 1:
            return b"", 0, "hardlink"
        byte_limit = max(0, int(max_bytes))
        if file_stat.st_size > byte_limit:
            return b"", 0, "oversized"
        chunks: list[bytes] = []
        remaining = byte_limit + 1
        while remaining > 0:
            chunk = os.read(file_fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > byte_limit:
            return b"", 0, "oversized"
        return raw, len(raw), ""
    except OSError:
        return b"", 0, "read_error"
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(current_fd)


def repository_file_size(
    path: Path,
    root: Path,
    *,
    root_fd: int,
    max_bytes: int,
) -> tuple[Optional[int], str]:
    """Return an anchored regular-file size without reading file content."""

    relative, reason = _repository_relative_file(path, root)
    if relative is None:
        return None, reason
    current_fd = os.dup(root_fd)
    file_fd: Optional[int] = None
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(relative.parts[-1], _file_open_flags(), dir_fd=current_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            return None, "not_regular"
        if file_stat.st_size > max(0, int(max_bytes)):
            return None, "oversized"
        return file_stat.st_size, ""
    except OSError:
        return None, "read_error"
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(current_fd)


def _has_symlink_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def safe_repository_file(
    path: Path,
    root: Path,
    *,
    max_bytes: int | None = None,
) -> tuple[bool, str]:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False, "outside_root"
    reason = repository_relative_path_reason(relative)
    if reason:
        return False, reason
    if _has_symlink_component(path, root):
        return False, "symlink"
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        file_stat = path.stat(follow_symlinks=False)
    except (OSError, ValueError):
        return False, "outside_root"
    if not stat.S_ISREG(file_stat.st_mode):
        return False, "not_regular"
    if max_bytes is not None and file_stat.st_size > max_bytes:
        return False, "oversized"
    return True, ""


def _git_root(root: Path, *, root_fd: int | None = None) -> Optional[Path]:
    command = ["git", "rev-parse", "--show-toplevel"]
    popen_kwargs: dict[str, Any] = {"cwd": str(root)}
    if root_fd is not None:
        command, popen_kwargs = command_in_directory_fd(command, root_fd)
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            **popen_kwargs,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()


def _looks_like_git_checkout(root: Path) -> bool:
    current = root
    while True:
        if (current / ".git").exists():
            return True
        if current == current.parent:
            return False
        current = current.parent


def git_repository_files(
    root: Path,
    *,
    max_entries: int,
    max_path_bytes: int,
    root_fd: int | None = None,
    timeout: float = 20.0,
) -> tuple[Optional[list[str]], bool]:
    """Stream Git-visible paths under root and stop at explicit input bounds."""

    git_root = _git_root(root) if root_fd is None else _git_root(root, root_fd=root_fd)
    if git_root is None:
        return None, False
    command = [
        "git",
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        ".",
    ]
    popen_kwargs: dict[str, Any] = {"cwd": str(root)}
    if root_fd is not None:
        command, popen_kwargs = command_in_directory_fd(command, root_fd)
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **popen_kwargs,
        )
    except OSError as exc:
        raise ValueError(
            "Git repository file selection failed; refusing filesystem fallback"
        ) from exc
    if proc.stdout is None:
        proc.kill()
        proc.wait()
        raise ValueError("Git repository file selection failed; refusing filesystem fallback")

    paths: list[str] = []
    buffer = b""
    entry_count = 0
    path_bytes = 0
    truncated = False
    timed_out = False
    selector: selectors.BaseSelector | None = selectors.DefaultSelector()
    try:
        selector.register(proc.stdout, selectors.EVENT_READ)
    except (AttributeError, OSError, ValueError):
        selector.close()
        selector = None
    deadline = time.monotonic() + max(0.1, float(timeout))
    try:
        while True:
            if selector is None:
                chunk = proc.stdout.read(64 * 1024)
            else:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    timed_out = True
                    break
                events = selector.select(timeout=min(0.1, remaining_time))
                if not events:
                    continue
                chunk = os.read(proc.stdout.fileno(), 64 * 1024)
            if not chunk:
                break
            buffer += chunk
            parts = buffer.split(b"\0")
            buffer = parts.pop()
            if (
                len(buffer) > MAX_REPOSITORY_PATH_BYTES
                or path_bytes + len(buffer) + 1 > max_path_bytes
            ):
                truncated = True
                break
            for raw_item in parts:
                if not raw_item:
                    continue
                if len(raw_item) > MAX_REPOSITORY_PATH_BYTES:
                    truncated = True
                    break
                entry_count += 1
                path_bytes += len(raw_item) + 1
                if entry_count > max_entries or path_bytes > max_path_bytes:
                    truncated = True
                    break
                item = raw_item.decode("utf-8", errors="replace")
                relative = Path(item)
                if relative.is_absolute() or ".." in relative.parts:
                    continue
                candidate = root / relative
                if root_fd is None:
                    safe, _reason = safe_repository_file(candidate, root)
                else:
                    size, _reason = repository_file_size(
                        candidate,
                        root,
                        root_fd=root_fd,
                        max_bytes=sys.maxsize,
                    )
                    safe = size is not None
                if safe:
                    paths.append(relative.as_posix())
            if truncated:
                break
    finally:
        if selector is not None:
            selector.close()
        proc.stdout.close()
        if (truncated or timed_out) and proc.poll() is None:
            proc.terminate()
        try:
            return_code = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            return_code = proc.wait()
    if timed_out:
        raise ValueError("Git repository file selection timed out")
    if not truncated and return_code != 0:
        raise ValueError("Git repository file selection failed; refusing filesystem fallback")
    return sorted(set(paths)), truncated


def filesystem_repository_files(
    root: Path,
    *,
    max_entries: int,
    max_path_bytes: int,
    root_fd: int | None = None,
) -> tuple[list[str], bool]:
    """Enumerate non-Git files with bounded streaming directory scans."""

    paths: list[str] = []
    entry_count = 0
    path_bytes = 0
    pending = [Path(".")]
    while pending:
        current_relative = pending.pop()
        current_path = root / current_relative
        current_fd: int | None = None
        entries: list[os.DirEntry[str]] = []
        input_truncated = False
        try:
            if root_fd is not None:
                current_fd = _open_repository_directory_fd(
                    root_fd,
                    current_relative,
                )
            with os.scandir(current_fd if current_fd is not None else current_path) as iterator:
                for entry in iterator:
                    entry_count += 1
                    relative = current_relative / entry.name
                    path_bytes += len(relative.as_posix().encode("utf-8")) + 1
                    if entry_count > max_entries or path_bytes > max_path_bytes:
                        input_truncated = True
                        break
                    entries.append(entry)
        except OSError:
            if current_fd is not None:
                os.close(current_fd)
            continue
        if input_truncated:
            if current_fd is not None:
                os.close(current_fd)
            return sorted(set(paths)), True

        directories: list[Path] = []
        try:
            for entry in sorted(entries, key=lambda item: item.name):
                relative = current_relative / entry.name
                candidate = root / relative
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_directory:
                    dirname = entry.name
                    if (
                        dirname in REPOSITORY_SKIP_PARTS
                        or dirname.endswith(".egg-info")
                        or is_sensitive_repository_path(relative)
                        or entry.is_symlink()
                    ):
                        continue
                    directories.append(relative)
                    continue
                try:
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError:
                    continue
                if not is_file:
                    continue
                if root_fd is None:
                    safe, _reason = safe_repository_file(candidate, root)
                else:
                    size, _reason = repository_file_size(
                        candidate,
                        root,
                        root_fd=root_fd,
                        max_bytes=sys.maxsize,
                    )
                    safe = size is not None
                if safe:
                    paths.append(relative.as_posix())
        finally:
            if current_fd is not None:
                os.close(current_fd)
        pending.extend(reversed(directories))
    return sorted(set(paths)), False


def validate_project_root(project_root: str | Path) -> Path:
    """Resolve an explicit repository root without allowing broad or sensitive roots."""

    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project_root is not a directory: {root}")
    filesystem_root = Path(root.anchor).resolve()
    home = Path.home().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    blocked_roots = {
        filesystem_root,
        home,
        *home.parents,
        temp_root,
        *temp_root.parents,
    }
    for raw_path in (
        "/Applications",
        "/Library",
        "/System",
        "/Volumes",
        "/etc",
        "/home",
        "/opt",
        "/private",
        "/private/tmp",
        "/private/var",
        "/tmp",
        "/usr",
        "/var",
    ):
        candidate = Path(raw_path)
        if candidate.exists():
            blocked_roots.add(candidate.resolve())
    if root in blocked_roots:
        raise ValueError(
            f"project_root is too broad and is blocked; provide a repository directory: {root}"
        )
    if root.name in REPOSITORY_SKIP_PARTS or is_sensitive_repository_path(root):
        raise ValueError(f"project_root points to a sensitive directory: {root}")
    return root
