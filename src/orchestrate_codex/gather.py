"""Local gather stages: durable fact packs and git snapshots."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


def _run(cmd: List[str], cwd: Path, timeout: float = 20.0) -> str:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        # Non-zero exit: do not pass stdout/stderr off as real content.
        return ""
    return (proc.stdout or "").strip()


def gather_git(project_root: str | Path = ".") -> Dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project_root is not a directory: {root}")
    git_root = _run(["git", "rev-parse", "--show-toplevel"], root)
    if not git_root:
        return {"ok": False, "error": "not a git repository", "root": str(root)}
    repo = Path(git_root)
    return {
        "ok": True,
        "root": str(repo),
        "branch": _run(["git", "branch", "--show-current"], repo) or "[detached]",
        "head": _run(["git", "rev-parse", "--short", "HEAD"], repo),
        "status": _run(["git", "status", "--short"], repo) or "clean",
        "log": _run(["git", "log", "--oneline", "-12"], repo),
        "diff_stat": _run(["git", "diff", "--stat", "HEAD"], repo)
        or _run(["git", "diff", "--stat"], repo),
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _version_from_tree(root: Path) -> str:
    plugin = root / ".codex-plugin" / "plugin.json"
    data = _read_json(plugin)
    if isinstance(data, dict) and data.get("version"):
        return str(data["version"])
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        if m:
            return m.group(1)
    init_files = list(root.glob("*/__init__.py"))
    for init in init_files[:5]:
        text = init.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        if m:
            return m.group(1)
    return ""


def _list_skills(root: Path) -> List[str]:
    skills = root / "skills"
    if not skills.is_dir():
        return []
    return sorted(p.name for p in skills.iterdir() if p.is_dir() and not p.name.startswith("."))


def _mcp_tools_from_config(root: Path) -> List[str]:
    names: List[str] = []
    for rel in ("mcp_config.json", ".mcp.json"):
        data = _read_json(root / rel)
        if not isinstance(data, dict):
            continue
        # tools not always listed; fall through
    # Scan mcp_server.py tool name strings if present
    for path in root.glob("*/mcp_server.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'"name":\s*"([a-z0-9_]+)"', text):
            name = m.group(1)
            if name not in names and ("codex" in name or name.startswith("google_") or name.startswith("orchestrate_")):
                names.append(name)
        for m in re.finditer(r'"(claude_codex_[a-z0-9_]+|grok_codex_[a-z0-9_]+|google_[a-z0-9_]+|orchestrate_[a-z0-9_]+)"', text):
            if m.group(1) not in names:
                names.append(m.group(1))
    return sorted(set(names))


def _cli_commands_from_tree(root: Path) -> List[str]:
    """CLI entry points a README may legitimately reference: scripts/*.py basenames and
    pyproject [project.scripts] console-script names. Without these, verify would flag a
    correct `python3 scripts/foo.py` reference as a hallucinated tool."""
    names: List[str] = []
    scripts = root / "scripts"
    if scripts.is_dir():
        for p in scripts.glob("*.py"):
            if not p.name.startswith("_"):
                names.append(p.stem)
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"(?ms)^\[project\.scripts\]\s*(.*?)(?:^\[|\Z)", text)
        if m:
            for line in m.group(1).splitlines():
                key = line.split("=", 1)[0].strip().strip('"')
                if key and not key.startswith("#"):
                    names.append(key)
    return sorted(set(names))


def _install_commands(root: Path) -> List[str]:
    cmds: List[str] = []
    if (root / "pyproject.toml").is_file():
        cmds.append("pip install -e .")
        text = (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
        if "[project.optional-dependencies]" in text and "dev" in text:
            cmds.append("pip install -e '.[dev]'")
    if (root / ".codex-plugin").is_dir():
        cmds.append(f'codex plugin marketplace add "{root}"')
    return cmds


def gather_durable_facts(project_root: str | Path = ".") -> Dict[str, Any]:
    """Deterministic product facts — no git diary / recent commits."""
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project_root is not a directory: {root}")
    version = _version_from_tree(root)
    tools = _mcp_tools_from_config(root)
    cli_commands = _cli_commands_from_tree(root)
    install_commands = _install_commands(root)
    packages = sorted(
        d.name for d in root.iterdir() if d.is_dir() and (d / "__init__.py").is_file()
    )
    skills = _list_skills(root)
    has_license = (root / "LICENSE").is_file() or (root / "LICENSE.md").is_file()
    readme = root / "README.md"
    readme_preview = ""
    if readme.is_file():
        readme_preview = readme.read_text(encoding="utf-8", errors="replace")[:1500]
    facts = {
        "ok": True,
        "root": str(root),
        "name": root.name,
        "version": version or "[unknown]",
        "skills": skills,
        "mcp_tools_detected": tools,
        "cli_commands": cli_commands,
        "install_commands": install_commands,
        "packages": packages,
        "has_license": has_license,
        "install_hints": install_commands or [
            f'codex plugin marketplace add "{root}"',
        ],
        "readme_preview_chars": len(readme_preview),
        "forbidden_in_output": [
            "session diary",
            "today we fixed",
            "HTTP 400 debug notes",
            "recent commits as product features",
        ],
        "text": _facts_as_text(
            root=root,
            version=version,
            skills=skills,
            tools=tools,
            cli_commands=cli_commands,
            install_commands=install_commands,
            has_license=has_license,
            readme_preview=readme_preview,
        ),
    }
    return facts


def _facts_as_text(
    *,
    root: Path,
    version: str,
    skills: List[str],
    tools: List[str],
    cli_commands: List[str],
    install_commands: List[str],
    has_license: bool,
    readme_preview: str,
) -> str:
    lines = [
        "DURABLE FACT PACK (use only these product facts; ignore session diary)",
        f"Project root: {root}",
        f"Version: {version or '[unknown]'}",
        f"License file present: {has_license}",
        f"Skills: {', '.join(skills) if skills else '[none detected]'}",
        f"MCP tools detected: {', '.join(tools) if tools else '[none detected in tree]'}",
        f"CLI commands: {', '.join(cli_commands) if cli_commands else '[none detected]'}",
        f"Install commands: {', '.join(install_commands) if install_commands else '[none detected]'}",
        "Do not invent tools, env vars, or install commands not listed here or in source_file.",
    ]
    if readme_preview:
        lines.append("Existing README preview (may be outdated; prefer facts above):")
        lines.append(readme_preview)
    return "\n".join(lines)


_CODE_EXTS = {".py", ".toml", ".md", ".json", ".cfg", ".ini", ".txt", ".yaml", ".yml"}
_CODE_SKIP_PARTS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", "dist", "build"}


def gather_code_context(
    project_root: str | Path = ".", *, max_files: int = 45, max_chars: int = 24000
) -> Dict[str, Any]:
    """Collect actual source text for LLM investigators to read and reason over.

    This is the raw material multiple leaf LLMs analyze (architecture, usage, …) —
    distinct from the deterministic durable fact pack, which stays a guardrail.
    """
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project_root is not a directory: {root}")

    def _priority(p: Path) -> tuple:
        name = p.name.lower()
        rank = 0 if name in {"pyproject.toml", "readme.md", "package.json"} else (
            1 if p.suffix == ".py" else 2
        )
        return (rank, len(p.relative_to(root).parts), str(p))

    candidates = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in _CODE_EXTS:
            continue
        if any(part in _CODE_SKIP_PARTS or part.endswith(".egg-info") for part in p.relative_to(root).parts):
            continue
        candidates.append(p)
    candidates.sort(key=_priority)

    parts: List[str] = []
    used_files: List[str] = []
    total = 0
    for p in candidates[: max_files * 2]:
        if len(used_files) >= max_files or total >= max_chars:
            break
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(p.relative_to(root))
        budget = max(0, max_chars - total)
        snippet = body[: min(4000, budget)]
        if not snippet:
            break
        block = f"\n===== FILE: {rel} =====\n{snippet}"
        parts.append(block)
        used_files.append(rel)
        total += len(block)

    text = f"CODE CONTEXT for {root.name} ({len(used_files)} files, ~{total} chars):\n" + "".join(parts)
    return {"ok": True, "root": str(root), "file_count": len(used_files), "files": used_files, "text": text}


def run_gather(stage: Dict[str, Any], *, project_root: str = ".", args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    args = args or {}
    root = str(args.get("project_root") or project_root or ".")
    cap = str(stage.get("capability") or "")
    if cap == "local_git" or stage.get("id") == "gather_git":
        data = gather_git(root)
        data["text"] = json.dumps(data, ensure_ascii=False, indent=2)
        return data
    if cap == "local_code" or stage.get("id") == "gather_code":
        return gather_code_context(root)
    # default durable / local facts
    return gather_durable_facts(root)
