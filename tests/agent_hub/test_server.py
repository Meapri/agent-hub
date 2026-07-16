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


def test_every_tool_routes_to_its_adapter():
    from agent_hub.providers.grok import grok_provider
    from agent_hub.providers.claude import claude_provider
    from agent_hub.providers.orchestrate import orchestrate_provider
    from agent_hub.providers.antigravity import antigravity_provider
    pairs = [(orchestrate, orchestrate_provider), (claude, claude_provider),
             (grok, grok_provider), (antigravity, antigravity_provider)]
    for mod, adapter in pairs:
        for spec in mod.tool_definitions():
            assert server._REGISTRY[spec["name"]] is adapter


def test_modern_protocol_completion_and_discover():
    modern = "2026-07-28"
    meta = {
        "io.modelcontextprotocol/protocolVersion": modern,
        "io.modelcontextprotocol/clientInfo": {"name": "t"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    # antigravity tool under modern: unified == legacy package (byte-equal)
    msg = {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
           "params": {"name": "google_antigravity_consent_status", "arguments": {}, "_meta": meta}}
    assert (json.dumps(server.handle_request(msg), sort_keys=True)
            == json.dumps(antigravity.handle_request(msg), sort_keys=True))
    # tools/list under modern gets resultType/ttl (server-wide now, not just antigravity)
    lst = server.handle_request({"jsonrpc": "2.0", "id": 6, "method": "tools/list",
                                 "params": {"_meta": meta}})
    assert lst["result"]["resultType"] == "complete"
    assert lst["result"]["ttlMs"] == 300_000
    # server/discover only under modern
    disc = server.handle_request({"jsonrpc": "2.0", "id": 7, "method": "server/discover",
                                  "params": {"_meta": meta}})
    assert disc["result"]["serverInfo"]["name"] == "agent-hub"
    assert modern in disc["result"]["supportedVersions"]
    no_meta = server.handle_request({"jsonrpc": "2.0", "id": 8, "method": "server/discover"})
    assert no_meta["error"]["code"] == -32602
    # initialize/ping rejected under modern (stateless)
    init_modern = server.handle_request({"jsonrpc": "2.0", "id": 9, "method": "initialize",
                                         "params": {"_meta": meta}})
    assert init_modern["error"]["code"] == -32601


def test_streaming_progress_available_to_adapters():
    # A leaf tool call carries the core stream emitter; non-streaming tools ignore it.
    from agent_hub.providers.antigravity import antigravity_provider
    emitted = []
    res = antigravity_provider.call("google_antigravity_consent_status", {},
                                    progress=lambda m, p: emitted.append((m, p)))
    assert "content" in res  # ignored progress, normal result
    assert emitted == []


def _call(mod_or_server, name, args):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": args}}
    return mod_or_server.handle_request(msg)


def test_every_toolscall_result_is_mcp_compliant():
    # Every tool's tools/call result MUST carry content[] — strict clients (Codex
    # under the modern protocol) reject raw payloads with a response-format error.
    for name in ["orchestrate_list_recipes", "claude_codex_consent_status",
                 "grok_codex_consent_status", "google_antigravity_consent_status"]:
        r = _call(server, name, {})["result"]
        assert isinstance(r.get("content"), list) and r["content"], name
        assert r["content"][0].get("type") == "text", name


def test_leaf_result_wraps_but_preserves_structured_payload():
    # claude/grok raw payloads are wrapped in content[] but keep their fields on top.
    for owner, name in [(claude, "claude_codex_consent_status"), (grok, "grok_codex_consent_status")]:
        wrapped = _call(server, name, {})["result"]
        raw = owner.dispatch_tool(name, {})
        assert "content" in wrapped
        for k, v in raw.items():
            assert wrapped.get(k) == v, f"{name}.{k}"


def test_content_having_providers_stay_byte_equal_to_legacy():
    # orchestrate + antigravity already produced content[]; the adapter path is byte-equal.
    for owner, name in [(orchestrate, "orchestrate_list_recipes"),
                        (antigravity, "google_antigravity_consent_status")]:
        assert json.dumps(_call(server, name, {}), sort_keys=True) == \
               json.dumps(_call(owner, name, {}), sort_keys=True), name


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
