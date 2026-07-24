"""Local verify heuristics for durable / change outputs."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import catalog, document_quality

# Recency/session-diary TONE, not vocabulary — "session diary" as a bare noun is dropped
# because durable docs that describe the policy legitimately mention it.
RECENCY_PATTERNS = [
    r"\btoday we\b",
    r"\bjust fixed\b",
    r"\brecently fixed\b",
    r"\bHTTP 400\b",
    r"\bthis session\b",
    r"\b방금 수정\b",
    r"\b오늘 고친\b",
    # "session diary" and "최근 작업" (recent-work) are dropped: they are policy
    # VOCABULARY that durable meta-docs legitimately describe, not recency tone.
]

# Leaf tools this orchestrator knows about — legitimate references in its own docs even
# though they're defined in sibling repos (so not in the local fact pack).
_KNOWN_LEAF_TOOLS = {c["leaf"] for c in catalog.CAPABILITIES} | set(catalog.LATEST_MODELS)

# Standard per-provider CLI/console commands present in every leaf repo (e.g. the MCP
# launcher `grok_codex_mcp`, `*_doctor`, `*_consent`). A meta-doc referencing these is
# not hallucinating — but the local fact pack can't see the sibling repos.
_LEAF_PROVIDERS = (
    "claude_codex",
    "grok_codex",
    "google_antigravity",
    "openai_codex",
)
_LEAF_CMD_SUFFIXES = (
    "mcp", "doctor", "consent", "login", "logout",
    "consent_status", "login_status", "provider_status", "list_models",
)
_KNOWN_LEAF_COMMANDS = {f"{p}_{s}" for p in _LEAF_PROVIDERS for s in _LEAF_CMD_SUFFIXES}

_FINAL_PLACEHOLDER_PATTERNS = (
    re.compile(r"\[(?:todo|tbd|fixme|placeholder)\b[^\]\n]*\]", re.I),
    re.compile(r"\[[^\]\n]{0,100}(?:확인 필요|채워 넣기|추가 필요)[^\]\n]*\]", re.I),
    re.compile(r"(?m)^\s*(?:[-*]\s*)?(?:todo|tbd|fixme)\b[^\n]*", re.I),
)
_REPOSITORY_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\."
    r"(?:py|pyi|ts|tsx|js|jsx|md|toml|json|ya?ml|ini|cfg|sh|service))\b",
    re.I,
)
_TREE_LINE_RE = re.compile(r"^(?P<indent>(?:│   |    )*)(?:├──|└──)\s+(?P<name>[^\s#]+)")
_NEGATED_PATH_CLAIM_RE = re.compile(
    r"(?:not (?:present|included|tracked|available)|missing|absent|"
    r"없(?:습니다|다)|부재|미포함|추적되지 않)",
    re.I,
)


def _placeholder_warnings(body: str) -> List[str]:
    warnings: List[str] = []
    for pattern in _FINAL_PLACEHOLDER_PATTERNS:
        for match in pattern.finditer(body):
            compact = re.sub(r"\s+", " ", match.group(0)).strip()
            warnings.append(f"placeholder_in_final_document:{compact[:120]}")
    return list(dict.fromkeys(warnings))


def _tree_paths(body: str) -> set[str]:
    """Reconstruct file paths from common README tree diagrams."""

    paths: set[str] = set()
    directories: Dict[int, str] = {}
    in_fence = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        match = _TREE_LINE_RE.match(line)
        if not match:
            continue
        depth = len(match.group("indent")) // 4
        name = match.group("name").rstrip()
        for existing in [item for item in directories if item >= depth]:
            directories.pop(existing, None)
        if name.endswith("/"):
            directories[depth] = name.rstrip("/")
            continue
        parents = [directories[index] for index in range(depth) if index in directories]
        paths.add("/".join([*parents, name]))
    return paths


def _repository_path_warnings(body: str, fact_pack: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(fact_pack, dict) or not fact_pack.get("repository_manifest_complete"):
        return []
    existing = set(str(item) for item in fact_pack.get("repository_files") or [])
    if not existing:
        return []
    claimed = {
        match.group(1).removeprefix("./")
        for match in _REPOSITORY_PATH_RE.finditer(body)
    }
    claimed.update(_tree_paths(body))
    warnings: List[str] = []
    lines = body.splitlines()
    for path in sorted(claimed):
        if path in existing or any(char in path for char in "<>{}*$"):
            continue
        mentioning = next((line for line in lines if path in line), "")
        # A filename itself may contain words such as "missing".  Only prose
        # around the path can turn the mention into an explicit absence claim.
        surrounding = mentioning.replace(path, "")
        if _NEGATED_PATH_CLAIM_RE.search(surrounding):
            continue
        warnings.append(f"repository_path_not_found:{path}")
    return warnings


def _is_known_token(token: str, allowed: set) -> bool:
    """A claimed token is legitimate if it's a known tool/command/package, OR a prefix of
    one (a provider like `google_antigravity`), OR a wildcard/partial of one
    (`google_antigravity_` for `google_antigravity_write`). Only genuinely invented names
    (no relation to any real token) are flagged."""
    if token in allowed:
        return True
    return any(known.startswith(token) or token.startswith(known) for known in allowed)


def verify_text(
    text: str,
    *,
    doc_class: str = "durable",
    fact_pack: Optional[Dict[str, Any]] = None,
    user_facing: bool = False,
) -> Dict[str, Any]:
    warnings: List[str] = []
    body = text or ""
    if not body.strip():
        warnings.append("empty_output")
    # Recency / session-diary tone is only forbidden in durable and transform docs;
    # change docs (PR, release notes) are expected to describe recent work.
    if doc_class in {"durable", "transform"}:
        for pat in RECENCY_PATTERNS:
            if re.search(pat, body, re.I):
                warnings.append(f"recency_language:{pat}")
        if re.search(r"[가-힣]", body):
            warnings.extend(
                document_quality.review_natural_korean(body, user_facing=user_facing)
            )
    if doc_class == "durable":
        if re.search(r"\b(git log|diff --stat|HEAD~)\b", body, re.I):
            warnings.append("git_internals_in_durable_doc")
        tools = []
        allowed: set = set()
        if isinstance(fact_pack, dict):
            tools = list(fact_pack.get("mcp_tools_detected") or [])
            # CLI commands, console scripts, and package names are legitimate references,
            # not hallucinated tools — important for docs that describe the tooling itself.
            allowed = (
                set(tools)
                | set(fact_pack.get("cli_commands") or [])
                | set(fact_pack.get("packages") or [])
                | _KNOWN_LEAF_TOOLS
                | _KNOWN_LEAF_COMMANDS
            )
        # flag tool-like tokens not in fact pack when pack known
        if tools:
            claimed = set(
                re.findall(
                    r"\b(?:google|claude_codex|grok_codex|openai_codex|orchestrate)"
                    r"_[a-z0-9_]+\b",
                    body,
                )
            )
            unknown = sorted(t for t in claimed if not _is_known_token(t, allowed))
            for t in unknown:
                warnings.append(f"tool_not_in_fact_pack:{t}")
        if user_facing:
            warnings.extend(_placeholder_warnings(body))
            warnings.extend(_repository_path_warnings(body, fact_pack))
    warnings.extend(_completeness_warnings(body))
    blocking = (
        "empty",
        "recency",
        "korean_style",
        "unclosed_code_fence",
        "truncated",
        "placeholder_in_final_document",
        "repository_path_not_found",
    )
    return {
        "ok": not any(w.startswith(blocking) for w in warnings),
        "warnings": warnings,
        "warning_count": len(warnings),
        "checker_version": document_quality.CHECKER_VERSION,
        "text": (
            "verify ok"
            if not warnings
            else "verify warnings:\n" + "\n".join(f"- {w}" for w in warnings)
        ),
    }


def _completeness_warnings(body: str) -> List[str]:
    """Detect a document that was cut off mid-generation (e.g. the leaf hit max_tokens).

    verify used to pass a truncated README as clean because it only checked tone/tools.
    """
    out: List[str] = []
    text = body.strip()
    # Only meaningful for real documents; skip short snippets to avoid false positives.
    if len(text) < 200:
        return out
    if text.count("```") % 2 == 1:
        out.append("unclosed_code_fence")
    terminal = ".!?:)]}`\"'”』」…"
    # A trailing heading with no body, or a short unpunctuated fragment under it = cut section.
    headings = [m.start() for m in re.finditer(r"(?m)^#{1,6}\s", text)]
    if headings:
        nl = text.find("\n", headings[-1])
        after = text[nl + 1:].strip() if nl != -1 else ""
        if not after or (len(after) < 25 and after[-1] not in terminal):
            out.append("truncated_trailing_section")
    # Ends mid-sentence: last non-empty line isn't closed by punctuation / table / list / fence.
    last = text.splitlines()[-1].strip()
    if last and "|" not in last and not last.startswith(("#", "-", "*", ">", "```")):
        if last[-1] not in terminal:
            out.append("truncated_midsentence")
    return out
