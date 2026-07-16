"""Autonomous broker: run a recipe end-to-end by invoking leaf MCP servers directly.

Opt-in alternative to the supervised flow. Reuses the exact same `runner` state
machine — it just plays the role Codex plays in supervised mode, calling each
leaf via a spawned MCP client and feeding results back through `continue_run`
(so fallback rotation, the verify→revise loop, and error classification all still
apply). Each leaf enforces its own consent/auth; the broker cannot bypass it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import catalog, errors
from . import leaves as leaves_mod
from . import runner
from .leaf_client import LeafClient, LeafError

DEFAULT_MAX_LEAF_CALLS = 24
DEFAULT_PER_CALL_TIMEOUT = 180.0


def _matched_key(tool: str, reg: Dict[str, Dict[str, Any]]) -> Optional[str]:
    if tool in reg:
        return tool
    candidates = [k for k in reg if tool.startswith(k)]
    return max(candidates, key=len) if candidates else None


def _get_client(
    tool: str, reg: Dict[str, Dict[str, Any]], clients: Dict[str, LeafClient], timeout: float
) -> Tuple[Optional[LeafClient], str]:
    key = _matched_key(tool, reg)
    if key is None:
        return None, f"no leaf launch config for '{tool}'"
    if key in clients:
        return clients[key], ""
    spec = reg[key]
    try:
        client = LeafClient(
            name=key,
            command=str(spec["command"]),
            args=[str(a) for a in (spec.get("args") or [])],
            cwd=spec.get("cwd"),
            env=spec.get("env"),
            default_timeout=timeout,
        )
    except LeafError as exc:
        return None, str(exc)
    clients[key] = client
    return client, ""


def run_auto(
    recipe_id: str,
    *,
    args: Optional[Dict[str, Any]] = None,
    bindings: Optional[Dict[str, str]] = None,
    project_root: str = ".",
    max_leaf_calls: int = DEFAULT_MAX_LEAF_CALLS,
    per_call_timeout: float = DEFAULT_PER_CALL_TIMEOUT,
    leaves: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    reg = leaves_mod.load_leaves() if leaves is None else leaves
    if not reg:
        return {
            "ok": False,
            "error": "no leaf servers configured",
            "hint": "Create ~/.orchestrate_codex/leaves.json (or set ORCHESTRATE_CODEX_LEAVES), "
                    "or use the supervised orchestrate_start_run flow instead.",
        }

    state = runner.start_run(recipe_id, args=args or {}, bindings=bindings, project_root=project_root)
    clients: Dict[str, LeafClient] = {}
    trace: List[Dict[str, Any]] = []
    calls = 0
    guard = 0
    guard_max = max_leaf_calls * 3 + len(state.get("steps") or []) + 8
    try:
        while True:
            guard += 1
            if guard > guard_max:
                break
            na = state.get("next_action") or {}
            typ = na.get("type")
            if typ in {"done", "failed"}:
                break
            if typ == "call_tool":
                stage = str(na.get("stage_id") or "")
                tool = str(na.get("tool") or "")
                if calls >= max_leaf_calls:
                    state = runner.continue_run(
                        run_id=state["run_id"], stage_id=stage, success=False,
                        error="max_leaf_calls exceeded",
                    )
                    trace.append({"stage": stage, "tool": tool, "ok": False, "error": "max_leaf_calls"})
                    continue
                client, err = _get_client(tool, reg, clients, per_call_timeout)
                if client is None:
                    # Unconfigured/unspawnable leaf → report as a leaf failure so the
                    # runner rotates to the next fallback tool.
                    state = runner.continue_run(
                        run_id=state["run_id"], stage_id=stage, success=False, error=err
                    )
                    trace.append({"stage": stage, "tool": tool, "ok": False, "error": err})
                    continue
                calls += 1
                try:
                    ok, text = client.call_tool(tool, na.get("arguments") or {}, timeout=per_call_timeout)
                except LeafError as exc:
                    ok, text = False, str(exc)
                # A leaf may hand back a transport/backend error (HTTP 503, connection
                # refused) as "successful" text — treat that as a failure so the runner
                # rotates to a fallback instead of using the error string as content.
                soft_error = ok and errors.looks_like_leaf_error(text)
                if soft_error:
                    ok = False
                trace.append({"stage": stage, "tool": tool, "ok": ok,
                              "result_chars": len(text), "soft_error": soft_error})
                state = runner.continue_run(
                    run_id=state["run_id"], stage_id=stage,
                    result_text=text if ok else "", success=ok, error="" if ok else text,
                )
                continue
            # local / continue: advance the state machine (local stages auto-run)
            state = runner.continue_run(run_id=state["run_id"], stage_id=str(na.get("stage_id") or ""))
    finally:
        for client in clients.values():
            client.close()

    artifacts = state.get("artifacts") or {}
    artifact = str(artifacts.get("draft") or artifacts.get("last_leaf_text") or "")
    return {
        "ok": state.get("status") == "completed",
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "artifact": artifact,
        "leaf_calls": calls,
        "trace": trace,
        "warnings": state.get("warnings", []),
        "error": state.get("error", ""),
        "state": state,
    }


def probe_models(leaves: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Live-confirm the latest model id per leaf with a tiny ping call.

    Leaf list_models catalogs are stale; this tests catalog.PROBE_CANDIDATES in order
    and reports the first that actually works — the real source of truth for latest ids.
    """
    reg = leaves_mod.load_leaves() if leaves is None else leaves
    if not reg:
        return {"configured": False, "confirmed": catalog.LATEST_MODELS,
                "note": "No leaves configured; returning last-known ids (unverified)."}
    confirmed: Dict[str, str] = {}
    detail: List[Dict[str, Any]] = []
    for leaf_tool, candidates in catalog.PROBE_CANDIDATES.items():
        key = _matched_key(leaf_tool, reg)
        if key is None:
            continue
        spec = reg[key]
        picked = None
        tried = []
        try:
            client = LeafClient(key, str(spec["command"]), [str(a) for a in spec.get("args", [])],
                                cwd=spec.get("cwd"), env=spec.get("env"), default_timeout=60)
            try:
                for cand in candidates:
                    ok, _ = client.call_tool(leaf_tool, {"prompt": "ping", "model": cand, "max_tokens": 5}, timeout=45)
                    tried.append({"model": cand, "ok": ok})
                    if ok:
                        picked = cand
                        break
            finally:
                client.close()
        except LeafError as exc:
            detail.append({"leaf": leaf_tool, "error": str(exc)})
            continue
        if picked:
            confirmed[leaf_tool] = picked
        detail.append({"leaf": leaf_tool, "confirmed": picked, "tried": tried})
    return {"configured": True, "confirmed": confirmed, "detail": detail}


def check_leaves(leaves: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Preflight: spawn each configured leaf, list its tools, report reachability."""
    reg = leaves_mod.load_leaves() if leaves is None else leaves
    results = []
    for key, spec in reg.items():
        entry: Dict[str, Any] = {"key": key, "command": spec.get("command")}
        try:
            client = LeafClient(
                name=key,
                command=str(spec["command"]),
                args=[str(a) for a in (spec.get("args") or [])],
                cwd=spec.get("cwd"),
                env=spec.get("env"),
                default_timeout=30.0,
            )
            try:
                entry["ok"] = True
                entry["tools"] = client.list_tools(timeout=20.0)
            finally:
                client.close()
        except LeafError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
        results.append(entry)
    return {"configured": bool(reg), "leaves": results}
