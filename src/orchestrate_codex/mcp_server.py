"""MCP stdio server for supervised orchestration v0.2."""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List

from . import __version__, broker, catalog, gather, policy, recipes, runner, store, verify

SERVER_NAME = "orchestrate-codex"
SERVER_VERSION = __version__
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


class RpcError(ValueError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _empty() -> Dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "name": "orchestrate_list_recipes",
            "description": "List built-in supervised orchestration recipes.",
            "inputSchema": _empty(),
        },
        {
            "name": "orchestrate_explain_recipe",
            "description": "Explain a recipe: stages, doc_class, context policy, default leaf bindings.",
            "inputSchema": {
                "type": "object",
                "properties": {"recipe_id": {"type": "string"}},
                "required": ["recipe_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "orchestrate_context_policy",
            "description": "Return context policy for a doc_class (durable|change|transform|direct).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "doc_class": {
                        "type": "string",
                        "enum": sorted(policy.DOC_CLASSES.keys()),
                    }
                },
                "required": ["doc_class"],
                "additionalProperties": False,
            },
        },
        {
            "name": "orchestrate_plan_recipe",
            "description": "Build a static plan (steps + suggested tools). Prefer start_run for stateful execution.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recipe_id": {"type": "string"},
                    "prompt": {"type": "string"},
                    "instruction": {"type": "string"},
                    "model": {"type": "string"},
                    "system": {"type": "string"},
                    "bindings": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["recipe_id"],
                "additionalProperties": True,
            },
        },
        {
            "name": "orchestrate_start_run",
            "description": (
                "Start a supervised run. Auto-executes local gather stages. "
                "Returns next_action (call_tool with leaf name+args, or done). "
                "Does not call other MCP servers — Codex must invoke the leaf tool."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recipe_id": {"type": "string"},
                    "prompt": {"type": "string"},
                    "instruction": {"type": "string"},
                    "model": {"type": "string"},
                    "system": {"type": "string"},
                    "project_root": {
                        "type": "string",
                        "description": "Workspace root for gather_facts / gather_git.",
                        "default": ".",
                    },
                    "bindings": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": 'e.g. {"chat":"grok_codex_chat"}',
                    },
                    "auto_local": {"type": "boolean", "default": True},
                    "source_text": {"type": "string", "description": "Source for transform tasks (translate/polish/summarize)."},
                    "target_language": {"type": "string", "description": "Target language for translate tasks."},
                    "models": {"description": "Model ids for compare_models (array or comma string)."},
                    "version": {"type": "string", "description": "Release version for release_draft."},
                },
                "required": ["recipe_id"],
                "description": "Extra properties are forwarded verbatim to the leaf tool (domain args).",
                "additionalProperties": True,
            },
        },
        {
            "name": "orchestrate_continue_recipe",
            "description": (
                "Advance a run after a leaf tool result. Pass run_id and/or full state. "
                "On leaf failure set success=false to try fallback_tools (Claude→Grok→AG)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "state": {"type": "object"},
                    "stage_id": {"type": "string"},
                    "result_text": {"type": "string"},
                    "success": {"type": "boolean", "default": True},
                    "error": {"type": "string"},
                    "auto_local": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "orchestrate_get_run",
            "description": "Fetch run state by run_id (same process only).",
            "inputSchema": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "orchestrate_fallback_chains",
            "description": "Show default capability→fallback leaf tool chains.",
            "inputSchema": _empty(),
        },
        {
            "name": "orchestrate_advise",
            "description": (
                "ROUTING BRIEF for the host model. Returns available leaves with their LATEST "
                "confirmed model ids and strength guide (what to route where), doc-class policies, "
                "recipes, and what the host should do itself. Call this first, then YOU decide the plan."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "available_tools": {"type": "array", "items": {"type": "string"},
                                        "description": "Leaf tool names present in the session (optional filter)."},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "orchestrate_step",
            "description": (
                "Prepare ONE delegated leaf call the way YOU planned it: resolves the leaf + latest "
                "model, injects deterministic context (gather facts/code/git), prior findings, and the "
                "doc-class policy. Returns {tool, arguments, fallback_tools, verify_after}. You then call "
                "that leaf tool and (if verify_after) pass the result to orchestrate_verify."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "capability": {"type": "string",
                                   "enum": ["chat", "write", "grounded_search", "image", "review_diff", "release", "compare"]},
                    "instruction": {"type": "string", "description": "What this leaf should do."},
                    "doc_class": {"type": "string", "enum": sorted(policy.DOC_CLASSES.keys()), "default": "direct"},
                    "leaf": {"type": "string", "description": "Force a specific leaf tool (else resolved from capability)."},
                    "model": {"type": "string", "description": "Force a model (else the latest known for that leaf)."},
                    "write_task": {"type": "string", "description": "For capability=write (readme, pr-description, translate, …)."},
                    "gather": {"type": "string", "enum": ["facts", "code", "git"],
                               "description": "Inject deterministic project context into the call."},
                    "project_root": {"type": "string", "default": "."},
                    "context": {"type": "string", "description": "Prior findings / source text to pass forward."},
                    "extra_args": {"type": "object", "description": "Extra leaf args (target_language, models, aspect_ratio, …)."},
                },
                "required": ["instruction"],
                "additionalProperties": False,
            },
        },
        {
            "name": "orchestrate_verify",
            "description": "Guardrail: check a produced text for hallucinated tools, recency/session-diary tone, and git-internals vs a doc_class. Returns warnings so YOU can decide whether to re-delegate.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "doc_class": {"type": "string", "enum": sorted(policy.DOC_CLASSES.keys()), "default": "durable"},
                    "project_root": {"type": "string", "description": "If given, gathers facts to check tool names against.", "default": "."},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
        {
            "name": "orchestrate_probe_models",
            "description": "Live-confirm the latest working model id per leaf (tiny ping). Leaf catalogs are stale; this is the source of truth. Needs leaves.json configured.",
            "inputSchema": _empty(),
        },
        {
            "name": "orchestrate_run",
            "description": (
                "AUTONOMOUS (opt-in): run a recipe end-to-end. The broker spawns the "
                "configured leaf MCP servers itself and returns the final artifact — no "
                "per-step next_action loop. Requires leaves.json (see orchestrate_check_leaves). "
                "Each leaf still enforces its own consent/auth."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recipe_id": {"type": "string"},
                    "prompt": {"type": "string"},
                    "instruction": {"type": "string"},
                    "project_root": {"type": "string", "default": "."},
                    "bindings": {"type": "object", "additionalProperties": {"type": "string"}},
                    "max_leaf_calls": {"type": "integer", "minimum": 1, "maximum": 100, "default": 24},
                    "per_call_timeout": {"type": "number", "minimum": 5, "maximum": 600, "default": 180},
                },
                "required": ["recipe_id"],
                "description": "Extra properties forward to the leaf tools (source_text, models, …).",
                "additionalProperties": True,
            },
        },
        {
            "name": "orchestrate_check_leaves",
            "description": "Preflight the autonomous broker: spawn each configured leaf and list its tools.",
            "inputSchema": _empty(),
        },
        {
            "name": "orchestrate_resolve_bindings",
            "description": (
                "Given the leaf tools currently connected (pass names from the client's "
                "tools/list), resolve primary + fallback per capability and list which "
                "recipes are runnable vs blocked. Enables dynamic capability discovery."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "available_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Leaf tool names present in the Codex session.",
                    },
                    "bindings": {"type": "object", "additionalProperties": {"type": "string"}},
                },
                "additionalProperties": False,
            },
        },
    ]


def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Spread the payload FIRST, then set canonical fields LAST so a payload that carries
    # its own "text" key (verify → "verify ok", gather → fact-pack text) can't clobber
    # the full JSON serialization the stdio content[] and supervised handoff rely on.
    return {
        "success": True,
        "provider": "orchestrate",
        "backend": "supervised-local",
        **payload,
        "warnings": payload.get("warnings") if isinstance(payload.get("warnings"), list) else [],
        "text": json.dumps(payload, ensure_ascii=False, indent=2)[:120000],
    }


# Reserved control keys are consumed by the orchestrator itself; everything else
# a caller passes flows through to the leaf tool (source_text, target_language,
# models, version, aspect_ratio, focus, …) so new domains need no schema change.
_CONTROL_KEYS = frozenset(
    {"recipe_id", "bindings", "project_root", "auto_local", "run_id", "state",
     "stage_id", "result_text", "success", "error", "max_leaf_calls", "per_call_timeout"}
)


def _passthrough_args(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in arguments.items() if k not in _CONTROL_KEYS and v is not None}


def dispatch_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if name == "orchestrate_list_recipes":
            return _ok({"recipes": recipes.list_recipes()})
        if name == "orchestrate_explain_recipe":
            return _ok(recipes.explain_recipe(str(arguments.get("recipe_id") or "")))
        if name == "orchestrate_context_policy":
            return _ok(policy.get_policy(str(arguments.get("doc_class") or "")))
        if name == "orchestrate_plan_recipe":
            rid = str(arguments.get("recipe_id") or "")
            bindings = arguments.get("bindings") if isinstance(arguments.get("bindings"), dict) else None
            plan = recipes.plan_recipe(rid, args=_passthrough_args(arguments), bindings=bindings)
            plan["note"] = (
                "Static plan. Prefer orchestrate_start_run for stateful supervised execution (v0.2)."
            )
            return _ok(plan)
        if name == "orchestrate_start_run":
            rid = str(arguments.get("recipe_id") or "")
            bindings = arguments.get("bindings") if isinstance(arguments.get("bindings"), dict) else None
            return _ok(
                runner.start_run(
                    rid,
                    args=_passthrough_args(arguments),
                    bindings=bindings,
                    project_root=str(arguments.get("project_root") or "."),
                    auto_local=bool(arguments.get("auto_local", True)),
                )
            )
        if name == "orchestrate_continue_recipe":
            return _ok(
                runner.continue_run(
                    run_id=str(arguments.get("run_id") or ""),
                    state=arguments.get("state") if isinstance(arguments.get("state"), dict) else None,
                    stage_id=str(arguments.get("stage_id") or ""),
                    result_text=str(arguments.get("result_text") or ""),
                    success=bool(arguments.get("success", True)),
                    error=str(arguments.get("error") or ""),
                    auto_local=bool(arguments.get("auto_local", True)),
                )
            )
        if name == "orchestrate_get_run":
            return _ok(runner.get_run(str(arguments.get("run_id") or "")))
        if name == "orchestrate_fallback_chains":
            return _ok({"fallback_chains": runner.FALLBACK_CHAINS, "default_bindings": recipes.DEFAULT_BINDINGS})
        if name == "orchestrate_advise":
            avail = arguments.get("available_tools")
            avail = [str(t) for t in avail] if isinstance(avail, list) else None
            return _ok(
                {
                    "you_are_the_orchestrator": (
                        "Decide the plan yourself: which sub-tasks to run, which model gets each, "
                        "and what to do directly. Delegate via orchestrate_step; verify durable "
                        "outputs via orchestrate_verify."
                    ),
                    "do_directly": catalog.DO_DIRECTLY,
                    "capabilities": catalog.capabilities(avail),
                    "latest_models": catalog.LATEST_MODELS,
                    "doc_classes": {k: policy.get_policy(k) for k in sorted(policy.DOC_CLASSES)},
                    "recipes": recipes.list_recipes(),
                    "bindings": runner.resolve_bindings(avail),
                }
            )
        if name == "orchestrate_step":
            return _ok(
                runner.prepare_step(
                    capability=str(arguments.get("capability") or "chat"),
                    instruction=str(arguments.get("instruction") or ""),
                    doc_class=str(arguments.get("doc_class") or "direct"),
                    model=arguments.get("model") or None,
                    leaf=arguments.get("leaf") or None,
                    write_task=arguments.get("write_task") or None,
                    gather_kind=arguments.get("gather") or None,
                    project_root=str(arguments.get("project_root") or "."),
                    context=arguments.get("context") or None,
                    extra_args=arguments.get("extra_args") if isinstance(arguments.get("extra_args"), dict) else None,
                )
            )
        if name == "orchestrate_verify":
            root = str(arguments.get("project_root") or ".")
            try:
                fact_pack = gather.gather_durable_facts(root)
            except (ValueError, OSError):
                fact_pack = None
            return _ok(
                verify.verify_text(
                    str(arguments.get("text") or ""),
                    doc_class=str(arguments.get("doc_class") or "durable"),
                    fact_pack=fact_pack if isinstance(fact_pack, dict) else None,
                )
            )
        if name == "orchestrate_probe_models":
            return _ok(broker.probe_models())
        if name == "orchestrate_run":
            rid = str(arguments.get("recipe_id") or "")
            binds = arguments.get("bindings") if isinstance(arguments.get("bindings"), dict) else None
            return _ok(
                broker.run_auto(
                    rid,
                    args=_passthrough_args(arguments),
                    bindings=binds,
                    project_root=str(arguments.get("project_root") or "."),
                    max_leaf_calls=int(arguments.get("max_leaf_calls") or broker.DEFAULT_MAX_LEAF_CALLS),
                    per_call_timeout=float(arguments.get("per_call_timeout") or broker.DEFAULT_PER_CALL_TIMEOUT),
                )
            )
        if name == "orchestrate_check_leaves":
            return _ok(broker.check_leaves())
        if name == "orchestrate_resolve_bindings":
            avail = arguments.get("available_tools")
            avail = [str(t) for t in avail] if isinstance(avail, list) else None
            binds = arguments.get("bindings") if isinstance(arguments.get("bindings"), dict) else None
            return _ok(runner.resolve_bindings(avail, bindings=binds))
        raise ValueError(f"unknown tool: {name}")
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "provider": "orchestrate",
            "backend": "supervised-local",
            "text": str(exc),
            "error": str(exc),
            "error_type": type(exc).__name__,
            "warnings": [],
        }


_RUN_URI_PREFIX = "orchestrate://run/"


def prompt_definitions() -> List[Dict[str, Any]]:
    """Expose each recipe as an MCP prompt so Codex can surface them in its UI."""
    prompts = []
    for r in recipes.list_recipes():
        prompts.append(
            {
                "name": r["id"],
                "description": f"[{r['doc_class']}] {r['description']}",
                "arguments": [
                    {"name": "prompt", "description": "The request / instruction.", "required": True},
                    {"name": "project_root", "description": "Workspace root.", "required": False},
                ],
            }
        )
    return prompts


def get_prompt(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    recipe = recipes.get_recipe(name)  # raises on unknown
    prompt = str(arguments.get("prompt") or "")
    project_root = str(arguments.get("project_root") or ".")
    text = (
        f"Start the '{recipe['id']}' orchestration recipe (doc_class={recipe['doc_class']}).\n"
        f"Call orchestrate_start_run with recipe_id='{recipe['id']}', "
        f"prompt={prompt!r}, project_root={project_root!r}, then follow each next_action.\n"
        f"Recipe: {recipe['description']}"
    )
    return {
        "description": recipe["description"],
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }


def _run_ids() -> List[str]:
    ids = set(runner._RUNS.keys()) | set(store.list_run_ids())
    return sorted(ids)


def resource_list() -> List[Dict[str, Any]]:
    out = []
    for rid in _run_ids():
        out.append(
            {
                "uri": f"{_RUN_URI_PREFIX}{rid}",
                "name": f"run {rid}",
                "description": "Supervised orchestration run state",
                "mimeType": "application/json",
            }
        )
    return out


def resource_read(uri: str) -> Dict[str, Any]:
    if not uri.startswith(_RUN_URI_PREFIX):
        raise RpcError(-32602, f"unknown resource uri: {uri}")
    rid = uri[len(_RUN_URI_PREFIX):]
    state = runner.get_run(rid)  # raises on unknown
    return {
        "contents": [
            {"uri": uri, "mimeType": "application/json", "text": json.dumps(state, ensure_ascii=False)}
        ]
    }


def handle_request(message: Dict[str, Any]) -> Dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None:
        return None
    method = message.get("method")
    try:
        if method == "initialize":
            params = message.get("params") or {}
            requested = str(params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION)
            selected = requested if requested in LEGACY_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
            result = {
                "protocolVersion": selected,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                    "resources": {"listChanged": True},
                },
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_definitions()}
        elif method == "prompts/list":
            result = {"prompts": prompt_definitions()}
        elif method == "prompts/get":
            params = message.get("params") or {}
            result = get_prompt(str(params.get("name") or ""), params.get("arguments") or {})
        elif method == "resources/list":
            result = {"resources": resource_list()}
        elif method == "resources/read":
            params = message.get("params") or {}
            result = resource_read(str(params.get("uri") or ""))
        elif method == "tools/call":
            params = message.get("params") or {}
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise RpcError(-32602, "tool arguments must be an object")
            if name not in {t["name"] for t in tool_definitions()}:
                raise RpcError(-32602, f"unknown tool: {name}")
            payload = dispatch_tool(name, arguments)
            # MCP-compliant tools/call result (content[] + isError), with the structured
            # payload retained as top-level fields for stateful supervised handoff.
            text = payload.get("text")
            if not isinstance(text, str):
                text = json.dumps(payload, ensure_ascii=False)
            result = {
                "content": [{"type": "text", "text": text}],
                "isError": not bool(payload.get("success", True)),
                **payload,
            }
        else:
            raise RpcError(-32601, f"unsupported method: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except RpcError as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": str(exc)}}
    except Exception as exc:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}}


def serve() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("id") is None and message.get("method"):
            continue
        response = handle_request(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
