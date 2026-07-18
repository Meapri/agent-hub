"""Local gather stages: durable fact packs and git snapshots."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_hub.core.repository_facts import collect_repository_manifest


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
    manifest = collect_repository_manifest(root)
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
        **manifest,
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
            repository_files=manifest["repository_files"],
            repository_manifest_complete=manifest["repository_manifest_complete"],
            repository_manifest_total=manifest["repository_manifest_total"],
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
        f"CLI commands: {', '.join(cli_commands) if cli_commands else '[none detected]'}",
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


def _read_code_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


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
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project_root is not a directory: {root}")
    normalized_depth = str(depth or "standard").strip().lower()
    if normalized_depth not in _CODE_CONTEXT_LIMITS:
        raise ValueError(
            "depth must be one of: " + ", ".join(sorted(_CODE_CONTEXT_LIMITS))
        )
    limits = _CODE_CONTEXT_LIMITS[normalized_depth]
    file_limit = max(1, int(max_files or limits["max_files"]))
    char_limit = max(1_000, int(max_chars or limits["max_chars"]))

    candidates = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in _CODE_EXTS:
            continue
        if any(part in _CODE_SKIP_PARTS or part.endswith(".egg-info") for part in p.relative_to(root).parts):
            continue
        candidates.append(p)
    candidates.sort(key=lambda path: _code_context_priority(path, root))
    broad_candidates = (
        _interleave_context_categories(candidates, root)
        if normalized_depth in {"standard", "deep"}
        else candidates
    )

    focus_terms = _extract_focus_terms(focus)
    bodies = {path: _read_code_text(path) for path in candidates}
    scored = sorted(
        (
            (_focus_score(path, root, focus_terms, bodies[path]), path)
            for path in candidates
        ),
        key=lambda item: (-item[0], _code_context_priority(item[1], root)),
    )
    focused_candidates = [
        path
        for score, path in scored[: int(limits["focused_files"])]
        if score > 0
    ]
    focused_set = set(focused_candidates)

    repo_map = [str(path.relative_to(root)) for path in candidates]
    git_state = gather_git(root)

    broad_parts: List[str] = []
    focused_parts: List[str] = []
    used_files: List[str] = []
    used_set: set[str] = set()
    evidence_segments: List[Dict[str, Any]] = []
    total = 0
    broad_limit = (
        int(char_limit * float(limits["broad_ratio"]))
        if focused_candidates
        else char_limit
    )
    broad_total = 0
    for p in broad_candidates:
        if len(used_files) >= file_limit or broad_total >= broad_limit:
            break
        if p in focused_set:
            continue
        body = bodies[p]
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

    focused_budget_start = char_limit - total
    for index, p in enumerate(focused_candidates):
        if len(used_files) >= file_limit or total >= char_limit:
            break
        rel = str(p.relative_to(root))
        remaining_candidates = len(focused_candidates) - index
        remaining_budget = char_limit - total
        fair_share = max(2_500, remaining_budget // max(1, remaining_candidates))
        requested_cap = (
            int(limits["focused_file_chars"])
            if _explicit_focus_match(p, root, focus_terms)
            else fair_share
        )
        # An explicit path may use the full-file cap, but keep a small slice for
        # every remaining focus candidate so one large file cannot starve them.
        reserved_for_rest = max(0, remaining_candidates - 1) * 2_500
        available = min(requested_cap, max(2_500, remaining_budget - reserved_for_rest))
        blocks, records = _focused_file_blocks(
            p,
            root,
            bodies[p],
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
    text = (
        "\n".join(header)
        + "\n\n--- PHASE 1: broad repository coverage ---\n"
        + "".join(broad_parts)
        + "\n\n--- PHASE 2: focus-driven deep reads ---\n"
        + "".join(focused_parts)
    )
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
