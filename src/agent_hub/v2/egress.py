"""Local fact-pack gathering and explicit egress manifests."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
import stat
import time
from typing import Any, Iterable, Mapping

from .contracts import (
    EGRESS_MANIFEST_SCHEMA,
    canonical_json,
    canonical_project_root,
    ensure_public_model_id,
)
from .errors import HubV2Error

FACT_PACK_SCHEMA = "fact_pack_v2"
MAX_SOURCE_FILES = 100
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_CHARS = 1_000_000

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{16,}", re.IGNORECASE),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}", re.IGNORECASE),
    re.compile(r"xai-[A-Za-z0-9_-]{8,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}", re.IGNORECASE),
    re.compile(
        r"""(?ix)
        ["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|
        aws[_-]?secret[_-]?access[_-]?key|password|passwd|secret[_-]?key|
        database[_-]?url)["']?
        \s*[:=]\s*(?:["'][^"'\r\n]{8,}["']|[^\s#;,]{8,})
        """
    ),
)
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")
_SENSITIVE_PATH_PARTS = frozenset({".git", ".ssh", ".aws", ".gnupg"})
_SENSITIVE_FILENAMES = frozenset(
    {
        ".netrc",
        "credentials.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})


def _source_path(root: Path, relative: str) -> tuple[Path, str]:
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise HubV2Error(
            "invalid_source_path",
            "Egress source paths must be project-relative.",
            scope="egress",
        )
    lowered_parts = tuple(part.lower() for part in raw.parts)
    lowered_name = raw.name.lower()
    if (
        any(part in _SENSITIVE_PATH_PARTS for part in lowered_parts)
        or lowered_name.startswith(".env")
        or lowered_name in _SENSITIVE_FILENAMES
        or raw.suffix.lower() in _SENSITIVE_SUFFIXES
    ):
        raise HubV2Error(
            "sensitive_source_denied",
            "A credential-sensitive path cannot be included in provider egress.",
            scope="egress",
            safe_details={"path": raw.as_posix()},
        )
    target = root / raw
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise HubV2Error(
            "source_unavailable",
            "An egress source file does not exist.",
            scope="egress",
            safe_details={"path": raw.as_posix()},
        ) from exc
    try:
        alias = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise HubV2Error(
            "source_path_escape",
            "An egress source escapes the project.",
            scope="egress",
        ) from exc
    info = resolved.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise HubV2Error(
            "unsafe_source_file",
            "An egress source must be a safe regular file.",
            scope="egress",
            safe_details={"path": alias},
        )
    if info.st_size > MAX_SOURCE_BYTES:
        raise HubV2Error(
            "source_too_large",
            "An egress source exceeds the size limit.",
            scope="egress",
            safe_details={"path": alias, "maximum_bytes": MAX_SOURCE_BYTES},
        )
    return resolved, alias


def redact_secret_lines(text: str) -> tuple[str, int]:
    """Redact whole lines containing credential-like values while preserving line numbers."""
    redacted: list[str] = []
    matches = 0
    private_key_block = False
    for line in text.splitlines(keepends=True):
        begins_private_key = _PRIVATE_KEY_BEGIN.search(line) is not None
        ends_private_key = _PRIVATE_KEY_END.search(line) is not None
        secret_candidate = (
            private_key_block
            or begins_private_key
            or any(pattern.search(line) for pattern in _SECRET_PATTERNS)
        )
        if secret_candidate:
            ending = "\n" if line.endswith("\n") else ""
            redacted.append("[REDACTED SECRET CANDIDATE]" + ending)
            matches += 1
        else:
            redacted.append(line)
        if begins_private_key:
            private_key_block = True
        if private_key_block and ends_private_key:
            private_key_block = False
    return "".join(redacted), matches


def _fact_pack_identity(fact_pack: Mapping[str, Any]) -> dict[str, Any]:
    """Return the content identity without collection-time metadata."""
    identity = {key: value for key, value in fact_pack.items() if key not in {"collected_at"}}
    items = fact_pack.get("items")
    if isinstance(items, list):
        identity["items"] = [
            {key: value for key, value in item.items() if key not in {"collected_at"}}
            if isinstance(item, Mapping)
            else item
            for item in items
        ]
    return identity


def prepare_egress(
    *,
    project_root: str,
    provider: str,
    model: str | None,
    destination_providers: Iterable[str] | None = None,
    source_paths: Iterable[str],
    policy_revision: int,
    estimated_max_tokens: int,
    artifact_sources: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    root = Path(canonical_project_root(project_root))
    paths = list(source_paths)
    artifacts = list(artifact_sources)
    if len(paths) + len(artifacts) > MAX_SOURCE_FILES:
        raise HubV2Error(
            "too_many_sources",
            "The egress request contains too many files.",
            scope="egress",
            safe_details={"maximum": MAX_SOURCE_FILES},
        )
    collected_at = time.time()
    items: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    total_chars = 0
    total_secret_candidates = 0
    for relative in paths:
        source, alias = _source_path(root, relative)
        try:
            raw = source.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise HubV2Error(
                "source_not_text",
                "An egress source is not readable UTF-8 text.",
                scope="egress",
                safe_details={"path": alias},
            ) from exc
        redacted, secret_candidates = redact_secret_lines(text)
        total_chars += len(redacted)
        if total_chars > MAX_TOTAL_CHARS:
            raise HubV2Error(
                "fact_pack_too_large",
                "The approved fact pack would exceed the size limit.",
                scope="egress",
                safe_details={"maximum_chars": MAX_TOTAL_CHARS},
            )
        total_secret_candidates += secret_candidates
        lines = redacted.splitlines()
        content_sha = sha256(redacted.encode("utf-8")).hexdigest()
        items.append(
            {
                "path": alias,
                "start_line": 1,
                "end_line": max(1, len(lines)),
                "complete": True,
                "collected_at": collected_at,
                "content_sha256": content_sha,
                "content": redacted,
            }
        )
        entries.append(
            {
                "kind": "repository",
                "path_alias": alias,
                "classification": "project",
                "chars": len(redacted),
                "sha256": content_sha,
                "secret_candidates_redacted": secret_candidates,
            }
        )
    for source in artifacts:
        artifact_id = str(source.get("artifact_id") or "")
        text = source.get("content")
        if not artifact_id.startswith("art_") or not isinstance(text, str):
            raise HubV2Error(
                "invalid_artifact_source",
                "An egress artifact source is invalid.",
                scope="egress",
            )
        if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise HubV2Error(
                "source_too_large",
                "An egress artifact exceeds the size limit.",
                scope="egress",
                safe_details={"artifact_id": artifact_id},
            )
        source_sha = str(source.get("content_sha256") or "")
        if sha256(text.encode("utf-8")).hexdigest() != source_sha:
            raise HubV2Error(
                "artifact_source_changed",
                "An egress artifact no longer matches its stored digest.",
                scope="egress",
            )
        redacted, secret_candidates = redact_secret_lines(text)
        total_chars += len(redacted)
        if total_chars > MAX_TOTAL_CHARS:
            raise HubV2Error(
                "fact_pack_too_large",
                "The approved fact pack would exceed the size limit.",
                scope="egress",
                safe_details={"maximum_chars": MAX_TOTAL_CHARS},
            )
        total_secret_candidates += secret_candidates
        alias = f"artifact:{artifact_id}"
        lines = redacted.splitlines()
        content_sha = sha256(redacted.encode("utf-8")).hexdigest()
        items.append(
            {
                "kind": "artifact",
                "artifact_id": artifact_id,
                "path": alias,
                "start_line": 1,
                "end_line": max(1, len(lines)),
                "complete": True,
                "collected_at": collected_at,
                "content_sha256": content_sha,
                "content": redacted,
            }
        )
        entries.append(
            {
                "kind": "artifact",
                "artifact_id": artifact_id,
                "path_alias": alias,
                "classification": str(source.get("sensitivity") or "project"),
                "chars": len(redacted),
                "source_sha256": source_sha,
                "sha256": content_sha,
                "secret_candidates_redacted": secret_candidates,
            }
        )
    fact_pack = {
        "schema": FACT_PACK_SCHEMA,
        "project_identity": sha256(str(root).encode("utf-8")).hexdigest(),
        "collected_at": collected_at,
        "items": items,
        "total_chars": total_chars,
    }
    destinations = list(
        dict.fromkeys(
            [
                str(provider),
                *(str(item) for item in (destination_providers or [])),
            ]
        )
    )
    manifest = {
        "schema": EGRESS_MANIFEST_SCHEMA,
        "provider": str(provider),
        "model": ensure_public_model_id(model) if model else None,
        "destinations": destinations,
        "policy_revision": int(policy_revision),
        "entries": entries,
        "total_chars": total_chars,
        "secret_candidates_redacted": total_secret_candidates,
        "estimated_max_tokens": int(estimated_max_tokens),
        "fact_pack_sha256": sha256(
            canonical_json(_fact_pack_identity(fact_pack)).encode("utf-8")
        ).hexdigest(),
    }
    manifest["manifest_sha256"] = sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    return {
        "schema": "agent_hub_egress_proposal_v2",
        "project_root": str(root),
        "manifest": manifest,
        "fact_pack": fact_pack,
        "approval_required": bool(entries),
    }


def verify_egress_approval(
    proposal: Mapping[str, Any],
    *,
    approved_manifest_sha256: str,
    expected_policy_revision: int,
) -> dict[str, Any]:
    manifest = proposal.get("manifest")
    fact_pack = proposal.get("fact_pack")
    missing = [
        name
        for name, value in (("manifest", manifest), ("fact_pack", fact_pack))
        if not isinstance(value, Mapping)
    ]
    if missing:
        # A proposal arrives here trimmed when the caller forwards only the
        # digest, or rebuilds the object from the fields they cared about.
        # Naming the missing keys is the difference between one retry and none.
        raise HubV2Error(
            "invalid_egress_proposal",
            (
                f"proposal is missing {' and '.join(missing)}; pass the whole object "
                "prepare returned, unmodified."
            ),
            scope="egress",
            safe_details={"missing": missing},
        )
    if manifest.get("manifest_sha256") != approved_manifest_sha256:
        raise HubV2Error(
            "egress_approval_conflict",
            "The approved egress digest does not match.",
            scope="egress",
        )
    if int(manifest.get("policy_revision", -1)) != expected_policy_revision:
        raise HubV2Error(
            "policy_revision_conflict",
            "The egress policy revision changed.",
            scope="egress",
            retryable=True,
        )
    copy = dict(manifest)
    supplied = copy.pop("manifest_sha256", None)
    calculated = sha256(canonical_json(copy).encode("utf-8")).hexdigest()
    if supplied != calculated:
        raise HubV2Error(
            "egress_proposal_tampered",
            "The egress manifest changed after preparation.",
            scope="egress",
        )
    fact_digest = sha256(canonical_json(_fact_pack_identity(fact_pack)).encode("utf-8")).hexdigest()
    if fact_digest != manifest.get("fact_pack_sha256"):
        raise HubV2Error(
            "egress_proposal_tampered",
            "The fact pack changed after preparation.",
            scope="egress",
        )
    return {"manifest": dict(manifest), "fact_pack": dict(fact_pack)}
