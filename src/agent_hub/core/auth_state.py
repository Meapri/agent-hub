"""Cross-process serialization helpers for provider authentication state."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import threading
import time
from typing import Iterator


_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_REFRESH_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def _process_lock(directory: Path) -> threading.RLock:
    key = str(directory.expanduser().resolve())
    with _LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


def _refresh_process_lock(directory: Path) -> threading.Lock:
    key = str(directory.expanduser().resolve())
    with _LOCKS_GUARD:
        return _REFRESH_PROCESS_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def auth_state_lock(directory: Path) -> Iterator[None]:
    """Serialize consent, pending-flow, and credential commits per provider."""

    root = directory.expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    process_lock = _process_lock(root)
    with process_lock:
        descriptor = os.open(root / ".auth-state.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


@contextmanager
def refresh_operation_lock(
    directory: Path,
    *,
    timeout: float = 120.0,
) -> Iterator[None]:
    """Serialize a provider's full remote refresh exchange across processes."""

    root = directory.expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    process_lock = _refresh_process_lock(root)
    bounded_timeout = max(0.1, float(timeout))
    deadline = time.monotonic() + bounded_timeout
    if not process_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("provider refresh operation is already running")
    descriptor = -1
    try:
        descriptor = os.open(
            root / ".auth-refresh.lock",
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("provider refresh operation is already running")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        process_lock.release()


def file_revision(path: Path) -> str:
    """Return an opaque revision that changes when a local state file changes."""

    try:
        payload = path.read_bytes()
        metadata = path.stat()
    except OSError:
        return "missing"
    material = b"\0".join(
        (
            str(metadata.st_dev).encode(),
            str(metadata.st_ino).encode(),
            str(metadata.st_mtime_ns).encode(),
            payload,
        )
    )
    return hashlib.sha256(material).hexdigest()


def consent_file_revision(path: Path) -> str:
    """Backward-compatible name for consent callers."""

    return file_revision(path)
