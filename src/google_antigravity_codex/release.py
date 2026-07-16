"""Release copilot helpers for the integrated Antigravity Codex plugin."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
from typing import Any, Dict, List
import urllib.parse

from . import response, security, writing

VERSION_FILE_PATTERNS = (
    ("package.json", re.compile(r'"version"\s*:\s*"([^"]+)"')),
    ("pyproject.toml", re.compile(r'(?m)^version\s*=\s*"([^"]+)"')),
    ("Cargo.toml", re.compile(r'(?m)^version\s*=\s*"([^"]+)"')),
    (".codex-plugin/plugin.json", re.compile(r'"version"\s*:\s*"([^"]+)"')),
    ("VERSION", re.compile(r"^\s*([^\s]+)\s*$")),
)
CONVENTIONAL_RE = re.compile(r"^(?P<type>[A-Za-z]+)(?:\([^)]+\))?(?P<breaking>!)?:\s*(?P<body>.+)$")
SEMVER_RE = re.compile(r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)")
CATEGORY_TITLES = {
    "breaking": "Breaking",
    "features": "Added",
    "fixes": "Fixed",
    "performance": "Performance",
    "docs": "Documentation",
    "tests": "Tests",
    "chores": "Chores",
    "other": "Other",
}


@dataclass
class CommandResult:
    command: str
    exit_code: int
    duration_sec: float
    stdout_tail: str
    stderr_tail: str


@dataclass
class VersionInfo:
    path: str
    version: str


@dataclass
class ReleaseSnapshot:
    repo_root: str
    branch: str
    head: str
    base_ref: str
    latest_tag: str
    compare_range: str
    remote_url: str
    compare_url: str
    release_tools: List[str]
    status_short: str
    changed_files: str
    diff_stat: str
    commits: str
    change_categories: Dict[str, List[str]]
    recommended_bump: str
    recommended_version: str
    versions: List[VersionInfo]
    checks: List[CommandResult]


def _tail(text: str, max_chars: int = 4000) -> str:
    text = (text or "").strip()
    return text if len(text) <= max_chars else text[-max_chars:].lstrip()


def _run(repo: Path, args: List[str], *, timeout_sec: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(repo),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
    )


def _git(repo: Path, args: List[str], *, timeout_sec: int = 30) -> str:
    proc = _run(repo, ["git", *args], timeout_sec=timeout_sec)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def repo_root(path: Path) -> Path:
    path = security.resolve_allowed_path(
        path,
        purpose="release repo",
        directory=True,
        explicit_root=path,
    )
    proc = _run(path, ["git", "rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        raise ValueError(f"not a git repository: {path}")
    return security.resolve_allowed_path(
        Path(proc.stdout.strip()),
        purpose="release repo root",
        directory=True,
        explicit_root=path,
    )


def normalize_remote_url(remote: str) -> str:
    remote = remote.strip()
    if remote.startswith("git@github.com:"):
        return "https://github.com/" + remote.removeprefix("git@github.com:").removesuffix(".git")
    if remote.startswith("https://github.com/"):
        return remote.removesuffix(".git")
    if remote.startswith("http://github.com/"):
        return "https://" + remote.removeprefix("http://").removesuffix(".git")
    parsed = urllib.parse.urlsplit(remote)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        if parsed.port:
            host += f":{parsed.port}"
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", "")).removesuffix(".git")
    return remote


def compare_url(remote_url: str, base_ref: str, head_ref: str) -> str:
    if not remote_url.startswith("https://github.com/") or not base_ref:
        return ""
    return f"{remote_url}/compare/{base_ref}...{head_ref}"


def latest_tag(repo: Path) -> str:
    return _git(repo, ["describe", "--tags", "--abbrev=0"])


def default_base(repo: Path) -> str:
    upstream = _git(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    if upstream:
        merge_base = _git(repo, ["merge-base", "HEAD", upstream])
        if merge_base:
            return merge_base
    return latest_tag(repo)


def detect_release_tools(repo: Path) -> List[str]:
    tools: List[str] = []
    names = {".releaserc", ".releaserc.json", ".releaserc.yml", "release.config.js"}
    if any((repo / name).exists() for name in names):
        tools.append("semantic-release")
    if (repo / "release-please-config.json").exists() or (repo / ".release-please-manifest.json").exists():
        tools.append("release-please")
    if (repo / ".changeset").is_dir():
        tools.append("changesets")
    if any((repo / name).exists() for name in (".goreleaser.yml", ".goreleaser.yaml")):
        tools.append("goreleaser")
    return tools


def version_infos(repo: Path) -> List[VersionInfo]:
    found: List[VersionInfo] = []
    for relative, pattern in VERSION_FILE_PATTERNS:
        path = repo / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        match = pattern.search(text)
        if match:
            found.append(VersionInfo(path=relative, version=match.group(1).strip()))
    return found


def strip_commit_hash(line: str) -> str:
    return re.sub(r"^[0-9a-f]{6,40}\s+", "", line.strip())


def categorize_commit(message: str) -> tuple[str, str]:
    message = strip_commit_hash(message)
    match = CONVENTIONAL_RE.match(message)
    if not match:
        return ("breaking" if "BREAKING CHANGE" in message else "other", message)
    kind = match.group("type").lower()
    body = match.group("body").strip()
    if match.group("breaking") or "BREAKING CHANGE" in message:
        return "breaking", body
    if kind == "feat":
        return "features", body
    if kind in {"fix", "bugfix", "hotfix"}:
        return "fixes", body
    if kind == "perf":
        return "performance", body
    if kind == "docs":
        return "docs", body
    if kind in {"test", "tests"}:
        return "tests", body
    if kind in {"chore", "build", "ci", "refactor", "style"}:
        return "chores", body
    return "other", body


def categorize_commits(commits: str) -> Dict[str, List[str]]:
    categories = {key: [] for key in CATEGORY_TITLES}
    for line in commits.splitlines():
        if not line.strip():
            continue
        category, body = categorize_commit(line)
        categories.setdefault(category, []).append(body)
    return {key: value for key, value in categories.items() if value}


def categorize_files(changed_files: str) -> Dict[str, List[str]]:
    items = [line.strip() for line in changed_files.splitlines() if line.strip()]
    return {"other": items[:20]} if items else {}


def recommended_bump(categories: Dict[str, List[str]]) -> str:
    if categories.get("breaking"):
        return "major"
    if categories.get("features"):
        return "minor"
    if any(categories.get(key) for key in ("fixes", "performance", "docs", "tests", "chores", "other")):
        return "patch"
    return "none"


def bump_version(version: str, bump: str) -> str:
    match = SEMVER_RE.match((version or "").strip())
    if not match or bump == "none":
        return ""
    major, minor, patch = int(match.group("major")), int(match.group("minor")), int(match.group("patch"))
    prefix = "v" if version.startswith("v") else ""
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    elif bump == "patch":
        patch += 1
    return f"{prefix}{major}.{minor}.{patch}"


def run_check(repo: Path, command: str, timeout_sec: int) -> CommandResult:
    if not security.env_flag("GOOGLE_ANTIGRAVITY_ALLOW_CHECK_COMMANDS"):
        raise ValueError(
            "release check_commands are disabled; run checks outside MCP or explicitly opt in locally"
        )
    argv = shlex.split(command)
    if not argv:
        raise ValueError("release check command cannot be empty")
    if len(command) > 4096:
        raise ValueError("release check command exceeds the 4096-character limit")
    timeout_sec = max(1, min(int(timeout_sec), 3600))
    started = time.monotonic()
    proc = subprocess.run(
        argv,
        cwd=str(repo),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
    )
    return CommandResult(
        command=command,
        exit_code=proc.returncode,
        duration_sec=round(time.monotonic() - started, 3),
        stdout_tail=_tail(proc.stdout),
        stderr_tail=_tail(proc.stderr),
    )


def collect_snapshot(arguments: Dict[str, Any]) -> ReleaseSnapshot:
    repo = repo_root(Path(str(arguments.get("repo") or ".")).expanduser())
    base = str(arguments.get("base_ref") or "").strip() or default_base(repo)
    head = str(arguments.get("head_ref") or "HEAD").strip() or "HEAD"
    diff_range = [f"{base}..{head}"] if base else []
    log_range = diff_range or ["--max-count=20"]
    commits = _git(repo, ["log", "--oneline", *log_range])
    status_short = _git(repo, ["status", "--short"])
    changed_files = _git(repo, ["diff", "--name-status", *diff_range])
    diff_stat = _git(repo, ["diff", "--stat", *diff_range])
    if status_short and not changed_files:
        changed_files = status_short
        diff_stat = _git(repo, ["diff", "--stat"]) or "[working tree has untracked or staged changes]"
    categories = categorize_commits(commits) or categorize_files(changed_files)
    versions = version_infos(repo)
    primary_version = versions[0].version if versions else latest_tag(repo)
    bump = recommended_bump(categories)
    remote = normalize_remote_url(_git(repo, ["remote", "get-url", "origin"]))
    check_commands = arguments.get("check_commands") or arguments.get("checks") or []
    if isinstance(check_commands, str):
        check_commands = [check_commands] if check_commands.strip() else []
    if len(check_commands) > 10:
        raise ValueError("at most 10 release check commands are allowed")
    checks = [
        run_check(repo, str(command), int(arguments.get("check_timeout_sec") or 600))
        for command in check_commands
    ]
    return ReleaseSnapshot(
        repo_root=str(repo),
        branch=_git(repo, ["branch", "--show-current"]),
        head=_git(repo, ["rev-parse", "--short", head]),
        base_ref=base,
        latest_tag=latest_tag(repo),
        compare_range=diff_range[0] if diff_range else "working tree",
        remote_url=remote,
        compare_url=compare_url(remote, base, head),
        release_tools=detect_release_tools(repo),
        status_short=status_short,
        changed_files=changed_files,
        diff_stat=diff_stat,
        commits=commits,
        change_categories=categories,
        recommended_bump=bump,
        recommended_version=bump_version(primary_version, bump),
        versions=versions,
        checks=checks,
    )


def snapshot_text(snapshot: ReleaseSnapshot) -> str:
    return "\n".join(
        [
            "Google Antigravity Release Snapshot",
            f"  repo: {snapshot.repo_root}",
            f"  branch: {snapshot.branch or '[detached]'}",
            f"  head: {snapshot.head}",
            f"  base: {snapshot.base_ref or '[not detected]'}",
            f"  compare: {snapshot.compare_range}",
            f"  latestTag: {snapshot.latest_tag or '[none]'}",
            f"  recommendedBump: {snapshot.recommended_bump}",
            f"  recommendedVersion: {snapshot.recommended_version or '[not detected]'}",
            f"  releaseTools: {', '.join(snapshot.release_tools) if snapshot.release_tools else '[none]'}",
            f"  workingTree: {'dirty' if snapshot.status_short else 'clean'}",
        ]
    )


def snapshot_to_dict(snapshot: ReleaseSnapshot) -> Dict[str, Any]:
    return asdict(snapshot)


def _markdown_list(items: List[str], fallback: str) -> str:
    return "\n".join(f"- {item}" for item in items) if items else f"- {fallback}"


def _category_sections(categories: Dict[str, List[str]]) -> str:
    if not categories:
        return "- Describe notable changes."
    sections: List[str] = []
    for key, title in CATEGORY_TITLES.items():
        items = categories.get(key)
        if items:
            sections.append(f"### {title}\n{_markdown_list(items, 'Describe notable changes.')}")
    return "\n\n".join(sections)


def _check_summary(checks: List[CommandResult]) -> str:
    if not checks:
        return "- [ ] Add verification commands or note manual testing."
    lines: List[str] = []
    for item in checks:
        mark = "x" if item.exit_code == 0 else " "
        status = "passed" if item.exit_code == 0 else f"failed with exit code {item.exit_code}"
        lines.append(f"- [{mark}] `{item.command}` {status}")
    return "\n".join(lines)


def render_draft(snapshot: ReleaseSnapshot, arguments: Dict[str, Any]) -> str:
    version = str(arguments.get("version") or snapshot.recommended_version or "").strip()
    tag = str(arguments.get("tag") or "").strip()
    title = str(arguments.get("title") or "").strip() or f"Release {version or tag or snapshot.head}"
    tag_text = tag or (f"v{version}" if version and not version.startswith("v") else version) or "[tag not selected]"
    sections = [
        "# Release Copilot Draft",
        "## Snapshot",
        f"- Repository: `{snapshot.repo_root}`",
        f"- Branch: `{snapshot.branch or '[detached]'}`",
        f"- Head: `{snapshot.head}`",
        f"- Base: `{snapshot.base_ref or '[not detected]'}`",
        f"- Compare range: `{snapshot.compare_range}`",
        f"- Compare URL: {snapshot.compare_url or '[not available]'}",
        f"- Detected release tools: {', '.join(snapshot.release_tools) if snapshot.release_tools else '[none]'}",
        f"- Latest tag: `{snapshot.latest_tag or '[none]'}`",
        f"- Recommended bump: `{snapshot.recommended_bump}`",
        f"- Recommended next version: `{snapshot.recommended_version or '[not detected]'}`",
        "",
        "## Working Tree",
        "```text",
        snapshot.status_short or "clean",
        "```",
        "",
        "## Changed Files",
        "```text",
        snapshot.changed_files or "[no changed files in compare range]",
        "```",
        "",
        "## Diff Stat",
        "```text",
        snapshot.diff_stat or "[no diff stat]",
        "```",
        "",
        "## Verification",
        _check_summary(snapshot.checks),
        "",
        "## PR Description Draft",
        "### Summary",
        _category_sections(snapshot.change_categories),
        "",
        "### Validation",
        _check_summary(snapshot.checks),
        "",
        "## Release Notes Draft",
        f"### {title}",
        _category_sections(snapshot.change_categories),
        "",
        "### Compatibility",
        "- [ ] Add breaking changes, migration notes, or mark as none.",
        "",
        "## Changelog Entry Draft",
        f"## {tag_text} - {date.today().isoformat()}",
        _category_sections(snapshot.change_categories),
    ]
    return "\n".join(sections).strip() + "\n"


def release_snapshot(arguments: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = collect_snapshot(arguments)
    return {
        "text": snapshot_text(snapshot),
        "snapshot": snapshot_to_dict(snapshot),
        **response.standard_fields(warnings=[], backend="local-release"),
    }


def release_draft(arguments: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = collect_snapshot(arguments)
    draft = render_draft(snapshot, arguments)
    polished = None
    if bool(arguments.get("polish")):
        polished = writing.run_writing(
            {
                "task": "release-notes",
                "profile": ["github-release"],
                "instruction": "Polish this release draft without inventing facts.",
                "source_text": draft,
                "model": arguments.get("model"),
                "timeout_sec": arguments.get("timeout_sec") or 180,
                "max_tokens": arguments.get("max_tokens") or 4096,
            }
        )
    return {
        "text": polished["text"] if polished else draft,
        "draft": draft,
        "polished": polished,
        "snapshot": snapshot_to_dict(snapshot),
        **response.standard_fields(
            warnings=(polished or {}).get("warnings", []) if polished else [],
            diagnostics=(polished or {}).get("diagnostics", {}) if polished else {},
            backend=str((polished or {}).get("backend") or "local-release"),
        ),
    }


def snapshot_json(arguments: Dict[str, Any]) -> str:
    return json.dumps(snapshot_to_dict(collect_snapshot(arguments)), ensure_ascii=False, indent=2)
