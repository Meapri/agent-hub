"""Phase-1 unified server: fan-in multiplexer parity + routing."""

from __future__ import annotations

import json

from agent_hub import server
from claude_codex import mcp_server as claude
from grok_codex import mcp_server as grok
from google_antigravity_codex import mcp_server as antigravity
from orchestrate_codex import mcp_server as orchestrate

_OWNERS = [orchestrate, claude, grok, antigravity]


def test_merged_tool_list_is_union_with_stable_names():
    unified = {t["name"] for t in server.tool_definitions()}
    expected = set()
    for mod in _OWNERS:
        expected |= {t["name"] for t in mod.tool_definitions()}
    assert unified == expected
    # every name globally unique (no collisions collapsed the count)
    assert len(server.tool_definitions()) == sum(len(m.tool_definitions()) for m in _OWNERS)


def test_every_tool_routes_to_its_owner():
    from agent_hub.providers.grok import grok_provider
    from agent_hub.providers.claude import claude_provider
    from agent_hub.providers.orchestrate import orchestrate_provider
    # grok is adapter-owned; the rest are still delegated to their module.
    for mod in [antigravity]:
        for spec in mod.tool_definitions():
            assert server._REGISTRY[spec["name"]] is mod
    for spec in grok.tool_definitions():
        assert server._REGISTRY[spec["name"]] is grok_provider
    for spec in claude.tool_definitions():
        assert server._REGISTRY[spec["name"]] is claude_provider
    for spec in orchestrate.tool_definitions():
        assert server._REGISTRY[spec["name"]] is orchestrate_provider


def _call(mod_or_server, name, args):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": args}}
    return mod_or_server.handle_request(msg)


def test_toolscall_is_byte_equal_to_legacy_for_readonly_tools():
    # Representative read-only tool per package (no network / no consent side effects).
    cases = [
        (orchestrate, "orchestrate_list_recipes", {}),
        (claude, "claude_codex_consent_status", {}),
        (grok, "grok_codex_consent_status", {}),
        (antigravity, "google_antigravity_consent_status", {}),
    ]
    for owner, name, args in cases:
        via_server = _call(server, name, args)
        via_legacy = _call(owner, name, args)
        assert json.dumps(via_server, sort_keys=True) == json.dumps(via_legacy, sort_keys=True), name


def test_unknown_tool_errors():
    resp = _call(server, "does_not_exist", {})
    assert resp["error"]["code"] == -32602


def test_initialize_and_list_and_prompts():
    init = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2024-11-05"}})
    assert init["result"]["serverInfo"]["name"] == "agent-hub"
    assert init["result"]["capabilities"]["resources"]["listChanged"] is True
    # prompts/resources delegate to orchestrate
    prompts = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "prompts/list"})
    assert prompts == orchestrate.handle_request({"jsonrpc": "2.0", "id": 2, "method": "prompts/list"})
