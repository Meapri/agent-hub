"""Supervised run state machine: plan → (local auto) → leaf steps → continue."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from hashlib import sha256
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from agent_hub import capabilities as agent_capabilities
from agent_hub import provider_registry
from agent_hub.core import handoff as handoff_state
from agent_hub.core import limits

from . import (
    __version__,
    catalog,
    errors,
    events,
    gather,
    policy,
    recipes,
    results,
    store,
    verify,
)

# capability → ordered fallback tools (first is primary unless bindings override)
FALLBACK_CHAINS: Dict[str, List[str]] = {
    "chat": list(provider_registry.chat_tools(planner_only=True)),
    "grounded_search": ["google_grounded_search"],
    "image": ["google_antigravity_generate_image"],
    # A structured writing stage prefers Antigravity's `write` leaf (which self-grounds
    # a durable fact pack for readme/technical-doc), then degrades to a generic chat leaf.
    "write": [
        "google_antigravity_write",
        *provider_registry.chat_tools(planner_only=True),
    ],
    # Antigravity-specific structured tools — no cross-provider fallback.
    "review_diff": ["google_antigravity_review_diff"],
    "release": ["google_antigravity_release_draft"],
    "compare": ["google_antigravity_compare_models"],
}

# Max concurrently-retained runs; oldest evicted first (in-process store only).
MAX_RUNS = 200
RUN_STATE_SCHEMA_VERSION = 2
RUN_LEASE_SECONDS = 60.0
FIXED_ACTION_SCHEMA = "fixed_action_v1"

# Default output budget for a structured write (README/PR/etc.); leaf defaults truncate.
DEFAULT_WRITE_MAX_TOKENS = limits.MAX_OUTPUT_TOKENS
# Keep enough headroom for long synthesis and for providers whose reasoning tokens share
# the output budget (notably Gemini high-thinking models).
DEFAULT_CHAT_MAX_TOKENS = limits.MAX_OUTPUT_TOKENS

_RUNS: Dict[str, Dict[str, Any]] = {}


@dataclass(frozen=True)
class FixedActionClaim:
    store_claim: store.RunClaim
    action: Dict[str, Any]
    handoff_drift_policy: str

    def public(self) -> Dict[str, Any]:
        return {
            "schema": "fixed_action_claim_v1",
            "run_id": self.store_claim.run_id,
            "action_id": self.action["action_id"],
            "claim_token": self.store_claim.token,
            "base_revision": self.store_claim.base_revision,
            "expires_at": self.store_claim.expires_at,
            "action": copy.deepcopy(self.action),
        }


def _load_handoff_snapshot(user_args: Dict[str, Any]) -> Dict[str, Any]:
    if "_handoff_snapshot" in user_args:
        raise ValueError("_handoff_snapshot is reserved for persisted internal state")
    return handoff_state.load_handoff(
        str(user_args.get("project_root") or "."),
        mode=str(user_args.get("handoff_mode") or "auto"),
        search=str(user_args.get("handoff_search") or "nearest"),
        file=str(user_args.get("handoff_file") or ""),
        max_chars=int(user_args.get("max_handoff_chars") or handoff_state.DEFAULT_MAX_CHARS),
    )


def _handoff_context(user_args: Dict[str, Any]) -> str:
    return handoff_state.render_context(user_args.get("_handoff_snapshot"))


def _tool_family(tool: str) -> str:
    """Group a leaf tool by the argument shape it expects."""
    t = tool or ""
    if t.endswith("_write"):
        return "write"
    if t.endswith("grounded_search") or t.endswith("_search"):
        return "search"
    if t.endswith("_generate_image") or t.endswith("_image"):
        return "image"
    if t.endswith("_review_diff"):
        return "review_diff"
    if t.endswith("_release_draft") or t.endswith("_release_snapshot"):
        return "release"
    if t.endswith("_compare_models"):
        return "compare"
    return "chat"


def _bindings(overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    bind = dict(recipes.DEFAULT_BINDINGS)
    if overrides:
        bind.update({str(k): str(v) for k, v in overrides.items() if str(v).strip()})
    return bind


def _fallback_tools(capability: str, bindings: Dict[str, str]) -> List[str]:
    chain = list(FALLBACK_CHAINS.get(capability) or [])
    primary = bindings.get(capability)
    if primary:
        chain = [primary] + [t for t in chain if t != primary]
    # also honor chat_alt / chat_gemini as ordered extras for chat
    if capability == "chat":
        for key in ("chat_alt", "chat_gemini"):
            t = bindings.get(key)
            if t and t not in chain:
                chain.append(t)
    return chain


def _stage_model(step: Dict[str, Any], user_args: Dict[str, Any]) -> Optional[str]:
    """Resolve the model for a stage: per-stage `models` map > stage default > global `model`.

    `user_args["models"]` is a dict keyed by stage id, so a multi-LLM recipe can send a
    different model to each leaf (e.g. Claude=opus, Grok=grok-4, writer=gemini-pro).
    """
    models = user_args.get("models")
    if isinstance(models, dict):
        picked = models.get(step.get("id"))
        if picked:
            return str(picked)
    if step.get("model"):
        return str(step["model"])
    if user_args.get("model"):
        return str(user_args["model"])
    return None


def _enrich_chat_args(
    step: Dict[str, Any],
    *,
    user_args: Dict[str, Any],
    pol: Dict[str, Any],
    artifacts: Dict[str, Any],
) -> Dict[str, Any]:
    # Build fresh — never inherit a prior tool's argument shape (e.g. a write→chat fallback).
    args: Dict[str, Any] = {}
    prompt = str(user_args.get("prompt") or user_args.get("instruction") or "").strip()
    parts = [
        f"[orchestrate stage={step.get('id')} doc_class={pol.get('doc_class')}]",
        str(step.get("instruction") or ""),
    ]
    if step.get("revise_notes"):
        parts.append(
            "REVISE — fix these local-check failures: "
            + "; ".join(str(n) for n in step["revise_notes"])
        )
    if artifacts.get("facts_text"):
        parts.append("FACT PACK:\n" + str(artifacts["facts_text"]))
    if artifacts.get("git_text") and pol.get("git") == "on":
        parts.append("GIT SNAPSHOT:\n" + str(artifacts["git_text"]))
    if artifacts.get("outline"):
        parts.append("OUTLINE:\n" + str(artifacts["outline"]))
    if artifacts.get("search"):
        parts.append("SEARCH RESULTS:\n" + str(artifacts["search"]))
    if artifacts.get("code_text"):
        parts.append(str(artifacts["code_text"]))
    if artifacts.get("findings_text"):
        parts.append("PRIOR FINDINGS FROM OTHER ANALYSTS:\n" + str(artifacts["findings_text"]))
    if user_args.get("source_text"):
        parts.append("SOURCE TEXT:\n" + str(user_args["source_text"]))
    if user_args.get("target_language"):
        parts.append("TARGET LANGUAGE: " + str(user_args["target_language"]))
    operational_handoff = _handoff_context(user_args)
    if operational_handoff and step.get("capability") != "grounded_search":
        parts.append(operational_handoff)
    if prompt:
        parts.append("User request:\n" + prompt)
    if step.get("capability") == "grounded_search":
        args["query"] = (
            prompt or args.get("query") or "status"
        )  # GROUNDING_SCHEMA has no max_tokens
    else:
        args["prompt"] = "\n\n".join(p for p in parts if p)
        # Always pass the conductor budget so every provider has the same generous
        # headroom for long analysis, prompt engineering, and synthesis.
        args["max_tokens"] = int(user_args.get("max_tokens") or DEFAULT_CHAT_MAX_TOKENS)
    model = _stage_model(step, user_args)
    if model:
        args["model"] = model
    if user_args.get("system"):
        args["system"] = user_args["system"]
    effort = user_args.get("reasoning_effort") or step.get("reasoning_effort")
    if effort:
        args["reasoning_effort"] = effort
    return args


def _enrich_write_args(
    step: Dict[str, Any],
    *,
    user_args: Dict[str, Any],
    pol: Dict[str, Any],
    artifacts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Arguments for a structured `*_write` leaf (WRITING_SCHEMA shape).

    The write leaf collects its own durable fact pack for readme/technical-doc and
    enforces durable safety, so we hand it a task + instruction + project_root and
    let it ground the whole project itself instead of pushing a prompt blob.
    """
    artifacts = artifacts or {}
    instruction = str(user_args.get("prompt") or user_args.get("instruction") or "").strip()
    full_instruction = instruction or str(step.get("instruction") or "")
    operational_handoff = _handoff_context(user_args)
    if operational_handoff:
        full_instruction = "\n\n".join(
            part for part in (full_instruction, operational_handoff) if part
        )
    if step.get("revise_notes"):
        full_instruction += (
            "\n\nREVISE — the previous draft failed these local checks; fix each: "
            + "; ".join(str(n) for n in step["revise_notes"])
        )
    args: Dict[str, Any] = {
        "task": str(step.get("write_task") or "auto"),
        "instruction": full_instruction,
        "project_root": str(user_args.get("project_root") or "."),
        "output_mode": "final",
    }
    # For change-class tasks let the leaf pull git context; durable tasks force it off.
    if pol.get("git") == "on":
        args["project_context"] = "auto"
    if user_args.get("output_mode"):
        args["output_mode"] = user_args["output_mode"]
    for key in recipes.WRITE_PASSTHROUGH:
        if user_args.get(key) not in (None, ""):
            args[key] = user_args[key]
    if "reasoning_effort" not in args and step.get("reasoning_effort"):
        args["reasoning_effort"] = step["reasoning_effort"]
    # Give the writer enough room for a full structured document — the leaf's default
    # is small and silently truncated long READMEs mid-section. Caller can override.
    if user_args.get("max_tokens"):
        args["max_tokens"] = int(user_args["max_tokens"])
    else:
        args["max_tokens"] = DEFAULT_WRITE_MAX_TOKENS
    model = _stage_model(step, user_args)  # per-stage override wins over global
    if model:
        args["model"] = model
    # Feed an upstream stage's output (e.g. grounded search) as the source to transform,
    # unless the caller supplied explicit source text/file.
    if "source_text" not in args and "source_file" not in args:
        upstream = (
            artifacts.get("findings_text") or artifacts.get("search") or artifacts.get("outline")
        )
        if upstream:
            args["source_text"] = str(upstream)
    return args


def _args_for_tool(
    step: Dict[str, Any],
    tool: str,
    *,
    user_args: Dict[str, Any],
    pol: Dict[str, Any],
    artifacts: Dict[str, Any],
) -> Dict[str, Any]:
    """Build leaf arguments matching the *selected* tool, not just the capability.

    A single stage may fall back from `google_antigravity_write` to `claude_codex_chat`;
    the two demand different argument shapes, so shape follows the tool actually chosen.
    """
    family = _tool_family(tool)
    if family == "write":
        args = _enrich_write_args(step, user_args=user_args, pol=pol, artifacts=artifacts)
    elif family == "search":
        prompt = str(user_args.get("prompt") or user_args.get("instruction") or "").strip()
        args = {"query": prompt or step.get("suggested_arguments", {}).get("query") or "status"}
        if user_args.get("model"):
            args["model"] = user_args["model"]
        for key in ("language", "freshness", "max_sources"):
            if user_args.get(key) not in (None, ""):
                args[key] = user_args[key]
    elif family == "image":
        prompt = str(user_args.get("prompt") or user_args.get("instruction") or "").strip()
        args = {"prompt": prompt or str(step.get("instruction") or "")}
        for key in ("model", "aspect_ratio", "image_size"):
            if user_args.get(key) not in (None, ""):
                args[key] = user_args[key]
    elif family == "compare":
        prompt = str(user_args.get("prompt") or user_args.get("instruction") or "").strip()
        operational_handoff = _handoff_context(user_args)
        if operational_handoff:
            prompt = "\n\n".join(part for part in (prompt, operational_handoff) if part)
        args = {"prompt": prompt or str(step.get("instruction") or "")}
        # COMPARE_SCHEMA takes `models` (plural), not `model`.
        if user_args.get("models") not in (None, "", []):
            args["models"] = user_args["models"]
    elif family == "review_diff":
        prompt = str(user_args.get("prompt") or user_args.get("instruction") or "").strip()
        operational_handoff = _handoff_context(user_args)
        if operational_handoff:
            prompt = "\n\n".join(part for part in (prompt, operational_handoff) if part)
        args = {"cwd": str(user_args.get("project_root") or ".")}
        if prompt:
            args["instruction"] = prompt
        for key in ("focus", "base", "ref", "staged", "paths", "model"):
            if user_args.get(key) not in (None, ""):
                args[key] = user_args[key]
    elif family == "release":
        args = {"repo": str(user_args.get("project_root") or user_args.get("repo") or ".")}
        for key in ("base_ref", "head_ref", "title", "version", "tag", "polish", "model"):
            if user_args.get(key) not in (None, ""):
                args[key] = user_args[key]
    else:
        # chat family (including write→chat degradation): fold artifacts into the prompt
        args = _enrich_chat_args(step, user_args=user_args, pol=pol, artifacts=artifacts)

    # A model id is provider-specific. When a stage falls back to a different-provider
    # tool, a carried-over model (e.g. grok-4.5 sent to a Gemini/Claude leaf) 404s — so
    # normalize the model to the SELECTED tool's provider (or drop it for the leaf default).
    if family not in {"compare"}:
        model = _resolve_model_for_tool(
            tool,
            args.get("model"),
            provider_models=(
                user_args.get("_provider_models")
                if family != "image"
                and isinstance(user_args.get("_provider_models"), dict)
                else None
            ),
        )
        if model:
            args["model"] = model
        else:
            args.pop("model", None)
        provider = next(
            (provider_id for prefix, provider_id in _PROVIDER_ID.items() if tool.startswith(prefix)),
            None,
        )
        automatic_effort = (
            "reasoning_effort" in args
            and not str(user_args.get("reasoning_effort") or "").strip()
        )
        if (
            automatic_effort
            and provider
            and model
            and not agent_capabilities.supports_reasoning_effort(provider, model)
        ):
            args.pop("reasoning_effort", None)
            warning = f"automatic_reasoning_effort_omitted:{provider}:{model}"
            warnings = step.setdefault("warnings", [])
            if warning not in warnings:
                warnings.append(warning)
    return args


_PROVIDER_MODEL_PREFIX = {
    "grok_codex": "grok",
    "claude_codex": "claude",
    "openai_codex": "gpt",
    "google_antigravity": "gemini",
    "google_grounded": "gemini",
}
_PROVIDER_ID = {
    "grok_codex": "grok",
    "claude_codex": "claude",
    "openai_codex": "gpt",
    "google_antigravity": "gemini",
    "google_grounded": "gemini",
}


def _resolve_model_for_tool(
    tool: str,
    requested: Optional[str],
    *,
    provider_models: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Return a model id valid for `tool`'s provider: keep a compatible request, else the
    provider's latest (catalog), else None so the leaf uses its own default."""
    provider = next((p for p in _PROVIDER_MODEL_PREFIX if str(tool).startswith(p)), None)
    if requested:
        if provider is None:
            return requested  # unknown provider — trust the caller
        if _PROVIDER_ID.get(provider) == "gemini" and str(requested).lower().startswith(
            ("gemini", "claude", "gpt-oss")
        ):
            # Antigravity intentionally exposes Gemini, Claude, and GPT-family
            # model ids through one Google provider catalog.
            return requested
        if str(requested).lower().startswith(_PROVIDER_MODEL_PREFIX[provider]):
            return requested  # already matches this provider
    provider_id = _PROVIDER_ID.get(provider or "")
    configured = (
        str((provider_models or {}).get(provider_id) or "").strip()
        if provider_id
        else ""
    )
    if configured:
        return configured
    return catalog.latest_for(tool)


def _build_steps(
    recipe: Dict[str, Any], bindings: Dict[str, str], user_args: Dict[str, Any], pol: Dict[str, Any]
) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    for i, stage in enumerate(recipe["stages"]):
        capability = str(stage.get("capability") or "")
        tool = stage.get("tool")
        binding = stage.get("binding")
        if tool is None and binding:
            tool = bindings.get(str(binding))
        fallbacks = _fallback_tools(capability, bindings) if capability in FALLBACK_CHAINS else []
        # The stage's bound tool must be the PRIMARY (attempt 0), even when it already
        # appears later in the capability's fallback chain — otherwise a stage bound to
        # e.g. grok would silently run claude (fallback[0]). This makes per-stage leaf
        # selection authoritative, which multi-LLM recipes depend on.
        if tool and capability in FALLBACK_CHAINS:
            fallbacks = [tool] + [t for t in fallbacks if t != tool]
        step = {
            "index": i,
            "id": stage["id"],
            "kind": stage["kind"],
            "capability": capability,
            "tool": tool,
            "fallback_tools": fallbacks,
            "tool_attempt_index": 0,
            "write_task": stage.get("write_task"),
            "reasoning_effort": stage.get("reasoning_effort"),
            "instruction": stage.get("instruction"),
            "suggested_arguments": recipes._suggest_args(stage, user_args, pol),
            "status": "pending",
            "result_text": "",
            "error": "",
        }
        steps.append(step)
    return steps


def start_run(
    recipe_id: str,
    *,
    args: Optional[Dict[str, Any]] = None,
    bindings: Optional[Dict[str, str]] = None,
    project_root: str = ".",
    auto_local: bool = True,
) -> Dict[str, Any]:
    recipe = recipes.get_recipe(recipe_id)
    pol = policy.get_policy(recipe["doc_class"])
    bind = _bindings(bindings)
    user_args = dict(args or {})
    if project_root:
        user_args.setdefault("project_root", project_root)
    canonical_root = str(
        gather.validate_project_root(user_args.get("project_root") or project_root or ".")
    )
    user_args["project_root"] = canonical_root
    handoff_snapshot = _load_handoff_snapshot(user_args)
    run_id = uuid.uuid4().hex[:12]
    created_at = time.time()
    state: Dict[str, Any] = {
        "run_id": run_id,
        "run_kind": "fixed",
        "recipe_id": recipe["id"],
        "doc_class": recipe["doc_class"],
        "context_policy": pol,
        "bindings": bind,
        "fallback_chains": FALLBACK_CHAINS,
        "mode": "supervised",
        "state_schema_version": RUN_STATE_SCHEMA_VERSION,
        "store_revision": 0,
        "version": __version__,
        "created_at": created_at,
        "updated_at": created_at,
        "user_args": user_args,
        "_handoff_snapshot": handoff_snapshot,
        "project_root": canonical_root,
        "steps": _build_steps(recipe, bind, user_args, pol),
        "artifacts": {},
        "cursor": 0,
        "status": "running",
        "revisions": 0,
        "revision_budget": (
            int(user_args["revision_budget"])
            if str(user_args.get("revision_budget", "")).strip() != ""
            else DEFAULT_REVISION_BUDGET
        ),
        "note": (
            "Call leaf MCP tools for steps with a tool. "
            "Local gather/verify run automatically. "
            "Then orchestrate_continue_recipe with stage result."
        ),
    }
    # Warn (non-fatal) if a recipe with a generative stage got no prompt to work from.
    needs_prompt = any(
        s.get("capability") in {"chat", "grounded_search", "write"} for s in state["steps"]
    )
    prompt_text = str(user_args.get("prompt") or user_args.get("instruction") or "").strip()
    if needs_prompt and not prompt_text:
        state.setdefault("warnings", []).append(
            "missing_prompt: recipe has a generative stage but no prompt/instruction"
        )
    events.append_event(
        state,
        "run_created",
        at=created_at,
        base_revision=None,
        resulting_revision=0,
        run_kind="fixed",
        workflow_id=recipe["id"],
        status=str(state.get("status") or ""),
        **events.text_identity(prompt_text, prefix="prompt"),
    )
    if auto_local:
        _auto_local_forward(state)
    state = store.create(state)
    _RUNS[run_id] = state
    _prune_runs()
    return _public_state(state)


def _prune_runs() -> None:
    """Bound the in-process run store; evict oldest runs first."""
    if len(_RUNS) <= MAX_RUNS:
        return
    ordered = sorted(_RUNS.values(), key=lambda s: float(s.get("created_at") or 0.0))
    for stale in ordered[: len(_RUNS) - MAX_RUNS]:
        _RUNS.pop(str(stale.get("run_id") or ""), None)


def forget_run(run_id: str) -> None:
    """Drop one process-local cache entry after a lifecycle state change or GC."""

    _RUNS.pop(store.validate_run_id(run_id), None)


def load_state(state_or_id: Any) -> Dict[str, Any]:
    if isinstance(state_or_id, str) and state_or_id:
        run_id = store.validate_run_id(state_or_id)
        if run_id in _RUNS:
            return _RUNS[run_id]
        # Not in memory — try the on-disk store (survives process restart).
        persisted = store.load(run_id)
        if persisted is not None:
            _RUNS[run_id] = persisted
            return persisted
    if isinstance(state_or_id, dict) and state_or_id.get("steps"):
        raw_run_id = state_or_id.get("run_id")
        if raw_run_id not in (None, ""):
            store.validate_run_id(raw_run_id)
        return copy.deepcopy(state_or_id)
    raise ValueError("Provide run_id from start_run or a full state object")


def _public_state(state: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(state)
    out.pop("_lease", None)
    out["event_journal"] = events.event_summary(out)
    out.pop("events", None)
    out.pop("event_seq", None)
    out.pop("events_dropped", None)
    out["current_step"] = _current_step(out)
    out["next_action"] = _next_action(out)
    snapshot = out.pop("_handoff_snapshot", None)
    if isinstance(snapshot, dict):
        out["handoff"] = handoff_state.public_snapshot(snapshot)
    if (
        out.get("run_id")
        and isinstance(out.get("store_revision"), int)
        and out["next_action"].get("type") not in {"done", "failed"}
    ):
        out["next_action"]["expected_revision"] = out["store_revision"]
    out["done"] = out.get("status") in store.TERMINAL_RUN_STATUSES
    return out


def _current_step(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    steps = state.get("steps") or []
    cursor = int(state.get("cursor") or 0)
    if cursor < 0 or cursor >= len(steps):
        return None
    return steps[cursor]


def _action_identity(state: Dict[str, Any], action: Dict[str, Any]) -> str:
    arguments = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
    arguments_json = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    material = json.dumps(
        {
            "schema": FIXED_ACTION_SCHEMA,
            "run_id": str(state.get("run_id") or ""),
            "store_revision": int(state.get("store_revision") or 0),
            "stage_id": str(action.get("stage_id") or ""),
            "tool": str(action.get("tool") or ""),
            "arguments_sha256": sha256(arguments_json.encode("utf-8")).hexdigest(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _next_action(state: Dict[str, Any]) -> Dict[str, Any]:
    if state.get("status") == "completed":
        return {"type": "done", "message": "Recipe completed"}
    if state.get("status") in store.TERMINAL_RUN_STATUSES - {"completed"}:
        status = str(state.get("status") or "failed")
        return {
            "type": "failed",
            "message": state.get("error") or f"Recipe is {status}",
        }
    step = _current_step(state)
    if not step:
        return {"type": "done", "message": "No more steps"}
    if step.get("status") == "ready" or step.get("status") == "pending":
        pol = state.get("context_policy") or {}
        if step.get("tool"):
            tools = step.get("fallback_tools") or ([step["tool"]] if step.get("tool") else [])
            idx = int(step.get("tool_attempt_index") or 0)
            tool = tools[idx] if idx < len(tools) else step.get("tool")
            # Argument shape follows the leaf actually selected (write vs chat vs search).
            step["suggested_arguments"] = _args_for_tool(
                step,
                tool,
                user_args={
                    **(state.get("user_args") or {}),
                    "_handoff_snapshot": state.get("_handoff_snapshot"),
                },
                pol=pol,
                artifacts=state.get("artifacts") or {},
            )
            action = {
                "type": "call_tool",
                "schema": FIXED_ACTION_SCHEMA,
                "stage_id": step["id"],
                "tool": tool,
                "fallback_tools": tools,
                "arguments": step.get("suggested_arguments") or {},
                "instruction": step.get("instruction"),
            }
            if step.get("warnings"):
                action["warnings"] = list(step["warnings"])
            action["action_id"] = _action_identity(state, action)
            return action
        return {
            "type": "local",
            "stage_id": step["id"],
            "kind": step.get("kind"),
            "message": "Local stage — will auto-run on continue if still pending",
        }
    return {"type": "continue", "stage_id": step.get("id")}


def _auto_local_forward(state: Dict[str, Any]) -> None:
    """Execute consecutive local gather/verify steps without leaf tools."""
    while True:
        step = _current_step(state)
        if not step or step.get("tool"):
            return
        if step.get("kind") not in {"gather", "verify"} and step.get("capability") not in {
            "local",
            "local_git",
            "local_code",
        }:
            return
        if step.get("status") in {"completed", "failed"}:
            state["cursor"] = int(state.get("cursor") or 0) + 1
            if state["cursor"] >= len(state["steps"]):
                state["status"] = "completed"
            continue
        _run_local_step(state, step)
        if step.get("status") == "failed":
            state["status"] = "failed"
            state["error"] = step.get("error")
            return
        _advance_cursor(state)  # honors a verify-scheduled revision rewind
        if state["cursor"] >= len(state["steps"]):
            state["status"] = "completed"
            return


def _run_local_step(state: Dict[str, Any], step: Dict[str, Any]) -> None:
    root = str(state.get("project_root") or ".")
    try:
        # Check verify FIRST: verify stages also carry capability "local", so the gather
        # branch below would otherwise swallow them and they'd never actually verify.
        is_gather = step.get("kind") == "gather" or (
            step.get("kind") != "verify" and step.get("capability") in {"local", "local_git"}
        )
        if is_gather:
            data = gather.run_gather(step, project_root=root, args=state.get("user_args") or {})
            step["status"] = "completed" if data.get("ok", True) else "failed"
            step["result_text"] = str(data.get("text") or "")
            if not data.get("ok", True):
                step["error"] = str(data.get("error") or "gather failed")
            artifacts = state.setdefault("artifacts", {})
            if step.get("id") == "gather_git" or step.get("capability") == "local_git":
                artifacts["git"] = data
                artifacts["git_text"] = data.get("text")
            elif step.get("id") == "gather_code" or step.get("capability") == "local_code":
                artifacts["code"] = data
                artifacts["code_text"] = data.get("text")
            else:
                artifacts["facts"] = data
                artifacts["facts_text"] = data.get("text")
            return
        if step.get("kind") == "verify":
            # verify previous draft/outline text
            artifacts = state.get("artifacts") or {}
            text = str(
                artifacts.get("draft")
                or artifacts.get("outline")
                or artifacts.get("last_leaf_text")
                or ""
            )
            result = verify.verify_text(
                text,
                doc_class=str(state.get("doc_class") or "durable"),
                fact_pack=artifacts.get("facts")
                if isinstance(artifacts.get("facts"), dict)
                else None,
                user_facing=(
                    state.get("recipe_id") == "durable_readme"
                    or (state.get("user_args") or {}).get("task") == "readme"
                ),
            )
            step["status"] = "completed"
            step["result_text"] = result.get("text") or ""
            artifacts["verify"] = result
            if result.get("warnings"):
                state.setdefault("warnings", [])
                state["warnings"].extend(result["warnings"])
            revision_scheduled = _maybe_schedule_revision(
                state,
                step,
                result.get("warnings") or [],
            )
            if not result.get("ok") and not revision_scheduled:
                step["status"] = "failed"
                step["error"] = "document_quality_failed: " + "; ".join(
                    str(item) for item in result.get("warnings") or []
                )
            return
        step["status"] = "completed"
        step["result_text"] = ""
    except Exception as exc:  # noqa: BLE001
        step["status"] = "failed"
        step["error"] = str(exc)


# Warning prefixes worth re-drafting for (vs. purely informational ones).
_ACTIONABLE_WARNINGS = (
    "recency_language",
    "tool_not_in_fact_pack",
    "git_internals",
    "korean_style",
    "unclosed_code_fence",
    "truncated",
)
DEFAULT_REVISION_BUDGET = 1


def _maybe_schedule_revision(
    state: Dict[str, Any],
    verify_step: Dict[str, Any],
    warnings: List[str],
) -> bool:
    """Turn verify from advisory into a control loop: on actionable warnings, rewind
    to the draft stage with correction notes, up to a bounded revision budget."""
    actionable = [w for w in warnings if w.startswith(_ACTIONABLE_WARNINGS)]
    if not actionable:
        return False
    budget = int(state.get("revision_budget", DEFAULT_REVISION_BUDGET))
    used = int(state.get("revisions", 0))
    if used >= budget:
        return False
    draft_idx = next((i for i, s in enumerate(state["steps"]) if s.get("id") == "draft"), None)
    if draft_idx is None:
        return False
    state["revisions"] = used + 1
    draft = state["steps"][draft_idx]
    draft["status"] = "pending"
    draft["tool_attempt_index"] = 0  # retry the primary (write) leaf
    draft["revise_notes"] = actionable
    verify_step["status"] = "pending"  # re-verify the revised draft
    state["_revise_to"] = draft_idx
    return True


def _advance_cursor(state: Dict[str, Any]) -> None:
    """Advance past the current step, unless a revision rewind was scheduled."""
    rewind = state.pop("_revise_to", None)
    if rewind is not None:
        state["cursor"] = int(rewind)
    else:
        state["cursor"] = int(state.get("cursor") or 0) + 1


def _advance_run_state(
    st: Dict[str, Any],
    *,
    stage_id: str,
    result_text: str,
    leaf_result: Optional[Dict[str, Any]],
    success: bool,
    error: str,
    auto_local: bool,
) -> Dict[str, Any]:
    step = _current_step(st)
    if not step:
        st["status"] = "completed"
        return st

    sid = stage_id or str(step.get("id") or "")
    if sid and sid != step.get("id"):
        raise ValueError(f"stage_id mismatch: expected {step.get('id')}, got {sid}")

    # If current is still local pending, run it
    if not step.get("tool") and step.get("status") == "pending":
        _run_local_step(st, step)
        if step.get("status") == "failed":
            st["status"] = "failed"
            st["error"] = step.get("error")
            return st
        _advance_cursor(st)  # honors a verify-scheduled revision rewind
        if auto_local:
            _auto_local_forward(st)
        if st.get("cursor", 0) >= len(st["steps"]) and st.get("status") == "running":
            st["status"] = "completed"
        return st

    if isinstance(leaf_result, dict) and isinstance(leaf_result.get("success"), bool):
        if bool(leaf_result["success"]) != bool(success):
            raise ValueError("leaf_result.success does not match success")
    normalized_result = results.OperationResult.from_result(
        leaf_result or {},
        success=success,
        text=result_text,
        error=error or None,
    )
    result_text = normalized_result.text
    success = normalized_result.success
    state_result = normalized_result.as_state_dict()
    step["result"] = state_result
    attempt_results = step.setdefault("attempt_results", [])
    if not isinstance(attempt_results, list):
        attempt_results = []
        step["attempt_results"] = attempt_results
    attempt_results.append(
        {
            **state_result,
            "tool": str(step.get("tool") or ""),
            "attempt_index": int(step.get("tool_attempt_index") or 0),
        }
    )
    del attempt_results[:-max(1, len(step.get("fallback_tools") or []) or 1)]

    if success:
        step["status"] = "completed"
        step["result_text"] = result_text or ""
        artifacts = st.setdefault("artifacts", {})
        artifacts["last_leaf_text"] = result_text or ""
        # stash by stage id
        if step.get("id") == "outline":
            artifacts["outline"] = result_text
        elif step.get("id") in {"draft", "synthesize", "chat"}:
            artifacts["draft"] = result_text
        elif step.get("id") == "search":
            artifacts["search"] = result_text
        elif str(step.get("id") or "").startswith("investigate"):
            # Multi-LLM investigation: accumulate each analyst's findings for the writer.
            fnd = artifacts.setdefault("findings", [])
            fnd.append({"analyst": step.get("tool"), "stage": step.get("id"), "text": result_text})
            artifacts["findings_text"] = "\n\n".join(
                f"### Finding — {f['stage']} (by {f['analyst']}):\n{f['text']}" for f in fnd
            )
        st["cursor"] = int(st.get("cursor") or 0) + 1
        if st["cursor"] >= len(st["steps"]):
            st["status"] = "completed"
        elif auto_local:
            _auto_local_forward(st)
            if st.get("cursor", 0) >= len(st["steps"]) and st.get("status") == "running":
                st["status"] = "completed"
    else:
        err_text = error or result_text or "leaf failed"
        category = errors.classify(err_text)
        step["error_category"] = category
        tools = step.get("fallback_tools") or []
        idx = int(step.get("tool_attempt_index") or 0) + 1
        # A malformed-request error won't be fixed by another provider — fail fast.
        if idx < len(tools) and errors.should_rotate(category):
            step["tool_attempt_index"] = idx
            step["status"] = "pending"
            step["error"] = f"{err_text} [{category}; rotating to {tools[idx]}]"
            step["tool"] = tools[idx]
        else:
            step["status"] = "failed"
            step["error"] = f"{err_text} [{category}]"
            st["status"] = "failed"
            st["error"] = step["error"]

    return st


def _validate_fixed_run_state(state: Dict[str, Any]) -> None:
    run_kind = state.get("run_kind")
    if (
        run_kind not in (None, "fixed")
        or not str(state.get("recipe_id") or "").strip()
        or not isinstance(state.get("steps"), list)
    ):
        raise ValueError("persisted run is not a fixed recipe run")
    store.validate_run_status(state)


def _handoff_policy(state: Dict[str, Any], override: str | None) -> str:
    drift_policy = (
        str(
            override
            or (state.get("user_args") or {}).get("handoff_drift_policy")
            or "pause"
        )
        .strip()
        .lower()
    )
    if drift_policy not in {"pause", "use-snapshot"}:
        raise ValueError("handoff_drift_policy must be pause or use-snapshot")
    return drift_policy


def _apply_handoff_drift_policy(
    state: Dict[str, Any],
    *,
    drift_policy: str,
) -> Dict[str, Any]:
    handoff_snapshot = state.get("_handoff_snapshot")
    drift = handoff_state.check_drift(
        handoff_snapshot if isinstance(handoff_snapshot, dict) else None
    )
    if drift.get("drifted") and drift_policy == "pause":
        raise handoff_state.HandoffDrift(drift)
    if drift.get("drifted"):
        state["handoff_drift"] = drift
        warnings = state.setdefault("warnings", [])
        if "handoff_drift_using_snapshot" not in warnings:
            warnings.append("handoff_drift_using_snapshot")
    else:
        state.pop("handoff_drift", None)
    return drift


def claim_next_action(
    *,
    run_id: str,
    expected_revision: int | None = None,
    action_id: str = "",
    lease_seconds: float = RUN_LEASE_SECONDS,
    handoff_drift_policy: str | None = None,
) -> FixedActionClaim:
    """Claim the authoritative fixed action before invoking its provider."""

    claim = store.claim(
        store.validate_run_id(run_id),
        expected_revision=expected_revision,
        lease_seconds=lease_seconds,
    )
    try:
        _validate_fixed_run_state(claim.state)
        if claim.state.get("status") in store.TERMINAL_RUN_STATUSES:
            raise ValueError("terminal run has no claimable action")
        drift_policy = _handoff_policy(claim.state, handoff_drift_policy)
        _apply_handoff_drift_policy(
            copy.deepcopy(claim.state),
            drift_policy=drift_policy,
        )
        action_state = copy.deepcopy(claim.state)
        action = _next_action(action_state)
        if action.get("type") != "call_tool":
            raise ValueError("current fixed step is not a provider action")
        current_action_id = str(action.get("action_id") or "")
        if action_id and action_id != current_action_id:
            raise ValueError("action_id does not match the authoritative next action")
        action["expected_revision"] = claim.base_revision
        return FixedActionClaim(
            store_claim=claim,
            action=action,
            handoff_drift_policy=drift_policy,
        )
    except Exception:
        try:
            store.abort_claim(claim)
        except Exception:  # noqa: BLE001
            pass
        raise


def resume_action_claim(
    *,
    run_id: str,
    claim_token: str,
    base_revision: int,
    action_id: str,
    handoff_drift_policy: str | None = None,
) -> FixedActionClaim:
    claim = store.resume_claim(
        store.validate_run_id(run_id),
        token=claim_token,
        base_revision=base_revision,
    )
    try:
        _validate_fixed_run_state(claim.state)
        drift_policy = _handoff_policy(claim.state, handoff_drift_policy)
        action = _next_action(copy.deepcopy(claim.state))
        if action.get("type") != "call_tool" or action.get("action_id") != action_id:
            raise ValueError("action_id does not match the claimed next action")
        action["expected_revision"] = claim.base_revision
        return FixedActionClaim(
            store_claim=claim,
            action=action,
            handoff_drift_policy=drift_policy,
        )
    except Exception:
        try:
            store.abort_claim(claim)
        except Exception:  # noqa: BLE001
            pass
        raise


def abort_action_claim(claim: FixedActionClaim) -> Dict[str, Any]:
    released = store.abort_claim(claim.store_claim)
    _RUNS[claim.store_claim.run_id] = released
    return _public_state(released)


def _append_fixed_commit_event(
    state: Dict[str, Any],
    *,
    action: Dict[str, Any],
    base_revision: int,
) -> None:
    stage_id = str(action.get("stage_id") or "")
    step = next(
        (
            item
            for item in state.get("steps") or []
            if isinstance(item, dict) and str(item.get("id") or "") == stage_id
        ),
        {},
    )
    metadata = step.get("result") if isinstance(step.get("result"), dict) else {}
    is_provider_action = action.get("type") == "call_tool"
    if is_provider_action:
        succeeded = bool(metadata.get("success"))
        event_type = "action_completed" if succeeded else "action_failed"
    else:
        succeeded = step.get("status") == "completed"
        event_type = "local_step_completed" if succeeded else "local_step_failed"
    raw_error = metadata.get("error")
    error_type = ""
    if isinstance(raw_error, dict):
        error_type = str(raw_error.get("type") or raw_error.get("code") or "")
    error_type = error_type or str(step.get("error_category") or "")
    result_fields: Dict[str, Any] = {}
    if isinstance(metadata.get("text_chars"), int):
        result_fields["result_chars"] = metadata["text_chars"]
    if isinstance(metadata.get("text_sha256"), str):
        result_fields["result_sha256"] = metadata["text_sha256"]
    if not result_fields and isinstance(step.get("result_text"), str):
        result_fields = events.text_identity(step["result_text"], prefix="result")
    events.append_event(
        state,
        event_type,
        at=float(state.get("updated_at") or time.time()),
        base_revision=base_revision,
        resulting_revision=base_revision + 1,
        run_kind="fixed",
        workflow_id=str(state.get("recipe_id") or ""),
        status=str(state.get("status") or ""),
        stage_id=stage_id,
        action_id=str(action.get("action_id") or ""),
        tool=str(action.get("tool") or ""),
        provider=metadata.get("provider"),
        model=metadata.get("model"),
        success=succeeded,
        error_type=error_type or None,
        retryable=bool(not succeeded and state.get("status") not in {"failed", "cancelled"}),
        usage=metadata.get("usage"),
        **result_fields,
    )


def commit_claimed_action(
    claim: FixedActionClaim,
    *,
    result_text: str = "",
    leaf_result: Optional[Dict[str, Any]] = None,
    success: bool = True,
    error: str = "",
    auto_local: bool = True,
) -> Dict[str, Any]:
    """Commit one provider result only while its action lease still matches."""

    try:
        working_state = copy.deepcopy(claim.store_claim.state)
        current_action = _next_action(copy.deepcopy(working_state))
        if (
            current_action.get("type") != "call_tool"
            or current_action.get("action_id") != claim.action.get("action_id")
        ):
            raise ValueError("claimed action no longer matches the run state")
        _apply_handoff_drift_policy(
            working_state,
            drift_policy=claim.handoff_drift_policy,
        )
        advanced = _advance_run_state(
            working_state,
            stage_id=str(claim.action.get("stage_id") or ""),
            result_text=result_text,
            leaf_result=leaf_result,
            success=success,
            error=error,
            auto_local=auto_local,
        )
        advanced["updated_at"] = time.time()
        _append_fixed_commit_event(
            advanced,
            action=current_action,
            base_revision=claim.store_claim.base_revision,
        )
        committed = store.commit_claim(claim.store_claim, advanced)
    except Exception:
        try:
            store.abort_claim(claim.store_claim)
        except Exception:  # noqa: BLE001
            pass
        raise
    _RUNS[claim.store_claim.run_id] = committed
    _prune_runs()
    return _public_state(committed)


def continue_run(
    *,
    run_id: str = "",
    state: Optional[Dict[str, Any]] = None,
    stage_id: str = "",
    result_text: str = "",
    leaf_result: Optional[Dict[str, Any]] = None,
    success: bool = True,
    error: str = "",
    auto_local: bool = True,
    expected_revision: int | None = None,
    action_id: str = "",
    claim_token: str = "",
    base_revision: int | None = None,
    handoff_drift_policy: str | None = None,
) -> Dict[str, Any]:
    if isinstance(state, dict) and "_handoff_snapshot" in state:
        raise ValueError("_handoff_snapshot is reserved for persisted internal state")
    claim_fields = bool(action_id or claim_token or base_revision is not None)
    if claim_fields:
        if state is not None:
            raise ValueError("claimed continuation cannot include echoed state")
        if not action_id or not claim_token or base_revision is None:
            raise ValueError(
                "action_id, claim_token, and base_revision are required together"
            )
        if expected_revision is not None and expected_revision != base_revision:
            raise ValueError("expected_revision does not match base_revision")
        claimed = resume_action_claim(
            run_id=run_id,
            claim_token=claim_token,
            base_revision=base_revision,
            action_id=action_id,
            handoff_drift_policy=handoff_drift_policy,
        )
        if stage_id and stage_id != claimed.action.get("stage_id"):
            abort_action_claim(claimed)
            raise ValueError("stage_id does not match the claimed action")
        return commit_claimed_action(
            claimed,
            result_text=result_text,
            leaf_result=leaf_result,
            success=success,
            error=error,
            auto_local=auto_local,
        )
    state_run_id = ""
    state_revision = None
    if state is not None:
        state_run_id = (
            store.validate_run_id(state.get("run_id"))
            if state.get("run_id") not in (None, "")
            else ""
        )
        state_revision = state.get("store_revision")
    requested_run_id = store.validate_run_id(run_id) if run_id not in (None, "") else ""
    if state_run_id and requested_run_id and state_run_id != requested_run_id:
        raise ValueError("run_id does not match state.run_id")
    for field, value in (
        ("expected_revision", expected_revision),
        ("state.store_revision", state_revision),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"{field} must be a non-negative integer")
    if (
        expected_revision is not None
        and state_revision is not None
        and expected_revision != state_revision
    ):
        raise ValueError("expected_revision does not match state.store_revision")
    expected = expected_revision if expected_revision is not None else state_revision
    persistent_run_id = state_run_id or requested_run_id
    if not persistent_run_id:
        if state is not None and state.get("run_kind") == "adaptive":
            raise ValueError("state is not a fixed recipe run")
        raise ValueError(
            "fixed continuation requires run_id; caller-supplied state-only "
            "continuation cannot provide revision, lease, or handoff drift guarantees"
        )

    claim = store.claim(
        persistent_run_id,
        expected_revision=expected,
        lease_seconds=RUN_LEASE_SECONDS,
    )
    try:
        _validate_fixed_run_state(claim.state)
        if claim.state.get("status") in store.TERMINAL_RUN_STATUSES:
            released = store.abort_claim(claim)
            _RUNS[persistent_run_id] = released
            return _public_state(released)
        drift_policy = _handoff_policy(claim.state, handoff_drift_policy)
        working_state = copy.deepcopy(claim.state)
        _apply_handoff_drift_policy(
            working_state,
            drift_policy=drift_policy,
        )
        current_action = _next_action(copy.deepcopy(working_state))
        advanced = _advance_run_state(
            working_state,
            stage_id=stage_id,
            result_text=result_text,
            leaf_result=leaf_result,
            success=success,
            error=error,
            auto_local=auto_local,
        )
        advanced["updated_at"] = time.time()
        _append_fixed_commit_event(
            advanced,
            action=current_action,
            base_revision=claim.base_revision,
        )
        committed = store.commit_claim(claim, advanced)
    except Exception:
        try:
            store.abort_claim(claim)
        except Exception:  # noqa: BLE001
            pass
        raise
    _RUNS[persistent_run_id] = committed
    _prune_runs()
    return _public_state(committed)


_CAP_BINDING = {
    "chat": "chat",
    "write": "write_ag",
    "grounded_search": "grounded_search",
    "image": "image",
    "review_diff": "review_diff",
    "release": "release",
    "compare": "compare",
}


def prepare_step(
    *,
    capability: str = "chat",
    instruction: str,
    doc_class: str = "direct",
    model: Optional[str] = None,
    leaf: Optional[str] = None,
    write_task: Optional[str] = None,
    gather_kind: Optional[str] = None,
    project_root: str = ".",
    context: Optional[str] = None,
    bindings: Optional[Dict[str, str]] = None,
    provider_models: Optional[Dict[str, str]] = None,
    extra_args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Prepare ONE ready-to-call leaf invocation for a host-planned delegation.

    The host model (GPT) decides *what* to delegate and *to whom*; this resolves the
    tool + latest model, injects deterministic context (facts/code/git) and any prior
    findings, applies the doc-class policy, and returns a fully-formed call. The host
    then invokes `tool` with `arguments` and (for durable/transform) runs verify.
    """
    capability = str(capability or "chat")
    pol = policy.get_policy(doc_class)
    bind = _bindings(bindings)

    tool = leaf or (bind.get(_CAP_BINDING[capability]) if capability in _CAP_BINDING else None)
    fallbacks = _fallback_tools(capability, bind) if capability in FALLBACK_CHAINS else []
    if tool and capability in FALLBACK_CHAINS:
        fallbacks = [tool] + [t for t in fallbacks if t != tool]
    elif not tool and fallbacks:
        tool = fallbacks[0]
    if not tool:
        raise ValueError(f"cannot resolve a leaf for capability={capability!r}")

    resolved_model = _resolve_model_for_tool(
        tool,
        model,
        provider_models=provider_models if capability != "image" else None,
    )

    artifacts: Dict[str, Any] = {}
    root = str(project_root or ".")
    if gather_kind == "facts":
        d = gather.gather_durable_facts(root)
        artifacts["facts"] = d
        artifacts["facts_text"] = d.get("text")
    elif gather_kind == "code":
        d = gather.gather_code_context(root)
        artifacts["code_text"] = d.get("text")
    elif gather_kind == "git":
        d = gather.gather_git(root)
        artifacts["git_text"] = d.get("text")
    if context:
        artifacts["findings_text"] = str(context)

    step = {
        "id": "step",
        "capability": capability,
        "write_task": write_task,
        "instruction": instruction,
        "tool": tool,
        "fallback_tools": fallbacks,
    }
    user_args: Dict[str, Any] = {"instruction": instruction, "project_root": root}
    if resolved_model:
        user_args["model"] = resolved_model
    if extra_args:
        user_args.update({k: v for k, v in extra_args.items() if v is not None})

    args = _args_for_tool(step, tool, user_args=user_args, pol=pol, artifacts=artifacts)
    return {
        "capability": capability,
        "doc_class": doc_class,
        "tool": tool,
        "fallback_tools": fallbacks,
        "model": args.get("model") or resolved_model,
        "arguments": args,
        "warnings": list(step.get("warnings") or []),
        "gathered": [
            k for k in ("facts_text", "code_text", "git_text", "findings_text") if k in artifacts
        ],
        "verify_after": doc_class in {"durable", "transform"},
        "note": (
            "Call `tool` with `arguments`. On failure, call the next entry in `fallback_tools`. "
            "If `verify_after`, submit the result text to orchestrate_verify with this doc_class."
        ),
    }


def resolve_bindings(
    available_tools: Optional[List[str]] = None,
    *,
    bindings: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Given the leaf tools actually connected (from the client's tools/list), pick a
    primary + fallback chain per capability and report which recipes are runnable.

    If `available_tools` is empty/None, no filtering is applied (assume all defaults present).
    """
    present = {str(t) for t in (available_tools or [])}
    bind = _bindings(bindings)
    chains: Dict[str, List[str]] = {}
    resolved: Dict[str, str] = {}
    for capability in FALLBACK_CHAINS:
        chain = _fallback_tools(capability, bind)
        usable = [t for t in chain if t in present] if present else chain
        chains[capability] = usable
        if usable:
            resolved[capability] = usable[0]

    runnable: List[str] = []
    blocked: List[Dict[str, Any]] = []
    for rid, recipe in recipes.all_recipes().items():
        missing = sorted(
            {
                str(stage.get("capability"))
                for stage in recipe["stages"]
                if stage.get("capability") in FALLBACK_CHAINS
                and not chains.get(str(stage.get("capability")))
            }
        )
        if missing:
            blocked.append({"id": rid, "missing_capabilities": missing})
        else:
            runnable.append(rid)
    return {
        "available_tools_seen": sorted(present),
        "filtered": bool(present),
        "bindings": resolved,
        "fallback_chains": chains,
        "runnable_recipes": sorted(runnable),
        "blocked_recipes": blocked,
    }


def get_run(run_id: str) -> Dict[str, Any]:
    validated = store.validate_run_id(run_id)
    persisted = store.load_strict(validated)
    _validate_fixed_run_state(persisted)
    _RUNS[validated] = persisted
    return _public_state(persisted)
