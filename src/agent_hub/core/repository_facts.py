"""Bounded repository file facts shared by durable document paths."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable


_FALLBACK_SKIP_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}


def _git_repository_files(root: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=str(root),
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return sorted(
        item.decode("utf-8", errors="replace")
        for item in proc.stdout.split(b"\0")
        if item and (root / item.decode("utf-8", errors="replace")).is_file()
    )


def _filesystem_files(root: Path) -> Iterable[str]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _FALLBACK_SKIP_PARTS for part in relative.parts):
            continue
        yield relative.as_posix()


def collect_repository_manifest(
    project_root: str | Path,
    *,
    max_files: int = 4_000,
    max_chars: int = 80_000,
) -> Dict[str, Any]:
    """Return a bounded, deterministic manifest for durable fact checking.

    Git-tracked and non-ignored untracked paths are preferred so documents can
    describe new files in the current change without including ignored local
    artifacts.  A filesystem fallback keeps the helper useful for non-Git
    fixtures.
    """

    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project_root is not a directory: {root}")
    candidates = _git_repository_files(root) or sorted(set(_filesystem_files(root)))
    selected: list[str] = []
    char_count = 0
    for path in candidates:
        added = len(path) + 1
        if len(selected) >= max_files or char_count + added > max_chars:
            break
        selected.append(path)
        char_count += added
    complete = len(selected) == len(candidates)
    directories = sorted(
        {
            parent.as_posix()
            for item in selected
            for parent in Path(item).parents
            if parent.as_posix() not in {"", "."}
        }
    )
    return {
        "repository_files": selected,
        "repository_directories": directories,
        "repository_manifest_complete": complete,
        "repository_manifest_total": len(candidates),
    }
