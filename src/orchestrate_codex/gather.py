"""Local gather stages: durable fact packs and git snapshots."""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent_hub.core.repository_facts import (
    REPOSITORY_SKIP_PARTS,
    collect_repository_manifest,
    command_in_directory_fd,
    filesystem_repository_files,
    git_repository_files,
    is_sensitive_repository_path,
    read_repository_text,
    repository_file_size,
    repository_path_matches_fd,
    repository_root_fd,
    repository_subdirectories,
    safe_repository_file,
)


def _run_bounded(
    cmd: List[str],
    cwd: Path,
    *,
    timeout: float = 20.0,
    max_bytes: int = 64 * 1024,
    cwd_fd: int | None = None,
) -> tuple[str, bool]:
    """Run a local command while bounding pipe memory and wall-clock time."""

    byte_limit = max(0, int(max_bytes))
    command = cmd
    popen_kwargs: Dict[str, Any] = {"cwd": str(cwd)}
    if cwd_fd is not None:
        command, popen_kwargs = command_in_directory_fd(cmd, cwd_fd)
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **popen_kwargs,
        )
    except OSError:
        return "", False
    if proc.stdout is None:
        proc.kill()
        proc.wait()
        return "", False

    chunks: List[bytes] = []
    total = 0
    truncated = False
    deadline = time.monotonic() + max(0.1, float(timeout))
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                truncated = True
                proc.kill()
                break
            events = selector.select(timeout=min(0.1, remaining_time))
            if not events:
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(proc.stdout.fileno(), min(64 * 1024, byte_limit - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > byte_limit:
                truncated = True
                proc.terminate()
                break
    finally:
        selector.close()
        proc.stdout.close()
        if proc.poll() is None:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        else:
            proc.wait()

    raw = b"".join(chunks)
    if len(raw) > byte_limit:
        raw = raw[:byte_limit]
    if proc.returncode != 0 and not truncated:
        # Non-zero exit: do not pass stdout/stderr off as real content.
        return "", False
    return raw.decode("utf-8", errors="replace").strip(), truncated


def _run(
    cmd: List[str],
    cwd: Path,
    timeout: float = 20.0,
    *,
    max_bytes: int = 64 * 1024,
) -> str:
    return _run_bounded(cmd, cwd, timeout=timeout, max_bytes=max_bytes)[0]


def gather_git(
    project_root: str | Path = ".",
    *,
    max_chars: int = 32_000,
    root_fd: int | None = None,
) -> Dict[str, Any]:
    if root_fd is None:
        root = validate_project_root(project_root)
        with repository_root_fd(root) as opened_root_fd:
            return gather_git(
                root,
                max_chars=max_chars,
                root_fd=opened_root_fd,
            )
    root = Path(project_root).expanduser()
    if not root.is_absolute():
        raise ValueError("project_root must be absolute when root_fd is set")
    output_limit = min(128_000, max(256, int(max_chars)))
    git_root, root_truncated = _run_bounded(
        ["git", "rev-parse", "--show-toplevel"],
        root,
        max_bytes=min(8_192, output_limit),
        cwd_fd=root_fd,
    )
    if not git_root:
        return {"ok": False, "error": "not a git repository", "root": str(root)}
    if root_fd is None:
        repo = Path(git_root).resolve()
        try:
            scope_text = root.relative_to(repo).as_posix() or "."
        except ValueError:
            return {
                "ok": False,
                "error": "project_root is outside the detected git repository",
                "root": str(root),
            }
    else:
        prefix, prefix_truncated = _run_bounded(
            ["git", "rev-parse", "--show-prefix"],
            root,
            max_bytes=min(8_192, output_limit),
            cwd_fd=root_fd,
        )
        root_truncated = root_truncated or prefix_truncated
        scope_text = prefix.rstrip("/") or "."
    remaining = output_limit
    output_truncated = root_truncated

    def field(command: List[str], *, fallback: str = "") -> str:
        nonlocal remaining, output_truncated
        value, was_truncated = _run_bounded(
            command,
            root,
            max_bytes=max(0, remaining),
            cwd_fd=root_fd,
        )
        remaining = max(0, remaining - len(value.encode("utf-8")))
        output_truncated = output_truncated or was_truncated
        if value:
            return value
        return "[truncated]" if was_truncated else fallback

    scoped = scope_text != "."
    branch = (
        "[scoped]"
        if scoped
        else field(["git", "branch", "--show-current"], fallback="[detached]")
    )
    head = field(["git", "rev-parse", "--short", "HEAD"])
    status = field(
        ["git", "status", "--short", "--untracked-files=all", "--", "."],
        fallback="clean",
    )
    log = field(
        [
            "git",
            "log",
            "--format=%h" if scoped else "--oneline",
            "-12",
            "--",
            ".",
        ]
    )
    diff_stat = field(["git", "diff", "--stat", "HEAD", "--", "."])
    if not diff_stat and remaining:
        diff_stat = field(["git", "diff", "--stat", "--", "."])
    return {
        "ok": True,
        "root": str(root),
        "scope": scope_text,
        "branch": branch,
        "head": head,
        "status": status,
        "log": log,
        "diff_stat": diff_stat,
        "output_char_limit": output_limit,
        "output_truncated": output_truncated,
    }


_DURABLE_READ_BYTE_LIMIT = 4 * 1024 * 1024
_DURABLE_TEXT_CHAR_LIMIT = 100_000
_DURABLE_METADATA_ENTRY_LIMIT = 1_000


def _read_json_text(text: str) -> Any:
    try:
        return json.loads(text) if text else None
    except (ValueError, TypeError):
        return None


def _package_init_files_from_tree(available: set[str]) -> Dict[str, str]:
    packages: Dict[str, str] = {}
    for item in available:
        parts = Path(item).parts
        if not parts or parts[-1] != "__init__.py":
            continue
        if len(parts) == 2:
            packages[parts[0]] = item
        elif len(parts) == 3 and parts[0] == "src":
            packages[parts[1]] = item
    return packages


def _version_from_tree(available: set[str], read_text: Callable[[str], str]) -> str:
    data = _read_json_text(read_text(".codex-plugin/plugin.json"))
    if isinstance(data, dict) and data.get("version"):
        return str(data["version"])
    if "pyproject.toml" in available:
        text = read_text("pyproject.toml")
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        if m:
            return m.group(1)
    init_files = sorted(_package_init_files_from_tree(available).values())
    for init in init_files[:5]:
        text = read_text(init)
        m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        if m:
            return m.group(1)
    return ""


def _list_skills(
    root: Path,
    available: set[str],
    *,
    manifest_source: str,
    root_fd: int,
) -> List[str]:
    skill_roots = (
        Path("skills"),
        Path("hubs/shared/skills"),
        Path("hubs/codex/skills"),
        Path("hubs/claude-code/skills"),
    )
    names: set[str] = set()
    for item in available:
        parts = Path(item).parts
        for skill_root in skill_roots:
            root_parts = skill_root.parts
            if (
                len(parts) > len(root_parts)
                and parts[: len(root_parts)] == root_parts
                and not parts[len(root_parts)].startswith(".")
            ):
                names.add(parts[len(root_parts)])
    if manifest_source != "filesystem":
        return sorted(names)[:_DURABLE_METADATA_ENTRY_LIMIT]
    for skill_root in skill_roots:
        discovered, _truncated = repository_subdirectories(
            skill_root,
            root,
            root_fd=root_fd,
            max_entries=_DURABLE_METADATA_ENTRY_LIMIT,
        )
        names.update(discovered)
    return sorted(names)[:_DURABLE_METADATA_ENTRY_LIMIT]


def _mcp_tools_from_config(
    available: set[str],
    read_text: Callable[[str], str],
) -> List[str]:
    names: List[str] = []
    server_files = sorted(
        item
        for item in available
        if Path(item).name == "mcp_server.py"
        or item == "src/agent_hub/operations.py"
    )
    for path in server_files:
        text = read_text(path)
        for m in re.finditer(r'_spec\(\s*"([a-z0-9_]+)"', text):
            if m.group(1) not in names:
                names.append(m.group(1))
                if len(names) >= _DURABLE_METADATA_ENTRY_LIMIT:
                    return sorted(set(names))
        for m in re.finditer(r'"name":\s*"([a-z0-9_]+)"', text):
            name = m.group(1)
            if name not in names and (
                "codex" in name
                or name.startswith("google_")
                or name.startswith("orchestrate_")
            ):
                names.append(name)
                if len(names) >= _DURABLE_METADATA_ENTRY_LIMIT:
                    return sorted(set(names))
        for m in re.finditer(
            r'"(claude_codex_[a-z0-9_]+|grok_codex_[a-z0-9_]+|'
            r'google_[a-z0-9_]+|orchestrate_[a-z0-9_]+)"',
            text,
        ):
            if m.group(1) not in names:
                names.append(m.group(1))
                if len(names) >= _DURABLE_METADATA_ENTRY_LIMIT:
                    return sorted(set(names))
    return sorted(set(names))[:_DURABLE_METADATA_ENTRY_LIMIT]


def _command_references_from_tree(
    available: set[str],
    read_text: Callable[[str], str],
) -> tuple[List[str], List[str]]:
    """Separate installed entry points from repository-local Python scripts."""

    repository_scripts = [
        Path(item).stem
        for item in available
        if len(Path(item).parts) == 2
        and Path(item).parts[0] == "scripts"
        and Path(item).suffix == ".py"
        and not Path(item).name.startswith("_")
    ]
    console_scripts: List[str] = []
    if "pyproject.toml" in available:
        text = read_text("pyproject.toml")
        m = re.search(r"(?ms)^\[project\.scripts\]\s*(.*?)(?:^\[|\Z)", text)
        if m:
            for line in m.group(1).splitlines():
                key = line.split("=", 1)[0].strip().strip('"')
                if key and not key.startswith("#"):
                    console_scripts.append(key)
                    if len(console_scripts) >= _DURABLE_METADATA_ENTRY_LIMIT:
                        break
    return (
        sorted(set(console_scripts))[:_DURABLE_METADATA_ENTRY_LIMIT],
        sorted(set(repository_scripts))[:_DURABLE_METADATA_ENTRY_LIMIT],
    )


def _cli_commands_from_tree(
    available: set[str],
    read_text: Callable[[str], str],
) -> List[str]:
    """All legitimate command-like references retained for verifier compatibility."""

    console_scripts, repository_scripts = _command_references_from_tree(
        available,
        read_text,
    )
    return sorted(set(console_scripts) | set(repository_scripts))[
        :_DURABLE_METADATA_ENTRY_LIMIT
    ]


def _install_commands(
    root: Path,
    available: set[str],
    read_text: Callable[[str], str],
) -> List[str]:
    cmds: List[str] = []
    if "pyproject.toml" in available:
        cmds.append("pip install -e .")
        text = read_text("pyproject.toml")
        if "[project.optional-dependencies]" in text and "dev" in text:
            cmds.append("pip install -e '.[dev]'")
    if ".codex-plugin/plugin.json" in available:
        cmds.append(f'codex plugin marketplace add "{root}"')
    return cmds


def gather_durable_facts(project_root: str | Path = ".") -> Dict[str, Any]:
    """Deterministic product facts — no git diary / recent commits."""
    root = validate_project_root(project_root)
    durable_read_bytes = 0
    durable_read_skips: Dict[str, int] = {}
    with repository_root_fd(root) as root_fd:
        manifest = collect_repository_manifest(root, root_fd=root_fd)
        if not repository_path_matches_fd(root, root_fd):
            raise ValueError("project_root changed during collection")
        available = set(manifest["repository_files"])

        def read_text(relative: str) -> str:
            nonlocal durable_read_bytes
            if relative not in available:
                return ""
            remaining = _DURABLE_READ_BYTE_LIMIT - durable_read_bytes
            if remaining <= 0:
                durable_read_skips["read_budget"] = (
                    durable_read_skips.get("read_budget", 0) + 1
                )
                return ""
            text, size, reason = read_repository_text(
                root / relative,
                root,
                root_fd=root_fd,
                max_bytes=min(_CODE_MAX_FILE_BYTES, remaining),
            )
            if reason:
                key = "read_budget" if reason == "oversized" else reason
                durable_read_skips[key] = durable_read_skips.get(key, 0) + 1
                return ""
            durable_read_bytes += size
            return text

        version = _version_from_tree(available, read_text)
        tools = _mcp_tools_from_config(available, read_text)
        console_scripts, repository_scripts = _command_references_from_tree(
            available,
            read_text,
        )
        cli_commands = sorted(set(console_scripts) | set(repository_scripts))[
            :_DURABLE_METADATA_ENTRY_LIMIT
        ]
        install_commands = _install_commands(root, available, read_text)
        readme_preview = read_text("README.md")[:1500]
        skills = _list_skills(
            root,
            available,
            manifest_source=str(manifest["repository_manifest_source"]),
            root_fd=root_fd,
        )

    packages = sorted(_package_init_files_from_tree(available))[
        :_DURABLE_METADATA_ENTRY_LIMIT
    ]
    has_license = bool({"LICENSE", "LICENSE.md"} & available)
    rendered_text = _facts_as_text(
        root=root,
        version=version,
        skills=skills,
        tools=tools,
        console_scripts=console_scripts,
        repository_scripts=repository_scripts,
        install_commands=install_commands,
        has_license=has_license,
        readme_preview=readme_preview,
        repository_files=manifest["repository_files"],
        repository_manifest_complete=manifest["repository_manifest_complete"],
        repository_manifest_total=manifest["repository_manifest_total"],
    )
    text_truncated = len(rendered_text) > _DURABLE_TEXT_CHAR_LIMIT
    if text_truncated:
        marker = "\n[durable fact text truncated at configured limit]"
        rendered_text = (
            rendered_text[: _DURABLE_TEXT_CHAR_LIMIT - len(marker)] + marker
        )
    facts = {
        "ok": True,
        "root": str(root),
        "name": root.name,
        "version": version or "[unknown]",
        "skills": skills,
        "mcp_tools_detected": tools,
        "cli_commands": cli_commands,
        "console_scripts": console_scripts,
        "repository_scripts": repository_scripts,
        "install_commands": install_commands,
        "packages": packages,
        "has_license": has_license,
        **manifest,
        "install_hints": install_commands or [
            f'codex plugin marketplace add "{root}"',
        ],
        "readme_preview_chars": len(readme_preview),
        "durable_read_bytes": durable_read_bytes,
        "durable_read_byte_limit": _DURABLE_READ_BYTE_LIMIT,
        "durable_read_skips": dict(sorted(durable_read_skips.items())),
        "text_char_limit": _DURABLE_TEXT_CHAR_LIMIT,
        "text_truncated": text_truncated,
        "forbidden_in_output": [
            "session diary",
            "today we fixed",
            "HTTP 400 debug notes",
            "recent commits as product features",
        ],
        "text": rendered_text,
    }
    return facts


def _facts_as_text(
    *,
    root: Path,
    version: str,
    skills: List[str],
    tools: List[str],
    console_scripts: List[str],
    repository_scripts: List[str],
    install_commands: List[str],
    has_license: bool,
    readme_preview: str,
    repository_files: List[str],
    repository_manifest_complete: bool,
    repository_manifest_total: int,
) -> str:
    lines = [
        "DURABLE FACT PACK (use only these product facts; ignore session diary)",
        f"Project root: {root}",
        f"Version: {version or '[unknown]'}",
        f"License file present: {has_license}",
        f"Skills: {', '.join(skills) if skills else '[none detected]'}",
        f"MCP tools detected: {', '.join(tools) if tools else '[none detected in tree]'}",
        (
            "Installed console scripts: "
            f"{', '.join(console_scripts) if console_scripts else '[none detected]'}"
        ),
        (
            "Repository Python scripts (run by path; not installed console scripts): "
            f"{', '.join(repository_scripts) if repository_scripts else '[none detected]'}"
        ),
        f"Install commands: {', '.join(install_commands) if install_commands else '[none detected]'}",
        "Repository files (deterministic bounded manifest):",
        "\n".join(repository_files) or "[none detected]",
        (
            "Repository manifest complete: "
            f"{repository_manifest_complete} ({len(repository_files)}/{repository_manifest_total})"
        ),
        "Do not invent tools, env vars, or install commands not listed here or in source_file.",
    ]
    if readme_preview:
        lines.append("Existing README preview (may be outdated; prefer facts above):")
        lines.append(readme_preview)
    return "\n".join(lines)


_CODE_EXTS = {".py", ".toml", ".md", ".json", ".cfg", ".ini", ".txt", ".yaml", ".yml"}
_CODE_SKIP_PARTS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", "dist", "build"}
_CODE_MAX_FILE_BYTES = 1_048_576
_CODE_CANDIDATE_LIMITS = {"shallow": 1_000, "standard": 4_000, "deep": 10_000}
_CODE_READ_BYTE_LIMITS = {
    "shallow": 8 * 1024 * 1024,
    "standard": 32 * 1024 * 1024,
    "deep": 128 * 1024 * 1024,
}
_CODE_FOCUS_SCAN_LIMITS = {"shallow": 64, "standard": 256, "deep": 512}
_CODE_CONTEXT_LIMITS = {
    "shallow": {
        "max_files": 20,
        "max_chars": 16_000,
        "broad_file_chars": 1_200,
        "focused_file_chars": 4_000,
        "focused_files": 2,
        "broad_ratio": 0.55,
    },
    "standard": {
        "max_files": 55,
        "max_chars": 60_000,
        "broad_file_chars": 1_600,
        "focused_file_chars": 12_000,
        "focused_files": 6,
        "broad_ratio": 0.45,
    },
    "deep": {
        "max_files": 160,
        "max_chars": 180_000,
        "broad_file_chars": 2_000,
        "focused_file_chars": 40_000,
        "focused_files": 14,
        "broad_ratio": 0.35,
    },
}


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


def _safe_code_file(
    path: Path,
    root: Path,
    *,
    root_fd: int | None = None,
) -> tuple[bool, str]:
    if path.suffix.lower() not in _CODE_EXTS:
        return False, "unsupported"
    if root_fd is not None:
        size, reason = repository_file_size(
            path,
            root,
            root_fd=root_fd,
            max_bytes=_CODE_MAX_FILE_BYTES,
        )
        return size is not None, reason
    return safe_repository_file(path, root, max_bytes=_CODE_MAX_FILE_BYTES)


def _looks_like_git_checkout(root: Path) -> bool:
    current = root
    while True:
        if (current / ".git").exists():
            return True
        if current == current.parent:
            return False
        current = current.parent


def _git_code_paths(
    root: Path,
    *,
    scan_limit: int,
    root_fd: int,
) -> tuple[Optional[List[Path]], bool]:
    relative_paths, truncated = git_repository_files(
        root,
        max_entries=scan_limit,
        max_path_bytes=max(256_000, scan_limit * 512),
        root_fd=root_fd,
    )
    if relative_paths is None:
        if _looks_like_git_checkout(root):
            raise ValueError("Git repository file selection failed; refusing filesystem fallback")
        return None, False
    return [root / item for item in relative_paths], truncated


def _filesystem_code_paths(
    root: Path,
    *,
    scan_limit: int,
    root_fd: int,
) -> tuple[List[Path], bool]:
    path_byte_limit = max(256_000, scan_limit * 512)
    relative_paths, truncated = filesystem_repository_files(
        root,
        max_entries=scan_limit,
        max_path_bytes=path_byte_limit,
        root_fd=root_fd,
    )
    return [root / item for item in relative_paths], truncated


def _code_candidates(
    root: Path,
    *,
    candidate_limit: int,
    root_fd: int,
) -> tuple[List[Path], bool, Dict[str, int]]:
    scan_limit = max(candidate_limit * 4, candidate_limit + 1)
    raw_paths, scan_truncated = _git_code_paths(
        root,
        scan_limit=scan_limit,
        root_fd=root_fd,
    )
    if raw_paths is None:
        raw_paths, scan_truncated = _filesystem_code_paths(
            root,
            scan_limit=scan_limit,
            root_fd=root_fd,
        )
    skipped: Dict[str, int] = {}
    candidates: List[Path] = []
    for path in raw_paths:
        safe, reason = _safe_code_file(path, root, root_fd=root_fd)
        if not safe:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        candidates.append(path)
    candidates.sort(key=lambda path: _code_context_priority(path, root))
    candidate_truncated = scan_truncated or len(candidates) > candidate_limit
    return candidates[:candidate_limit], candidate_truncated, skipped

_FOCUS_STOP_TERMS = {
    "actual",
    "after",
    "agent",
    "and",
    "before",
    "broad",
    "changes",
    "cite",
    "codebase",
    "codex",
    "collection",
    "complete",
    "deep",
    "document",
    "evidence",
    "exact",
    "file",
    "files",
    "final",
    "focused",
    "for",
    "from",
    "have",
    "implementation",
    "include",
    "inspect",
    "investigate",
    "line",
    "model",
    "numbering",
    "orchestrate",
    "original",
    "partial",
    "phase",
    "ranges",
    "read",
    "repository",
    "result",
    "should",
    "source",
    "src",
    "step",
    "test",
    "tests",
    "that",
    "the",
    "this",
    "two",
    "two-phase",
    "verify",
    "versus",
    "with",
    "검증",
    "결과",
    "근거",
    "문서",
    "실제",
    "작업",
    "코드",
    "파일",
}


def _code_context_priority(path: Path, root: Path) -> tuple:
    """Rank durable interfaces before implementation detail and tests before prose."""

    rel = path.relative_to(root)
    lowered = str(rel).lower()
    name = path.name.lower()
    root_contracts = {
        "pyproject.toml",
        "package.json",
        "agents.md",
        "claude.md",
        "readme.md",
        "ruler.toml",
    }
    interface_names = {
        "mcp_server.py",
        "server.py",
        "operations.py",
        "orchestrator.py",
        "recipes.py",
        "runner.py",
        "gather.py",
        "verify.py",
        "main.py",
        "cli.py",
        "__init__.py",
    }
    if len(rel.parts) == 1 and name in root_contracts:
        rank = 0
    elif name in interface_names:
        rank = 1
    elif any(part in {"config", "configs", "instructions", ".ruler"} for part in rel.parts):
        rank = 2
    elif "test" in name or any(part == "tests" for part in rel.parts):
        rank = 3
    elif any(part in {"scripts", "hubs", "plugins"} for part in rel.parts):
        rank = 4
    elif path.suffix == ".py":
        rank = 5
    elif path.suffix in {".toml", ".json", ".yaml", ".yml"}:
        rank = 6
    else:
        rank = 7
    return (rank, len(rel.parts), lowered)


def _interleave_context_categories(candidates: List[Path], root: Path) -> List[Path]:
    """Keep one large category from crowding every other kind of evidence out."""

    buckets: Dict[int, List[Path]] = {}
    for path in candidates:
        rank = int(_code_context_priority(path, root)[0])
        buckets.setdefault(rank, []).append(path)
    ordered: List[Path] = []
    while any(buckets.values()):
        for rank in sorted(buckets):
            if buckets[rank]:
                ordered.append(buckets[rank].pop(0))
    return ordered


def _extract_focus_terms(focus: str | None) -> List[str]:
    """Keep bounded identifiers and path fragments that can locate relevant code."""

    raw_terms = re.findall(r"[A-Za-z0-9_./-]{3,}|[가-힣]{2,}", str(focus or ""))
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in raw_terms:
        normalized = raw.lower().strip("./-")
        variants = [normalized, *re.split(r"[./_-]+", normalized)]
        for term in variants:
            if len(term) < 3 or term in _FOCUS_STOP_TERMS or term.isdigit() or term in seen:
                continue
            seen.add(term)
            ordered.append(term)
            if len(ordered) >= 40:
                return ordered
    return ordered


def _read_code_text(
    path: Path,
    *,
    root: Path | None = None,
    root_fd: int | None = None,
    max_bytes: int = _CODE_MAX_FILE_BYTES,
) -> str:
    read_root = (root or path.parent).resolve()
    safe, _reason = _safe_code_file(path, read_root, root_fd=root_fd)
    if not safe:
        return ""
    if root_fd is None:
        with repository_root_fd(read_root) as opened_root_fd:
            return _read_code_text(
                path,
                root=read_root,
                root_fd=opened_root_fd,
                max_bytes=max_bytes,
            )
    text, _size, _reason = read_repository_text(
        path,
        read_root,
        root_fd=root_fd,
        max_bytes=min(_CODE_MAX_FILE_BYTES, max(0, int(max_bytes))),
    )
    return text


def _focus_score(path: Path, root: Path, terms: List[str], body: str) -> int:
    if not terms:
        return 0
    rel = str(path.relative_to(root)).lower()
    name = path.name.lower()
    lowered = body.lower()
    score = 0
    content_hits = 0
    for term in terms:
        if term == name:
            score += 60
        elif term in name:
            score += 30
        elif term in rel:
            score += 18
        if term in lowered:
            hits = min(8, lowered.count(term))
            score += hits
            content_hits += hits
    rel_parts = path.relative_to(root).parts
    if content_hits and rel_parts and rel_parts[0] == "src":
        score += 24
    elif content_hits and rel_parts and rel_parts[0] == "tests":
        score += 10
    return score


def _explicit_focus_match(path: Path, root: Path, terms: List[str]) -> bool:
    rel = str(path.relative_to(root)).lower()
    name = path.name.lower()
    return any(term == name or term == rel or term.endswith(f"/{name}") for term in terms)


def _render_numbered_range(
    lines: List[str],
    start: int,
    end: int,
    *,
    max_chars: int,
) -> tuple[str, int]:
    chunks: List[str] = []
    used = 0
    actual_end = start
    for index in range(start, min(end, len(lines))):
        rendered = f"{index + 1:>6} | {lines[index]}\n"
        if chunks and used + len(rendered) > max_chars:
            break
        if not chunks and len(rendered) > max_chars:
            rendered = rendered[:max_chars]
        chunks.append(rendered)
        used += len(rendered)
        actual_end = index + 1
        if used >= max_chars:
            break
    return "".join(chunks), actual_end


def _merge_line_windows(matches: List[int], line_count: int, radius: int = 18) -> List[tuple[int, int]]:
    ranges: List[tuple[int, int]] = []
    for line_index in matches:
        start = max(0, line_index - radius)
        end = min(line_count, line_index + radius + 1)
        if ranges and start <= ranges[-1][1] + 2:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def _focused_file_blocks(
    path: Path,
    root: Path,
    body: str,
    terms: List[str],
    *,
    max_chars: int,
) -> tuple[List[str], List[Dict[str, Any]]]:
    """Return a complete small file or several line-numbered relevant windows."""

    rel = str(path.relative_to(root))
    lines = body.splitlines()
    full, full_end = _render_numbered_range(lines, 0, len(lines), max_chars=max_chars)
    full_header = f"\n===== FILE: {rel} | lines 1-{full_end} | complete =====\n"
    if full_end == len(lines) and len(full_header) + len(full) <= max_chars:
        return [full_header + full], [
            {"path": rel, "mode": "complete", "start_line": 1, "end_line": full_end}
        ]

    lowered_terms = [term.lower() for term in terms]
    lowered_body = "\n".join(lines).lower()
    line_match_counts = {
        term: sum(1 for line in lines if term in line.lower())
        for term in lowered_terms
    }
    frequency_cutoff = max(4, len(lines) // 100)
    definition_terms = [
        term
        for term in lowered_terms
        if re.search(
            rf"(?m)^\s*(?:async\s+)?(?:def|class)\s+{re.escape(term)}\b",
            lowered_body,
        )
    ]
    selective_terms = definition_terms + [
        term
        for term in lowered_terms
        if 0 < line_match_counts[term] <= frequency_cutoff and term not in definition_terms
    ]
    active_terms = selective_terms or lowered_terms
    matches = [
        index
        for index, line in enumerate(lines)
        if any(term in line.lower() for term in active_terms)
    ]
    if not matches:
        matches = [0, max(0, len(lines) - 1)]
    windows = _merge_line_windows(matches, len(lines))
    def window_score(item: tuple[int, int]) -> int:
        window_lines = lines[item[0] : item[1]]
        score = sum(
            1
            for line in window_lines
            for term in active_terms
            if term in line.lower()
        )
        joined = "\n".join(window_lines).lower()
        for term in active_terms:
            if re.search(rf"(?m)^\s*(?:async\s+)?(?:def|class)\s+{re.escape(term)}\b", joined):
                score += 80
        return score

    windows.sort(key=lambda item: (-window_score(item), item[0]))
    blocks: List[str] = []
    records: List[Dict[str, Any]] = []
    remaining = max_chars
    for start, end in windows:
        header = f"\n===== FILE: {rel} | focused lines {start + 1}-{end} | partial =====\n"
        if remaining <= len(header) + 80:
            break
        rendered, actual_end = _render_numbered_range(
            lines,
            start,
            end,
            max_chars=remaining - len(header),
        )
        if not rendered:
            continue
        block = header + rendered
        blocks.append(block)
        records.append(
            {
                "path": rel,
                "mode": "focused_window",
                "start_line": start + 1,
                "end_line": actual_end,
            }
        )
        remaining -= len(block)
    return blocks, records


def gather_code_context(
    project_root: str | Path = ".",
    *,
    depth: str = "standard",
    focus: str | None = None,
    max_files: int | None = None,
    max_chars: int | None = None,
) -> Dict[str, Any]:
    """Collect actual source text for LLM investigators to read and reason over.

    This is the raw material multiple leaf LLMs analyze (architecture, usage, …) —
    distinct from the deterministic durable fact pack, which stays a guardrail.
    """
    root = validate_project_root(project_root)
    with repository_root_fd(root) as root_fd:
        return _gather_code_context(
            root,
            root_fd=root_fd,
            depth=depth,
            focus=focus,
            max_files=max_files,
            max_chars=max_chars,
        )


def _gather_code_context(
    root: Path,
    *,
    root_fd: int,
    depth: str,
    focus: str | None,
    max_files: int | None,
    max_chars: int | None,
) -> Dict[str, Any]:
    normalized_depth = str(depth or "standard").strip().lower()
    if normalized_depth not in _CODE_CONTEXT_LIMITS:
        raise ValueError(
            "depth must be one of: " + ", ".join(sorted(_CODE_CONTEXT_LIMITS))
        )
    limits = _CODE_CONTEXT_LIMITS[normalized_depth]
    file_limit = min(
        int(limits["max_files"]),
        max(1, int(max_files or limits["max_files"])),
    )
    char_limit = min(
        int(limits["max_chars"]),
        max(1_000, int(max_chars or limits["max_chars"])),
    )
    metadata_reserve = min(char_limit, min(8_000, max(1_000, char_limit // 4)))
    source_char_limit = max(0, char_limit - metadata_reserve)
    candidate_limit = int(_CODE_CANDIDATE_LIMITS[normalized_depth])
    candidates, candidate_truncated, skipped_file_counts = _code_candidates(
        root,
        candidate_limit=candidate_limit,
        root_fd=root_fd,
    )
    if not repository_path_matches_fd(root, root_fd):
        raise ValueError("project_root changed during collection")
    broad_candidates = (
        _interleave_context_categories(candidates, root)
        if normalized_depth in {"standard", "deep"}
        else candidates
    )

    focus_terms = _extract_focus_terms(focus)
    read_byte_limit = int(_CODE_READ_BYTE_LIMITS[normalized_depth])
    focus_scan_byte_limit = int(read_byte_limit * 0.75)
    read_bytes = 0
    bodies: Dict[Path, str] = {}
    read_budget_skips: set[Path] = set()

    def body_for(path: Path, *, byte_ceiling: int = read_byte_limit) -> str:
        nonlocal read_bytes
        if path in bodies:
            return bodies[path]
        remaining_bytes = max(0, byte_ceiling - read_bytes)
        source_bytes, reason = repository_file_size(
            path,
            root,
            root_fd=root_fd,
            max_bytes=min(_CODE_MAX_FILE_BYTES, remaining_bytes),
        )
        if source_bytes is None:
            key = "read_budget" if reason == "oversized" else reason
            if path not in read_budget_skips:
                skipped_file_counts[key] = skipped_file_counts.get(key, 0) + 1
                read_budget_skips.add(path)
            return ""
        body = _read_code_text(
            path,
            root=root,
            root_fd=root_fd,
            max_bytes=remaining_bytes,
        )
        read_bytes += source_bytes
        bodies[path] = body
        return body

    path_scores = {
        path: _focus_score(path, root, focus_terms, "")
        for path in candidates
    }
    scan_order = sorted(
        candidates,
        key=lambda path: (
            -path_scores[path],
            _code_context_priority(path, root),
        ),
    )
    focus_scan_limit = int(_CODE_FOCUS_SCAN_LIMITS[normalized_depth])
    focus_scan_candidates = scan_order[:focus_scan_limit] if focus_terms else []
    for path in focus_scan_candidates:
        path_scores[path] = _focus_score(
            path,
            root,
            focus_terms,
            body_for(path, byte_ceiling=focus_scan_byte_limit),
        )
    scored = sorted(
        ((path_scores[path], path) for path in candidates),
        key=lambda item: (-item[0], _code_context_priority(item[1], root)),
    )
    focused_candidates = [
        path
        for score, path in scored[: int(limits["focused_files"])]
        if score > 0
    ]
    focused_set = set(focused_candidates)

    repo_map = [str(path.relative_to(root)) for path in candidates]
    git_state = gather_git(
        root,
        max_chars=max(256, min(8_000, char_limit // 5)),
        root_fd=root_fd,
    )
    if not repository_path_matches_fd(root, root_fd):
        raise ValueError("project_root changed during collection")

    broad_parts: List[str] = []
    focused_parts: List[str] = []
    used_files: List[str] = []
    used_set: set[str] = set()
    evidence_segments: List[Dict[str, Any]] = []
    total = 0
    broad_limit = (
        int(source_char_limit * float(limits["broad_ratio"]))
        if focused_candidates
        else source_char_limit
    )
    broad_total = 0
    for p in broad_candidates:
        if len(used_files) >= file_limit or broad_total >= broad_limit:
            break
        if p in focused_set:
            continue
        body = body_for(p)
        if not body:
            continue
        rel = str(p.relative_to(root))
        lines = body.splitlines()
        available = min(
            int(limits["broad_file_chars"]),
            broad_limit - broad_total,
        )
        header = f"\n===== FILE: {rel} | broad sample from line 1 | partial =====\n"
        if available <= len(header) + 40:
            break
        rendered, actual_end = _render_numbered_range(
            lines,
            0,
            len(lines),
            max_chars=available - len(header),
        )
        if not rendered:
            continue
        block = header + rendered
        broad_parts.append(block)
        used_files.append(rel)
        used_set.add(rel)
        evidence_segments.append(
            {
                "path": rel,
                "mode": "broad_sample",
                "start_line": 1,
                "end_line": actual_end,
            }
        )
        broad_total += len(block)
        total += len(block)

    focused_budget_start = source_char_limit - total
    for index, p in enumerate(focused_candidates):
        if len(used_files) >= file_limit or total >= source_char_limit:
            break
        rel = str(p.relative_to(root))
        remaining_candidates = len(focused_candidates) - index
        remaining_budget = source_char_limit - total
        fair_share = max(2_500, remaining_budget // max(1, remaining_candidates))
        requested_cap = (
            int(limits["focused_file_chars"])
            if _explicit_focus_match(p, root, focus_terms)
            else fair_share
        )
        # An explicit path may use the full-file cap, but keep a small slice for
        # every remaining focus candidate so one large file cannot starve them.
        reserved_for_rest = max(0, remaining_candidates - 1) * 2_500
        available = min(
            requested_cap,
            remaining_budget,
            max(2_500, remaining_budget - reserved_for_rest),
        )
        blocks, records = _focused_file_blocks(
            p,
            root,
            body_for(p),
            focus_terms,
            max_chars=available,
        )
        if not blocks:
            continue
        focused_parts.extend(blocks)
        evidence_segments.extend(records)
        if rel not in used_set:
            used_files.append(rel)
            used_set.add(rel)
        total += sum(len(block) for block in blocks)

    map_limit = 80 if normalized_depth == "deep" else 40 if normalized_depth == "standard" else 20
    header = [
        f"CODE CONTEXT for {root.name}",
        f"Investigation depth: {normalized_depth}",
        (
            f"Selected source: {len(used_files)} files, ~{total} chars; "
            f"focused deep reads: {len(focused_candidates)}; "
            f"focused character allowance: {focused_budget_start}"
        ),
        (
            f"Selection limits: {len(candidates)}/{candidate_limit} safe candidates"
            f"{' (candidate list truncated)' if candidate_truncated else ''}; "
            f"{read_bytes}/{read_byte_limit} source bytes read; "
            f"focus scan truncated: "
            f"{bool(focus_terms and len(candidates) > len(focus_scan_candidates))}"
        ),
        f"Skipped files by safety reason: {json.dumps(skipped_file_counts, sort_keys=True)}",
        "Focus candidates (highest relevance first):",
        *[f"- {path.relative_to(root)}" for path in focused_candidates],
        "Every excerpt keeps original line numbers. 'complete' means the whole file is present.",
        "Repository map (bounded):",
        *[f"- {item}" for item in repo_map[:map_limit]],
        "Git state:",
        json.dumps(git_state, ensure_ascii=False, indent=2),
        (
            "Coverage checklist: entry points, public schemas/tools, configuration, tests, "
            "generated-document synchronization, and working-tree state. State any missing evidence."
        ),
    ]
    source_text = (
        "\n\n--- PHASE 1: broad repository coverage ---\n"
        + "".join(broad_parts)
        + "\n\n--- PHASE 2: focus-driven deep reads ---\n"
        + "".join(focused_parts)
    )
    header_text = "\n".join(header)
    header_char_limit = max(0, char_limit - len(source_text))
    header_truncated = len(header_text) > header_char_limit
    if header_truncated:
        marker = "\n[context metadata truncated]"
        if header_char_limit <= len(marker):
            header_text = marker[:header_char_limit]
        else:
            header_text = header_text[: header_char_limit - len(marker)] + marker
    text = header_text + source_text
    complete_files = sorted(
        {record["path"] for record in evidence_segments if record["mode"] == "complete"}
    )
    partial_files = sorted(set(used_files) - set(complete_files))
    return {
        "ok": True,
        "root": str(root),
        "depth": normalized_depth,
        "file_count": len(used_files),
        "files": used_files,
        "complete_files": complete_files,
        "partial_files": partial_files,
        "evidence_segments": evidence_segments,
        "focus_applied": bool(focus_terms),
        "focus_term_count": len(focus_terms),
        "focused_files": [str(path.relative_to(root)) for path in focused_candidates],
        "candidate_count": len(candidates),
        "candidate_limit": candidate_limit,
        "candidate_truncated": candidate_truncated,
        "focus_scan_truncated": bool(
            focus_terms and len(candidates) > len(focus_scan_candidates)
        ),
        "read_bytes": read_bytes,
        "read_byte_limit": read_byte_limit,
        "focus_scan_byte_limit": focus_scan_byte_limit,
        "skipped_file_counts": dict(sorted(skipped_file_counts.items())),
        "source_truncated_files": [],
        "text_chars": len(text),
        "text_char_limit": char_limit,
        "text_truncated": header_truncated,
        "git": git_state,
        "text": text,
    }


def run_gather(stage: Dict[str, Any], *, project_root: str = ".", args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    args = args or {}
    root = str(args.get("project_root") or project_root or ".")
    cap = str(stage.get("capability") or "")
    if cap == "local_git" or stage.get("id") == "gather_git":
        data = gather_git(root)
        data["text"] = json.dumps(data, ensure_ascii=False, indent=2)
        return data
    if cap == "local_code" or stage.get("id") == "gather_code":
        return gather_code_context(
            root,
            depth=str(stage.get("investigation_depth") or args.get("investigation_depth") or "standard"),
            focus=str(
                stage.get("instruction")
                or args.get("instruction")
                or args.get("prompt")
                or ""
            ),
        )
    # default durable / local facts
    return gather_durable_facts(root)
