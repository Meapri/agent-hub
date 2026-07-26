"""Bounded local FTS5 project indexing with no cloud embeddings."""

from __future__ import annotations

from fnmatch import fnmatch
from hashlib import sha256
from pathlib import Path
import re
import stat
import time
from typing import Any, Iterable, Mapping

from .contracts import canonical_project_root
from .egress import prepare_egress, redact_secret_lines
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
FACT_WINDOW_RADIUS = 18


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
    secret_candidates_redacted = 0
    truncated = False
    for path in sorted(root.rglob("*")):
        if len(indexed) >= MAX_INDEX_FILES:
            truncated = True
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
        content, redacted = redact_secret_lines(content)
        secret_candidates_redacted += redacted
        if total_chars + len(content) > MAX_INDEX_TOTAL_CHARS:
            truncated = True
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
    removed = (
        0
        if truncated
        else store.prune_context_documents(
            project_identity=identity,
            namespace=namespace,
            keep_path_aliases=[str(item["path"]) for item in indexed],
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
        "secret_candidates_redacted": secret_candidates_redacted,
        "complete": not truncated,
        "stale_documents_removed": removed,
        "cloud_embedding_used": False,
    }


def _query_terms(query: str) -> list[str]:
    return list(
        dict.fromkeys(
            token.casefold()
            for token in re.findall(r"[\w./:-]+", query, flags=re.UNICODE)
            if len(token) >= 2
        )
    )[:20]


def _matching_window(content: str, query: str) -> tuple[str, int, int, bool]:
    lines = content.splitlines()
    if not lines:
        return "", 1, 1, True
    terms = _query_terms(query)
    matches = [
        index for index, line in enumerate(lines) if any(term in line.casefold() for term in terms)
    ]
    anchor = matches[0] if matches else 0
    start = max(0, anchor - FACT_WINDOW_RADIUS)
    end = min(len(lines), anchor + FACT_WINDOW_RADIUS + 1)
    return "\n".join(lines[start:end]), start + 1, end, start == 0 and end == len(lines)


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
    items = []
    for item in matches:
        content, start_line, end_line, complete = _matching_window(
            str(item["content"]),
            query,
        )
        items.append(
            {
                "path": item["path"],
                "start_line": start_line,
                "end_line": end_line,
                "complete": complete,
                "scope_complete": False,
                "source_sha256": item["content_sha256"],
                "content_sha256": sha256(content.encode("utf-8")).hexdigest(),
                "collected_at": item["indexed_at"],
                "content": content,
            }
        )
    return {
        "schema": FACT_PACK_SCHEMA,
        "project_identity": project_identity(project_root),
        "query_sha256": sha256(query.encode("utf-8")).hexdigest(),
        "collected_at": time.time(),
        "items": items,
        "coverage": {
            "requested_paths": [],
            "covered_paths": [item["path"] for item in items],
            "missing_paths": [],
            "complete": False,
            "reason": "unbounded_query",
        },
        "total_chars": sum(len(item["content"]) for item in items),
        "retrieval": "sqlite_fts5",
    }


def collect_scoped_fact_pack(
    *,
    project_root: str,
    source_paths: Iterable[str],
    expected_sources: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    requested = list(dict.fromkeys(str(path) for path in source_paths))
    if not requested:
        raise HubV2Error(
            "inspection_scope_empty",
            "A scoped inspection requires at least one approved source.",
            scope="context",
        )
    proposal = prepare_egress(
        project_root=project_root,
        provider="local",
        model=None,
        destination_providers=["local"],
        source_paths=requested,
        policy_revision=0,
        estimated_max_tokens=0,
    )
    fact_pack = dict(proposal["fact_pack"])
    approved = dict(expected_sources or {})
    mismatched = [
        item["path"]
        for item in fact_pack["items"]
        if approved and approved.get(item["path"]) != item["content_sha256"]
    ]
    if mismatched:
        raise HubV2Error(
            "inspection_source_changed",
            "An approved inspection source changed after planning.",
            scope="context",
            retryable=True,
            safe_details={"paths": mismatched[:20]},
        )
    covered = [str(item["path"]) for item in fact_pack["items"]]
    missing = [path for path in requested if path not in covered]
    fact_pack.update(
        {
            "query_sha256": None,
            "coverage": {
                "requested_paths": requested,
                "covered_paths": covered,
                "missing_paths": missing,
                "complete": not missing and all(item["complete"] for item in fact_pack["items"]),
            },
            "retrieval": "approved_complete_sources",
        }
    )
    return fact_pack


def require_index_results(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("indexed_count", 0) == 0:
        raise HubV2Error(
            "context_index_empty",
            "No supported project text files were indexed.",
            scope="context",
        )
    return result
