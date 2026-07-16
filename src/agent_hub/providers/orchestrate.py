"""Orchestrate (conductor) adapter.

Its tools/call result wraps content[] + isError AND spreads the structured
payload as top-level fields (the supervised host reads run_id/next_action/steps
from there). This reproduces the legacy assembly byte-for-byte: text is the
payload's own ``text`` if a str, else ``json.dumps(payload, ensure_ascii=False)``;
isError = not payload.get("success", True); payload spread LAST.

Note: only tools/call ownership moves here. prompts/list|get and
resources/list|read still delegate to the orchestrate module in the server, and
the autonomous broker (subprocess spawn) is unchanged — the in-process rewrite is
a separate, later step.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from orchestrate_codex import mcp_server as _orch

from agent_hub.core.rpc import RpcError

from .base import Provider


def _run_auto_inprocess(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """orchestrate_run via the broker, but with an IN-PROCESS leaf transport.

    Reproduces the legacy dispatch wrapping (_ok(broker.run_auto(...))) and adds a
    client_resolver so the broker calls sibling adapters in-process instead of
    spawning subprocess leaves. Consent still runs inside each adapter.
    """
    from orchestrate_codex import broker
    from orchestrate_codex.mcp_server import _ok, _passthrough_args
    from agent_hub.core.inprocess import make_resolver

    try:
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
                client_resolver=make_resolver(),
            )
        )
    except Exception as exc:  # noqa: BLE001 — mirror dispatch_tool's uniform error envelope.
        return {
            "success": False,
            "provider": "orchestrate",
            "backend": "supervised-local",
            "text": str(exc),
            "error": str(exc),
            "error_type": type(exc).__name__,
            "warnings": [],
        }


class OrchestrateProvider(Provider):
    name = "orchestrate"

    def tool_specs(self) -> List[Dict[str, Any]]:
        return _orch.tool_definitions()

    def call(self, name: str, arguments: Any, progress=None) -> Dict[str, Any]:
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "tool arguments must be an object")
        if name == "orchestrate_run":
            payload = _run_auto_inprocess(arguments)
        else:
            payload = _orch.dispatch_tool(name, arguments)
        text = payload.get("text")
        if not isinstance(text, str):
            text = json.dumps(payload, ensure_ascii=False)
        return {
            "content": [{"type": "text", "text": text}],
            "isError": not bool(payload.get("success", True)),
            **payload,
        }


orchestrate_provider = OrchestrateProvider()
