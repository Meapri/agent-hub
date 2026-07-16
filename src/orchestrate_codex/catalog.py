"""Routing catalog: what each leaf/model is good for, and the current best model id.

This is the menu the host model (GPT in the Codex app, Claude in Claude Code) reads
to decide how to allocate work — which model gets which sub-task, and what to just do
itself. The orchestrator does NOT make that judgment; it supplies the menu, the latest
model ids, and guardrails.

Latest model ids were confirmed empirically (a tiny ping call), because the leaves'
own list_models catalogs are stale. Update via `orchestrate_probe_models`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Confirmed-working latest model id per leaf tool (2026-07). Leaf catalogs omit these
# but pass them through to the provider. Keep in sync via orchestrate_probe_models.
LATEST_MODELS: Dict[str, str] = {
    "claude_codex_chat": "claude-opus-4-8",
    "grok_codex_chat": "grok-4.5",
    "google_antigravity_chat": "gemini-3.1-pro-high",
    "google_antigravity_write": "gemini-3.1-pro-high",
    # grounded_search / image keep their own provider defaults.
}

# Candidate ids the probe tool will test to refresh LATEST_MODELS live.
PROBE_CANDIDATES: Dict[str, List[str]] = {
    "claude_codex_chat": ["claude-opus-4-8", "claude-sonnet-5", "claude-opus-4-6"],
    "grok_codex_chat": ["grok-4.5", "grok-4", "grok-4-fast-reasoning"],
    "google_antigravity_chat": ["gemini-3.1-pro-high", "gemini-3.5-flash-high"],
}

# Strength guide (author's defaults). Each entry: how a Codex host should route to it.
CAPABILITIES: List[Dict[str, Any]] = [
    {
        "capability": "chat",
        "role": "reasoning-claude",
        "leaf": "claude_codex_chat",
        "good_for": "Deep, careful reasoning; code architecture analysis; long-context reading; "
                    "nuanced review; rigorous logic. Prefer when correctness/depth matters.",
        "avoid_for": "Throwaway quick lookups (slower/pricier than needed).",
    },
    {
        "capability": "chat",
        "role": "reasoning-grok",
        "leaf": "grok_codex_chat",
        "good_for": "Fast, broad reasoning; a diverse second opinion; brainstorming breadth; "
                    "quick analysis. Good as an independent cross-check against Claude.",
        "avoid_for": "The single source of truth for high-stakes correctness.",
    },
    {
        "capability": "chat",
        "role": "reasoning-gemini",
        "leaf": "google_antigravity_chat",
        "good_for": "General Gemini reasoning; can enable inline grounding. A third distinct lens.",
        "avoid_for": "Structured document authoring — use the write leaf instead.",
    },
    {
        "capability": "write",
        "role": "author-gemini",
        "leaf": "google_antigravity_write",
        "good_for": "Final structured document authoring (readme, technical-doc, pr-description, "
                    "release-notes, translate, summarize). Self-grounds durable facts; enforces "
                    "durable safety. Prefer for the SYNTHESIS/writing step.",
        "avoid_for": "Open-ended analysis — feed it findings, don't ask it to investigate.",
        "note": "Takes {task, instruction, source_text, project_root}, NOT a raw prompt.",
    },
    {
        "capability": "grounded_search",
        "role": "web-research",
        "leaf": "google_grounded_search",
        "good_for": "Current/external facts with sources (news, library docs, versions). "
                    "Use when the answer depends on info outside the repo.",
        "avoid_for": "Reasoning over code already in the workspace.",
    },
    {
        "capability": "image",
        "role": "image-gen",
        "leaf": "google_antigravity_generate_image",
        "good_for": "Generating images/diagrams-as-art from a prompt.",
        "avoid_for": "Anything text.",
    },
    {
        "capability": "review_diff",
        "role": "code-review",
        "leaf": "google_antigravity_review_diff",
        "good_for": "Reviewing a git diff for correctness/risks/test gaps.",
        "avoid_for": "Non-diff prose.",
    },
    {
        "capability": "compare",
        "role": "model-compare",
        "leaf": "google_antigravity_compare_models",
        "good_for": "Running one prompt across 2-3 models to compare answers.",
        "avoid_for": "Single-answer tasks.",
    },
]

DO_DIRECTLY = (
    "As the host model (GPT in the Codex app, Claude in Claude Code) you should do these "
    "YOURSELF rather than delegate: planning and decomposition, file reads/edits, running "
    "commands/tests, simple transformations, and anything not needing a specialist model. "
    "Delegate a sub-task to a leaf only when another model's strength (depth, breadth, a "
    "distinct lens, web grounding, image, or structured authoring) genuinely improves the "
    "result or saves your own context."
)


def latest_for(leaf: Optional[str]) -> Optional[str]:
    return LATEST_MODELS.get(str(leaf or ""))


def capabilities(available_tools: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    present = {str(t) for t in (available_tools or [])}
    out = []
    for cap in CAPABILITIES:
        if present and cap["leaf"] not in present:
            continue
        entry = dict(cap)
        entry["latest_model"] = latest_for(cap["leaf"])
        out.append(entry)
    return out
