"""Bounded local FTS5 project indexing with no cloud embeddings."""

from __future__ import annotations

from fnmatch import fnmatch
from hashlib import sha256
from pathlib import Path
import stat
import time
from typing import Any, Iterable

from .contracts import canonical_project_root
from .errors import HubV2Error
from .store import HubStore

CONTEXT_INDEX_SCHEMA = "agent_hub_context_index_v2"
FACT_PACK_SCHEMA = "fact_pack_v2"
MAX_INDEX_FILES = 2_000
MAX_INDEX_FILE_BYTES = 512 * 1024
MAX_INDEX_TOTAL_CHARS = 5_000_000
DEFAULT_SUFFIXES = frozenset(
    {
        ".md",
        ".txt",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".sh",
        ".swift",
        ".rs",
        ".go",
    }
)
DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".agent-hub",
    }
)


def project_identity(project_root: str) -> str:
    root = canonical_project_root(project_root)
    return sha256(root.encode("utf-8")).hexdigest()


def _ignore_patterns(root: Path) -> list[str]:
    path = root / ".gitignore"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return [
        line.strip().lstrip("/")
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("!") and not line.lstrip().startswith("#")
    ]


def _ignored(alias: str, patterns: Iterable[str]) -> bool:
    return any(
        fnmatch(alias, pattern)
        or fnmatch(Path(alias).name, pattern)
        or (pattern.endswith("/") and alias.startswith(pattern.rstrip("/") + "/"))
        for pattern in patterns
    )


def index_project(
    store: HubStore,
    *,
    project_root: str,
    namespace: str = "code",
    suffixes: Iterable[str] = DEFAULT_SUFFIXES,
) -> dict[str, Any]:
    root = Path(canonical_project_root(project_root))
    identity = project_identity(str(root))
    allowed_suffixes = set(suffixes)
    patterns = _ignore_patterns(root)
    indexed: list[dict[str, Any]] = []
    skipped = 0
    total_chars = 0
    for path in sorted(root.rglob("*")):
        if len(indexed) >= MAX_INDEX_FILES:
            break
        try:
            alias = path.relative_to(root).as_posix()
            if any(part in DEFAULT_EXCLUDED_DIRS for part in path.relative_to(root).parts):
                continue
            if _ignored(alias, patterns):
                continue
            info = path.lstat()
        except OSError:
            skipped += 1
            continue
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or path.suffix.lower() not in allowed_suffixes
            or info.st_size > MAX_INDEX_FILE_BYTES
        ):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped += 1
            continue
        if total_chars + len(content) > MAX_INDEX_TOTAL_CHARS:
            break
        total_chars += len(content)
        indexed.append(
            store.index_context_document(
                project_identity=identity,
                namespace=namespace,
                path_alias=alias,
                content=content,
                complete=True,
            )
        )
    return {
        "schema": CONTEXT_INDEX_SCHEMA,
        "project_identity": identity,
        "namespace": namespace,
        "indexed": indexed,
        "indexed_count": len(indexed),
        "skipped": skipped,
        "total_chars": total_chars,
        "cloud_embedding_used": False,
    }


def search_fact_pack(
    store: HubStore,
    *,
    project_root: str,
    query: str,
    namespace: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    matches = store.search_context(
        project_identity=project_identity(project_root),
        query=query,
        namespace=namespace,
        limit=limit,
    )
    return {
        "schema": FACT_PACK_SCHEMA,
        "project_identity": project_identity(project_root),
        "query_sha256": sha256(query.encode("utf-8")).hexdigest(),
        "collected_at": time.time(),
        "items": [
            {
                "path": item["path"],
                "start_line": None,
                "end_line": None,
                "complete": False,
                "content_sha256": item["content_sha256"],
                "collected_at": item["indexed_at"],
                "content": item["excerpt"],
            }
            for item in matches
        ],
        "total_chars": sum(len(item["excerpt"]) for item in matches),
        "retrieval": "sqlite_fts5",
    }


def require_index_results(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("indexed_count", 0) == 0:
        raise HubV2Error(
            "context_index_empty",
            "No supported project text files were indexed.",
            scope="context",
        )
    return result
