"""Git-diff-aware code review via Antigravity chat."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_hub.core import limits

from . import chat, model_prefs, response, security

MAX_DIFF_CHARS = 1_000_000
MAX_UNTRACKED_FILES = 100
MAX_UNTRACKED_FILE_BYTES = 1_000_000
DEFAULT_REVIEW_PROMPT = (
    "You are reviewing a git diff for bugs, security issues, regressions, and "
    "missing tests. Be specific: cite file paths and hunks. Separate must-fix "
    "issues from nits. Do not invent code that is not in the diff."
)


def _run_git(repo: Path, args: List[str], timeout: float = 30.0) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0 and not completed.stdout:
        err = (completed.stderr or "").strip()[:300]
        raise RuntimeError(err or f"git {' '.join(args)} failed")
    return completed.stdout or ""


def _collect_diff(
    repo: Path,
    *,
    staged: bool = False,
    base: str = "",
    paths: Optional[List[str]] = None,
    include_untracked: bool = False,
) -> str:
    if staged:
        args = ["diff", "--cached"]
    elif base:
        args = ["diff", base]
    else:
        args = ["diff", "HEAD"]
    if paths:
        args.append("--")
        args.extend(paths)
    tracked = _run_git(repo, args)
    if staged or not include_untracked:
        return tracked

    raw_untracked = _run_git(repo, ["ls-files", "--others", "--exclude-standard", "-z"])
    untracked = [item for item in raw_untracked.split("\0") if item]
    if paths:
        selected: List[str] = []
        for item in untracked:
            for requested in paths:
                prefix = str(requested).strip().strip("/")
                if item == prefix or item.startswith(prefix + "/"):
                    selected.append(item)
                    break
        untracked = selected
    additions: List[str] = []
    for relative in untracked[:MAX_UNTRACKED_FILES]:
        candidate = (repo / relative).resolve()
        try:
            candidate.relative_to(repo)
        except ValueError:
            continue
        if not candidate.is_file() or candidate.is_symlink():
            continue
        try:
            if candidate.stat().st_size > MAX_UNTRACKED_FILE_BYTES:
                continue
            with candidate.open("rb") as stream:
                if b"\0" in stream.read(8192):
                    continue
        except OSError:
            continue
        # git diff --no-index returns 1 when differences are found; _run_git accepts
        # non-zero status when stdout contains the generated patch.
        additions.append(_run_git(repo, ["diff", "--no-index", "--", os.devnull, relative]))
        if len(tracked) + sum(len(item) for item in additions) >= MAX_DIFF_CHARS:
            break
    return tracked + ("\n" if tracked and additions else "") + "\n".join(additions)


def _resolve_repo(cwd: str) -> Path:
    try:
        return security.explicit_workspace_root(cwd)
    except Exception as exc:
        # Fall back only for relative/local paths that still look like a workspace.
        candidate = Path(cwd).expanduser().resolve()
        if not candidate.exists():
            raise ValueError(f"Repository path does not exist: {cwd}") from exc
        return candidate


def review_diff(arguments: Dict[str, Any]) -> Dict[str, Any]:
    cwd = str(arguments.get("cwd") or arguments.get("repo") or ".").strip() or "."
    repo = _resolve_repo(cwd)
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        # allow worktree gitfile
        raise ValueError(f"Not a git repository: {repo}")

    staged = bool(arguments.get("staged"))
    base = str(arguments.get("base") or arguments.get("ref") or "").strip()
    paths = arguments.get("paths")
    path_list = [str(p) for p in paths] if isinstance(paths, list) else None
    include_untracked = bool(arguments.get("include_untracked", False))

    status = _run_git(repo, ["status", "--short"])
    branch = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    diff = _collect_diff(
        repo,
        staged=staged,
        base=base,
        paths=path_list,
        include_untracked=include_untracked,
    )
    if not diff.strip():
        # unstaged empty — try unstaged + untracked message
        return {
            "text": "No diff to review (working tree matches selected base).",
            "success": True,
            "branch": branch,
            "repo": str(repo),
            "diff_chars": 0,
            "status": status,
            **response.standard_fields(backend="diff-review", warnings=["empty_diff"]),
        }

    truncated = False
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS]
        truncated = True

    instruction = str(arguments.get("instruction") or DEFAULT_REVIEW_PROMPT).strip()
    focus = str(arguments.get("focus") or "").strip()
    model = model_prefs.resolve_model(
        explicit=str(arguments.get("model") or ""),
        task="code",
        fallback="gemini-3.1-pro-high",
    )
    # active profile may override
    try:
        from . import profiles

        defaults = profiles.chat_defaults_from_profile()
        if defaults.get("model") and not arguments.get("model"):
            if str(defaults.get("task") or "") in {"code", "chat", "pair"}:
                model = str(defaults["model"])
    except Exception:
        pass

    prompt = (
        f"{instruction}\n\n"
        + (f"Focus: {focus}\n\n" if focus else "")
        + f"Repository: {repo}\nBranch: {branch}\n"
        + f"Diff mode: {'staged' if staged else (f'base={base}' if base else 'HEAD')}\n"
        + ("(diff truncated)\n" if truncated else "")
        + "```diff\n"
        + diff
        + "\n```\n"
        + f"\nGit status:\n```\n{status[:4000]}\n```"
    )

    chat_response = chat.run_chat(
        {
            "prompt": prompt,
            "model": model,
            "task": "code",
            "temperature": arguments.get("temperature", 0.2),
            "max_tokens": int(arguments.get("max_tokens") or chat.DEFAULT_MAX_TOKENS),
            "timeout_sec": (arguments.get("timeout_sec") or limits.MAX_PROVIDER_TIMEOUT_SECONDS),
            "retry_count": arguments.get("retry_count", limits.MAX_PROVIDER_RETRIES),
            "thinking_level": str(arguments.get("thinking_level") or "high"),
        }
    )
    diagnostics = chat_response.get("diagnostics") or {}
    warnings = list(chat_response.get("warnings") or [])
    if truncated:
        warnings.append("diff_truncated")
    if diagnostics.get("capacity_fallback"):
        warnings.append(f"capacity_fallback_used:{diagnostics.get('used_model') or 'unknown'}")

    return {
        "text": str(chat_response.get("text") or ""),
        "success": bool(chat_response.get("success") or chat_response.get("text")),
        "model": model,
        "used_model": diagnostics.get("used_model") or model,
        "capacity_fallback": bool(diagnostics.get("capacity_fallback")),
        "repo": str(repo),
        "branch": branch,
        "diff_chars": len(diff),
        "truncated": truncated,
        "status_preview": status[:2000],
        "reasoning": chat_response.get("reasoning") or "",
        **response.standard_fields(
            model=model,
            usage=chat_response.get("usage") or {},
            warnings=warnings,
            diagnostics=diagnostics,
            backend=str(chat_response.get("backend") or "diff-review"),
        ),
    }
