"""Local gather stages: durable fact packs and git snapshots."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List

from agent_hub.core.repository_facts import (
    REPOSITORY_SKIP_PARTS,
    collect_repository_manifest,
    is_sensitive_repository_path,
    read_repository_text,
    repository_path_matches_fd,
    repository_root_fd,
    repository_subdirectories,
)


_DURABLE_READ_BYTE_LIMIT = 4 * 1024 * 1024
_DURABLE_TEXT_CHAR_LIMIT = 100_000
_DURABLE_METADATA_ENTRY_LIMIT = 1_000
_DURABLE_MAX_FILE_BYTES = 1_048_576


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
        if Path(item).name == "mcp_server.py" or item == "src/agent_hub/v2/tools.py"
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
                "codex" in name or name.startswith("google_") or name.startswith("orchestrate_")
            ):
                names.append(name)
                if len(names) >= _DURABLE_METADATA_ENTRY_LIMIT:
                    return sorted(set(names))
        for m in re.finditer(
            r'"(agent_hub_[a-z0-9_]+|claude_codex_[a-z0-9_]+|grok_codex_[a-z0-9_]+|'
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
                durable_read_skips["read_budget"] = durable_read_skips.get("read_budget", 0) + 1
                return ""
            text, size, reason = read_repository_text(
                root / relative,
                root,
                root_fd=root_fd,
                max_bytes=min(_DURABLE_MAX_FILE_BYTES, remaining),
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

    packages = sorted(_package_init_files_from_tree(available))[:_DURABLE_METADATA_ENTRY_LIMIT]
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
        rendered_text = rendered_text[: _DURABLE_TEXT_CHAR_LIMIT - len(marker)] + marker
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
        "install_hints": install_commands
        or [
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
