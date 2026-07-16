"""Durable document fact packs for product docs (README / technical-doc).

Leaf-side helper aligned with orchestrate-codex durable policy:
version, skills, MCP tool names — never git diary or session work.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List


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
    for init in list(root.glob("*/__init__.py"))[:8]:
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


def _mcp_tools(root: Path) -> List[str]:
    names: List[str] = []
    for path in root.glob("*/mcp_server.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(
            r'"(google_[a-z0-9_]+|claude_codex_[a-z0-9_]+|grok_codex_[a-z0-9_]+|orchestrate_[a-z0-9_]+)"',
            text,
        ):
            if m.group(1) not in names:
                names.append(m.group(1))
    return sorted(names)


def collect_durable_facts(project_root: str | Path) -> Dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project_root is not a directory: {root}")
    version = _version_from_tree(root)
    skills = _list_skills(root)
    tools = _mcp_tools(root)
    has_license = (root / "LICENSE").is_file() or (root / "LICENSE.md").is_file()
    lines = [
        "DURABLE FACT PACK (product facts only — ignore session diary / recent commits)",
        f"Project root: {root}",
        f"Version: {version or '[unknown]'}",
        f"License file present: {has_license}",
        f"Skills: {', '.join(skills) if skills else '[none detected]'}",
        f"MCP tools detected: {', '.join(tools) if tools else '[none detected]'}",
        "Do not invent tools, env vars, install steps, or features not listed here or in source text.",
        "Forbidden tone: today/just fixed/session work/HTTP debug notes as product docs.",
    ]
    return {
        "ok": True,
        "root": str(root),
        "version": version or "[unknown]",
        "skills": skills,
        "mcp_tools_detected": tools,
        "has_license": has_license,
        "text": "\n".join(lines),
    }
