"""Writing copilot support backed by Antigravity chat.

This module absorbs the useful routing and prompt-building surface from the
old Gemini Writing Copilot while using this plugin's Antigravity chat path.

Doc-class split (aligned with orchestrate-codex, no hard dependency):
- **durable** (readme, technical-doc): 1-shot leaf — fact pack on, git diary off.
- **change** (pr-description, release-notes): git context allowed via auto.
- **transform** (polish, translate, …): source-first, git off by default.

Multi-step outline→draft→verify still belongs in orchestrate-codex
``durable_readme``; this leaf enforces durable *safety* when called directly.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List

from . import chat, doc_facts, model_prefs, response as response_schema, security

DEFAULT_MODEL = "gemini-3.1-pro-preview"
TASKS = {
    "auto",
    "draft",
    "rewrite",
    "polish",
    "summarize",
    "translate",
    "outline",
    "custom",
    "email",
    "announcement",
    "blog",
    "pr-description",
    "release-notes",
    "readme",
    "proposal",
    "product-copy",
    "technical-doc",
}

PROFILE_TEXT = {
    "chanwoo-ko": (
        "Write in natural, modern Korean suitable for tech work and public "
        "communication. Avoid translationese and overly formal academic phrasing. "
        "Keep sentences clear, human, and concise."
    ),
    "professional-ko": (
        "Write in formal, polite Korean appropriate for business email, official "
        "announcements, and reports. Use precise terminology and a respectful tone."
    ),
    "github-release": (
        "Write concise, actionable release notes. Group bullets by user-impacting "
        "changes, fixes, and breaking changes. Keep claims source-grounded."
    ),
    "email-polite": (
        "Write clear, concise, courteous email. State the main point early and end "
        "with a useful next step."
    ),
    "product-copy-clear": (
        "Write benefit-driven product copy. Use active voice and prioritize clarity "
        "over cleverness."
    ),
}

DEFAULT_PROFILES = {
    "email": ["email-polite"],
    "announcement": ["chanwoo-ko"],
    "blog": ["chanwoo-ko"],
    "product-copy": ["product-copy-clear"],
    "proposal": ["professional-ko"],
    "pr-description": ["github-release"],
    "release-notes": ["github-release"],
    "readme": ["professional-ko"],
    "technical-doc": ["professional-ko"],
    "polish": ["chanwoo-ko"],
    "rewrite": ["chanwoo-ko"],
    "translate": ["chanwoo-ko"],
    "summarize": ["professional-ko"],
}

TASK_GUIDANCE = {
    "draft": "Create a complete first pass from the provided inputs.",
    "rewrite": "Rewrite the source while preserving its meaning and facts.",
    "polish": "Improve clarity, grammar, tone, and flow without inventing facts.",
    "summarize": "Condense the source to its important points and omit minor details.",
    "translate": "Translate accurately and naturally into the target language.",
    "outline": "Create a structured outline with logical section hierarchy.",
    "email": "Draft a clear email with purpose, context, and next step.",
    "announcement": "Lead with the important news, then explain context and impact.",
    "blog": "Write an engaging, readable post with clear headings.",
    "pr-description": "Summarize the code changes, motivation, and validation.",
    "release-notes": "Group changes into concise release-note sections.",
    "readme": (
        "Write practical product documentation with setup, usage, and configuration. "
        "Use only the durable fact pack and provided source. Never describe session "
        "work, 'today we fixed', git logs, or debug notes as product features. "
        "Do not invent MCP tools, env vars, or install commands."
    ),
    "proposal": "Explain the problem, solution, benefits, and next steps.",
    "product-copy": "Highlight user benefit in active, direct language.",
    "technical-doc": (
        "Explain technical concepts with clear structure. Ground claims in the "
        "durable fact pack and source. No session diary or recent-commit tone."
    ),
    "custom": "Follow the user's explicit instruction exactly.",
}

# Leaf durable class — mirrors orchestrate-codex durable (not multi-step).
DURABLE_TASKS = frozenset({"readme", "technical-doc"})
CHANGE_TASKS = frozenset({"pr-description", "release-notes"})

RECENCY_RE = re.compile(
    r"(?i)\b(today we|just fixed|recently fixed|this session|session diary|"
    r"HTTP 400|방금 수정|오늘 고친|최근 작업)\b"
)

SECRET_RE = re.compile(
    r"(?i)(authorization|bearer|token|refresh_token|access_token|client_secret|cookie|api[_-]?key)"
    r"([:=]\s*)?[^\s]+"
)


def redact(value: Any) -> str:
    return SECRET_RE.sub(lambda match: f"{match.group(1)}=REDACTED", str(value or ""))


def _run_git(root: Path, args: List[str], *, timeout_sec: int = 20) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(root),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _git_root(path: Path) -> Path | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(path),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()


def infer_task(arguments: Dict[str, Any], source_text: str = "") -> str:
    requested = str(arguments.get("task") or "auto").strip().lower()
    if requested and requested != "auto":
        return requested if requested in TASKS else "custom"
    haystack = " ".join(
        str(arguments.get(key) or "") for key in ("instruction", "context", "audience")
    ).lower()
    if any(word in haystack for word in ("release note", "release-notes", "릴리즈")):
        return "release-notes"
    if any(word in haystack for word in ("pr description", "pull request", "reviewer")):
        return "pr-description"
    if any(word in haystack for word in ("translate", "번역")):
        return "translate"
    if any(word in haystack for word in ("summarize", "summary", "요약")):
        return "summarize"
    if any(word in haystack for word in ("email", "메일")):
        return "email"
    if source_text.strip():
        return "polish"
    return "draft"


def _profile_names(value: Any, task: str) -> List[str]:
    if isinstance(value, str) and value.strip():
        raw = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        raw = [str(item).strip() for item in value]
    else:
        raw = []
    names = [item for item in raw if item]
    return names or list(DEFAULT_PROFILES.get(task, ["chanwoo-ko"]))


def profile_text(names: List[str]) -> str:
    sections: List[str] = []
    for name in names:
        text = PROFILE_TEXT.get(name)
        if text:
            sections.append(f"Profile {name}: {text}")
        else:
            sections.append(f"Profile {name}: Use the named style if it is clear from context.")
    return "\n".join(sections)


def read_source(arguments: Dict[str, Any]) -> str:
    chunks: List[str] = []
    text = str(arguments.get("source_text") or "")
    if text:
        chunks.append(text)
    source_file = str(arguments.get("source_file") or "").strip()
    if source_file:
        workspace_root = str(arguments.get("workspace_root") or "").strip() or None
        path = security.resolve_allowed_path(
            source_file,
            purpose="source_file",
            directory=False,
            explicit_root=workspace_root,
        )
        max_bytes = security.bounded_int_env(
            "GOOGLE_ANTIGRAVITY_MAX_SOURCE_BYTES",
            1024 * 1024,
            minimum=1,
            maximum=10 * 1024 * 1024,
        )
        if path.stat().st_size > max_bytes:
            raise ValueError(f"source_file exceeds the {max_bytes}-byte size limit: {path}")
        chunks.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(chunk for chunk in chunks if chunk)


def is_durable_task(task: str) -> bool:
    return task in DURABLE_TASKS


def resolve_project_context_mode(arguments: Dict[str, Any], task: str) -> str:
    """Resolve git context mode with durable safety.

    Durable tasks never pull git diary (even if caller passes auto/git-*).
    Change tasks default auto → git-diff / git-summary.
    """
    requested = str(arguments.get("project_context") or "off").strip().lower()
    if is_durable_task(task):
        # Explicit override only if force_git_context=true (escape hatch for rare cases).
        if arguments.get("force_git_context") is True and requested in {"git-summary", "git-diff"}:
            return requested
        return "off"
    if requested == "auto":
        if task in CHANGE_TASKS:
            return "git-diff" if task == "pr-description" else "git-summary"
        return "off"
    if requested in {"git-summary", "git-diff", "off"}:
        return requested
    return "off"


def collect_project_context(arguments: Dict[str, Any], task: str) -> str:
    requested = resolve_project_context_mode(arguments, task)
    if requested not in {"git-summary", "git-diff"}:
        return ""
    requested_root = security.resolve_allowed_path(
        str(arguments.get("project_root") or "."),
        purpose="project_root",
        directory=True,
        explicit_root=str(arguments.get("project_root") or "."),
    )
    root = _git_root(requested_root)
    if root is None:
        return ""
    max_chars = max(1000, min(int(arguments.get("max_project_context_chars") or 8000), 50000))
    sections = [
        f"Repository: {root}",
        f"Branch: {_run_git(root, ['branch', '--show-current']) or '[detached]'}",
        f"Head: {_run_git(root, ['rev-parse', '--short', 'HEAD'])}",
        "Status:\n" + (_run_git(root, ["status", "--short"]) or "clean"),
        "Recent commits:\n" + (_run_git(root, ["log", "--oneline", "-12"]) or "[none]"),
        "Diff stat:\n" + (_run_git(root, ["diff", "--stat"]) or "[none]"),
    ]
    if requested == "git-diff":
        sections.append("Diff:\n" + (_run_git(root, ["diff", "--", "."], timeout_sec=30) or "[none]"))
    context = "\n\n".join(sections)
    return context[:max_chars]


def collect_durable_context(arguments: Dict[str, Any], task: str) -> Dict[str, Any]:
    """Fact pack for durable leaf writes. Disabled when fact_pack=false."""
    if not is_durable_task(task):
        return {"used": False, "text": ""}
    if arguments.get("fact_pack") is False:
        return {"used": False, "text": "", "skipped": True}
    root = str(arguments.get("project_root") or arguments.get("workspace_root") or ".").strip() or "."
    try:
        facts = doc_facts.collect_durable_facts(root)
    except Exception as exc:  # noqa: BLE001
        return {"used": False, "text": "", "error": str(exc)}
    return {"used": True, "text": str(facts.get("text") or ""), "facts": facts}


def build_prompt(arguments: Dict[str, Any]) -> Dict[str, Any]:
    source_text = read_source(arguments)
    task = infer_task(arguments, source_text)
    profiles = _profile_names(arguments.get("profile"), task)
    durable = is_durable_task(task)
    project_context = collect_project_context(arguments, task)
    durable_ctx = collect_durable_context(arguments, task)
    instruction = str(arguments.get("instruction") or "").strip()
    context = str(arguments.get("context") or "").strip()
    tone = str(arguments.get("tone") or "").strip()
    audience = str(arguments.get("audience") or "").strip()
    target_language = str(arguments.get("target_language") or "").strip()
    length = str(arguments.get("length") or "").strip()
    output_mode = str(arguments.get("output_mode") or "final").strip()
    if not any(
        [
            instruction,
            source_text,
            context,
            project_context,
            durable_ctx.get("text"),
        ]
    ):
        raise ValueError(
            "instruction, source_text, source_file, context, project_context, or durable fact pack is required"
        )

    system = (
        "You are the writing arm of Google Antigravity Codex. Return only the "
        "requested writing unless the output mode asks for notes. Preserve source "
        "facts, names, versions, dates, commands, and numbers. Do not invent tests, "
        "links, issue IDs, or compatibility claims. If evidence is missing, use a "
        "clear placeholder instead of guessing."
    )
    if durable:
        system += (
            " This is a durable product document: use the fact pack and provided source only. "
            "Never turn session work, recent commits, or debug notes into product claims. "
            "For multi-step README pipelines (outline→draft→verify), prefer orchestrate-codex "
            "durable_readme; this path is a single durable-safe pass."
        )
    parts = [
        f"Task: {task}",
        f"Doc class: {'durable' if durable else ('change' if task in CHANGE_TASKS else 'transform')}",
        f"Task guidance: {TASK_GUIDANCE.get(task, TASK_GUIDANCE['custom'])}",
        f"Output mode: {output_mode}",
        profile_text(profiles),
    ]
    if instruction:
        parts.append(f"Instruction:\n{instruction}")
    if tone:
        parts.append(f"Tone:\n{tone}")
    if audience:
        parts.append(f"Audience:\n{audience}")
    if target_language:
        parts.append(f"Target language:\n{target_language}")
    if length:
        parts.append(f"Length:\n{length}")
    if context:
        # For durable, still allow explicit context but label it carefully
        label = "Additional context (do not treat as session diary if durable)" if durable else "Additional context"
        parts.append(f"{label}:\n{context}")
    if durable_ctx.get("text"):
        parts.append(f"Durable fact pack:\n{redact(durable_ctx['text'])}")
    if project_context:
        parts.append(f"Project context:\n{redact(project_context)}")
    if source_text:
        parts.append(f"Source text:\n{redact(source_text)}")
    return {
        "task": task,
        "profiles": profiles,
        "system": system,
        "prompt": "\n\n".join(parts),
        "project_context_used": bool(project_context),
        "durable": durable,
        "fact_pack_used": bool(durable_ctx.get("used")),
        "doc_class": "durable" if durable else ("change" if task in CHANGE_TASKS else "transform"),
    }


def review_text(text: str, *, durable: bool = False) -> List[str]:
    warnings: List[str] = []
    if "[TODO" in text or "[todo" in text:
        warnings.append("output_contains_todo_placeholder")
    if "As an AI" in text:
        warnings.append("output_contains_model_meta_commentary")
    if durable and RECENCY_RE.search(text or ""):
        warnings.append("durable_output_contains_recency_language")
    if durable and re.search(r"\b(git log|diff --stat|HEAD~)\b", text or "", re.I):
        warnings.append("durable_output_contains_git_internals")
    return warnings


def run_writing(arguments: Dict[str, Any]) -> Dict[str, Any]:
    built = build_prompt(arguments)
    model = model_prefs.resolve_model(
        explicit=str(arguments.get("model") or os.getenv("GOOGLE_ANTIGRAVITY_WRITING_MODEL") or ""),
        task="writing",
        fallback=DEFAULT_MODEL,
    ) or DEFAULT_MODEL
    chat_response = chat.run_chat(
        {
            "prompt": built["prompt"],
            "system": built["system"],
            "model": model,
            "task": "writing",
            "temperature": arguments.get("temperature", 0.35),
            "max_tokens": int(arguments.get("max_tokens") or 4096),
            "timeout_sec": arguments.get("timeout_sec") or 180,
            "retry_count": arguments.get("retry_count", 1),
            "retry_sleep_cap_sec": arguments.get("retry_sleep_cap_sec", 8),
        }
    )
    text = str(chat_response.get("text") or "").strip()
    warnings = review_text(text, durable=bool(built.get("durable")))
    diagnostics = dict(chat_response.get("diagnostics") or {})
    diagnostics.update(
        {
            "doc_class": built.get("doc_class"),
            "durable": built.get("durable"),
            "fact_pack_used": built.get("fact_pack_used"),
            "project_context_used": built.get("project_context_used"),
        }
    )
    return {
        "text": text,
        "task": built["task"],
        "profiles": built["profiles"],
        "model": model,
        "doc_class": built.get("doc_class"),
        "project_context_used": built["project_context_used"],
        "fact_pack_used": built.get("fact_pack_used"),
        "quality_warnings": warnings,
        "usage": chat_response.get("usage", {}),
        **response_schema.standard_fields(
            model=model,
            usage=chat_response.get("usage", {}),
            warnings=warnings,
            diagnostics=diagnostics,
            backend=str(chat_response.get("backend") or "agy-session"),
        ),
    }
