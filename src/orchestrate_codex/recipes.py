"""Built-in supervised recipes (+ optional user recipes from JSON config)."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import policy

_USER_RECIPES_ENV = "ORCHESTRATE_CODEX_RECIPES"

# Optional user args forwarded verbatim to the Antigravity `write` leaf (WRITING_SCHEMA).
WRITE_PASSTHROUGH = (
    "source_text", "source_file", "workspace_root", "context", "profile", "tone",
    "audience", "target_language", "length", "model",
)

DEFAULT_BINDINGS = {
    "chat": "claude_codex_chat",
    "chat_alt": "grok_codex_chat",
    "chat_gemini": "google_antigravity_chat",
    "grounded_search": "google_grounded_search",
    "image": "google_antigravity_generate_image",
    "write_ag": "google_antigravity_write",
    "review_diff": "google_antigravity_review_diff",
    "release": "google_antigravity_release_draft",
    "compare": "google_antigravity_compare_models",
}

# --- Writing domains (one row = one recipe) --------------------------------
# id -> (write_task, doc_class, description, instruction)
# The Antigravity `write` leaf owns task-specific structure, durable safety, and
# (for durable/change) its own project/git grounding. durable=fact pack + verify,
# change=git snapshot, transform/direct=source-driven single pass.
_WRITE_RECIPES = {
    "durable_readme": (
        "readme", "durable",
        "Write/rewrite a whole-project product README from a fact pack, not recent session work.",
        "Write a complete README covering the whole project (purpose, install, usage, tools/commands, "
        "license). Ground every claim in the durable fact pack; forbid recency tone. No invented tools.",
    ),
    "technical_doc": (
        "technical-doc", "durable",
        "Write project technical documentation grounded in durable facts.",
        "Explain the system with clear structure grounded in the fact pack; no recency tone, no invented APIs.",
    ),
    "proposal": (
        "proposal", "durable",
        "Draft a proposal / design doc for the project.",
        "Write a structured proposal (context, goals, approach, risks) grounded in project facts.",
    ),
    "change_pr": (
        "pr-description", "change",
        "Draft a PR description from git changes.",
        "Write PR summary, test plan, and risks grounded only in the git snapshot.",
    ),
    "release_notes": (
        "release-notes", "change",
        "Draft release notes from git history.",
        "Group user-impacting changes, fixes, and breaking changes from the git snapshot. Source-grounded only.",
    ),
    "translate_doc": (
        "translate", "transform",
        "Translate source text (pass source_text/source_file + target_language).",
        "Translate the provided source text faithfully into target_language; preserve meaning and formatting.",
    ),
    "polish_text": (
        "polish", "transform",
        "Polish / tighten existing text (pass source_text).",
        "Polish the provided source text for clarity and flow without changing meaning.",
    ),
    "rewrite_text": (
        "rewrite", "transform",
        "Rewrite existing text to a new tone/audience (pass source_text).",
        "Rewrite the provided source text per the instruction/tone/audience; keep the core message.",
    ),
    "summarize_text": (
        "summarize", "transform",
        "Summarize source text (pass source_text).",
        "Summarize the provided source text concisely without inventing facts.",
    ),
    "announcement": (
        "announcement", "direct",
        "Draft an announcement.",
        "Write clear announcement copy from the instruction.",
    ),
    "blog_post": (
        "blog", "direct",
        "Draft a blog post.",
        "Write an engaging, well-structured blog post from the instruction.",
    ),
    "email_draft": (
        "email", "direct",
        "Draft an email.",
        "Write a clear, courteous email from the instruction; state the point early.",
    ),
    "product_copy": (
        "product-copy", "direct",
        "Draft marketing / product copy.",
        "Write benefit-driven, active-voice product copy from the instruction.",
    ),
}


def _write_recipe(recipe_id: str, task: str, doc_class: str, description: str, instruction: str) -> Dict[str, Any]:
    stages: List[Dict[str, Any]] = []
    if doc_class == "durable":
        stages.append({
            "id": "gather_facts", "kind": "gather", "capability": "local", "tool": None,
            "instruction": (
                "Collect deterministic facts only: version, install commands, MCP tool names, "
                "auth env vars, license. No session diary or today's bugfixes."
            ),
        })
    elif doc_class == "change":
        stages.append({
            "id": "gather_git", "kind": "gather", "capability": "local_git", "tool": None,
            "instruction": "Collect branch, status, commits, and diff stat/summary.",
        })
    stages.append({
        "id": "draft", "kind": "write", "capability": "write", "binding": "write_ag",
        "write_task": task,
        "instruction": instruction + " If falling back to a chat leaf, use only the provided context.",
    })
    if doc_class == "durable":
        stages.append({
            "id": "verify", "kind": "verify", "capability": "local", "tool": None,
            "instruction": "Check tool names/commands against the fact pack; flag session-diary language.",
        })
    return {"id": recipe_id, "doc_class": doc_class, "description": description, "stages": stages}


RECIPES: Dict[str, Dict[str, Any]] = {
    rid: _write_recipe(rid, *spec) for rid, spec in _WRITE_RECIPES.items()
}

# --- Cross-capability / non-writing domains --------------------------------
RECIPES["research_brief"] = {
    "id": "research_brief",
    "doc_class": "transform",
    "description": "Grounded web search, then synthesize a sourced brief via the write leaf.",
    "stages": [
        {
            "id": "search", "kind": "generate", "capability": "grounded_search",
            "binding": "grounded_search",
            "instruction": "Answer with sources via Google grounded search leaf.",
        },
        {
            "id": "synthesize", "kind": "write", "capability": "write", "binding": "write_ag",
            "write_task": "summarize",
            "instruction": "Synthesize a clear, sourced brief from the search results. Invent no sources.",
        },
    ],
}
# Back-compat alias for the previous recipe id.
RECIPES["research_then_write"] = {**deepcopy(RECIPES["research_brief"]), "id": "research_then_write"}

RECIPES["review_diff"] = {
    "id": "review_diff",
    "doc_class": "change",
    "description": "Review the working/staged git diff with the Antigravity diff-review leaf.",
    "stages": [
        {
            "id": "review", "kind": "generate", "capability": "review_diff", "binding": "review_diff",
            "instruction": "Review the diff for correctness, risks, and test gaps.",
        },
    ],
}

RECIPES["release_draft"] = {
    "id": "release_draft",
    "doc_class": "change",
    "description": "Draft a release from a git range with the Antigravity release leaf.",
    "stages": [
        {
            "id": "release", "kind": "generate", "capability": "release", "binding": "release",
            "instruction": "Draft release notes for the git range (pass version/tag/base_ref as needed).",
        },
    ],
}

RECIPES["generate_image"] = {
    "id": "generate_image",
    "doc_class": "direct",
    "description": "Single-shot image generation via the Antigravity image leaf.",
    "stages": [
        {
            "id": "image", "kind": "generate", "capability": "image", "binding": "image",
            "instruction": "Generate an image from the prompt (aspect_ratio/image_size optional).",
        },
    ],
}

RECIPES["compare_models"] = {
    "id": "compare_models",
    "doc_class": "direct",
    "description": "Ask the same prompt across 2-3 models and compare (pass models).",
    "stages": [
        {
            "id": "compare", "kind": "generate", "capability": "compare", "binding": "compare",
            "instruction": "Run the prompt across the given models and compare outputs.",
        },
    ],
}

RECIPES["deep_readme"] = {
    "id": "deep_readme",
    "doc_class": "durable",
    "description": "Multi-LLM: Claude analyzes architecture, Grok analyzes usage/API, then Gemini writes the README.",
    "stages": [
        {
            "id": "gather_facts", "kind": "gather", "capability": "local", "tool": None,
            "instruction": "Collect deterministic facts (version, tools, license) as a guardrail.",
        },
        {
            "id": "gather_code", "kind": "gather", "capability": "local_code", "tool": None,
            "instruction": "Collect source text for the LLM analysts to read.",
        },
        {
            "id": "investigate_arch", "kind": "generate", "capability": "chat", "binding": "chat",
            "instruction": (
                "You are a senior code analyst. From the CODE CONTEXT, explain this project's "
                "architecture: modules, responsibilities, data flow, key abstractions. "
                "Ground every claim in the code shown. Be concrete; no filler."
            ),
        },
        {
            "id": "investigate_usage", "kind": "generate", "capability": "chat", "binding": "chat_alt",
            "instruction": (
                "You are a senior code analyst. From the CODE CONTEXT, explain how a user installs "
                "and uses this project: install commands, entry points, MCP tools and what each does, "
                "auth/consent flow, configuration. Ground every claim in the code shown."
            ),
        },
        {
            "id": "draft", "kind": "write", "capability": "write", "binding": "write_ag",
            "write_task": "readme",
            "instruction": (
                "Synthesize a complete README from the analysts' FINDINGS and the durable fact pack. "
                "Include overview, features, install, quick start, MCP tool reference, auth/consent, "
                "architecture, license. Ground claims in the findings/facts; invent nothing."
            ),
        },
        {
            "id": "verify", "kind": "verify", "capability": "local", "tool": None,
            "instruction": "Check tool names/commands against the fact pack; flag session-diary language.",
        },
    ],
}

RECIPES["direct_chat"] = {
    "id": "direct_chat",
    "doc_class": "direct",
    "description": "Single leaf chat (orchestration optional).",
    "stages": [
        {
            "id": "chat", "kind": "generate", "capability": "chat", "binding": "chat",
            "instruction": "One-shot chat with the bound leaf.",
        }
    ],
}


def _user_recipe_file() -> Path:
    override = os.environ.get(_USER_RECIPES_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".orchestrate_codex" / "recipes.json"


def _build_user_recipe(recipe_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("recipe spec must be an object")
    doc_class = str(spec.get("doc_class") or "direct")
    policy.get_policy(doc_class)  # validate doc_class or raise
    description = str(spec.get("description") or recipe_id)
    if isinstance(spec.get("stages"), list) and spec["stages"]:
        return {"id": recipe_id, "doc_class": doc_class, "description": description, "stages": spec["stages"]}
    # Shorthand: a write-task recipe generated the same way as built-ins.
    task = str(spec.get("write_task") or "auto")
    instruction = str(spec.get("instruction") or description)
    return _write_recipe(recipe_id, task, doc_class, description, instruction)


def load_user_recipes() -> Dict[str, Dict[str, Any]]:
    """Load user-defined recipes from JSON (env override or ~/.orchestrate_codex/recipes.json)."""
    try:
        raw = json.loads(_user_recipe_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for rid, spec in raw.items():
        try:
            out[str(rid)] = _build_user_recipe(str(rid), spec)
        except (ValueError, KeyError, TypeError):
            continue  # skip malformed entries rather than break the whole registry
    return out


def all_recipes() -> Dict[str, Dict[str, Any]]:
    """Built-in recipes merged with user recipes (user entries override built-ins by id)."""
    merged = dict(RECIPES)
    merged.update(load_user_recipes())
    return merged


def list_recipes() -> List[Dict[str, Any]]:
    out = []
    for recipe in all_recipes().values():
        out.append(
            {
                "id": recipe["id"],
                "doc_class": recipe["doc_class"],
                "description": recipe["description"],
                "stage_count": len(recipe["stages"]),
            }
        )
    return out


def get_recipe(recipe_id: str) -> Dict[str, Any]:
    key = (recipe_id or "").strip()
    registry = all_recipes()
    if key not in registry:
        raise ValueError(f"unknown recipe: {recipe_id}. Known: {sorted(registry)}")
    return deepcopy(registry[key])


def explain_recipe(recipe_id: str) -> Dict[str, Any]:
    recipe = get_recipe(recipe_id)
    pol = policy.get_policy(recipe["doc_class"])
    return {"recipe": recipe, "context_policy": pol, "default_bindings": DEFAULT_BINDINGS}


def plan_recipe(
    recipe_id: str,
    *,
    args: Optional[Dict[str, Any]] = None,
    bindings: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    recipe = get_recipe(recipe_id)
    pol = policy.get_policy(recipe["doc_class"])
    bind = dict(DEFAULT_BINDINGS)
    if bindings:
        bind.update({str(k): str(v) for k, v in bindings.items() if str(v).strip()})
    args = args or {}
    steps = []
    for i, stage in enumerate(recipe["stages"]):
        tool = stage.get("tool")
        binding = stage.get("binding")
        if tool is None and binding:
            tool = bind.get(str(binding))
        steps.append(
            {
                "index": i,
                "id": stage["id"],
                "kind": stage["kind"],
                "capability": stage.get("capability"),
                "tool": tool,
                "instruction": stage.get("instruction"),
                "suggested_arguments": _suggest_args(stage, args, pol),
                "status": "pending",
            }
        )
    return {
        "recipe_id": recipe["id"],
        "doc_class": recipe["doc_class"],
        "context_policy": pol,
        "bindings": bind,
        "mode": "supervised",
        "note": (
            "Execute each step's tool in order via Codex MCP. "
            "This orchestrator does not call leaf HTTP APIs itself (v0.2 supervised)."
        ),
        "steps": steps,
        "user_args": args,
    }


def _suggest_args(stage: Dict[str, Any], args: Dict[str, Any], pol: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(args.get("prompt") or args.get("instruction") or "").strip()
    base: Dict[str, Any] = {}
    if stage.get("capability") == "write":
        base["task"] = str(stage.get("write_task") or "auto")
        base["instruction"] = prompt or str(stage.get("instruction") or "")
        base["project_root"] = str(args.get("project_root") or ".")
        base["output_mode"] = str(args.get("output_mode") or "final")
        if pol.get("git") == "on":
            base["project_context"] = "auto"
        for key in WRITE_PASSTHROUGH:
            if args.get(key) not in (None, ""):
                base[key] = args[key]
        return base
    if stage.get("capability") == "image":
        base["prompt"] = prompt or str(stage.get("instruction") or "")
        for key in ("model", "aspect_ratio", "image_size"):
            if args.get(key) not in (None, ""):
                base[key] = args[key]
        return base
    if stage.get("capability") == "compare":
        base["prompt"] = prompt or str(stage.get("instruction") or "")
        if args.get("models") not in (None, "", []):
            base["models"] = args["models"]  # COMPARE_SCHEMA has no singular `model`
        return base
    if stage.get("capability") == "review_diff":
        base["cwd"] = str(args.get("project_root") or ".")
        base["instruction"] = prompt or str(stage.get("instruction") or "")
        for key in ("focus", "base", "ref", "staged", "paths", "model"):
            if args.get(key) not in (None, ""):
                base[key] = args[key]
        return base
    if stage.get("capability") == "release":
        base["repo"] = str(args.get("project_root") or args.get("repo") or ".")
        for key in ("base_ref", "head_ref", "title", "version", "tag", "polish", "model"):
            if args.get(key) not in (None, ""):
                base[key] = args[key]
        return base
    if stage.get("capability") in {"chat", "grounded_search"} and prompt:
        if stage.get("capability") == "grounded_search":
            base["query"] = prompt
        else:
            base["prompt"] = (
                f"[orchestrate stage={stage.get('id')} doc_class={pol.get('doc_class')}]\n"
                f"{stage.get('instruction')}\n\nUser request:\n{prompt}"
            )
        if args.get("model"):
            base["model"] = args["model"]
        if args.get("system"):
            base["system"] = args["system"]
    if stage.get("kind") == "gather":
        base["hint"] = stage.get("instruction")
        if pol.get("git") == "on":
            base["collect"] = ["status", "log", "diff_stat"]
        if pol.get("facts") == "required":
            base["collect"] = ["version", "tools", "install", "auth", "license"]
    return base
