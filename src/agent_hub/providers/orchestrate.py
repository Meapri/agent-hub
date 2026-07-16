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


class OrchestrateProvider(Provider):
    name = "orchestrate"

    def tool_specs(self) -> List[Dict[str, Any]]:
        return _orch.tool_definitions()

    def call(self, name: str, arguments: Any, progress=None) -> Dict[str, Any]:
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "tool arguments must be an object")
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
