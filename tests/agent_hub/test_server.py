"""Phase-1 unified server: fan-in multiplexer parity + routing."""

from __future__ import annotations

import ast
import inspect
import json

from agent_hub import server
from agent_hub import operations
from claude_codex import mcp_server as claude
from grok_codex import mcp_server as grok
from google_antigravity_codex import mcp_server as antigravity
from orchestrate_codex import mcp_server as orchestrate

_OWNERS = [orchestrate, claude, grok, antigravity]


def test_tool_surfaces_keep_legacy_callable_without_listing_it(monkeypatch):
    legacy = set()
    for mod in _OWNERS:
        legacy |= {t["name"] for t in mod.tool_definitions()}
    canonical = {t["name"] for t in operations.tool_definitions()}

    monkeypatch.delenv("AGENT_HUB_TOOL_SURFACE", raising=False)
    assert {t["name"] for t in server.tool_definitions()} == canonical
    assert len(canonical) == 26

    monkeypatch.setenv("AGENT_HUB_TOOL_SURFACE", "legacy")
    assert {t["name"] for t in server.tool_definitions()} == legacy

    monkeypatch.setenv("AGENT_HUB_TOOL_SURFACE", "all")
    all_names = {t["name"] for t in server.tool_definitions()}
    assert all_names == canonical | legacy
    assert len(server.tool_definitions()) == len(canonical) + len(legacy)

    # Hidden legacy names remain available to existing clients.
    monkeypatch.setenv("AGENT_HUB_TOOL_SURFACE", "unified")
    assert "grok_codex_consent_status" not in {t["name"] for t in server.tool_definitions()}
    assert _call(server, "grok_codex_consent_status", {})["result"]["content"]


def test_every_tool_routes_to_its_adapter():
    from agent_hub.providers.grok import grok_provider
    from agent_hub.providers.claude import claude_provider
    from agent_hub.providers.orchestrate import orchestrate_provider
    from agent_hub.providers.antigravity import antigravity_provider

    pairs = [
        (orchestrate, orchestrate_provider),
        (claude, claude_provider),
        (grok, grok_provider),
        (antigravity, antigravity_provider),
    ]
    for mod, adapter in pairs:
        for spec in mod.tool_definitions():
            assert server._REGISTRY[spec["name"]] is adapter


def test_canonical_tools_share_one_complete_contract():
    from agent_hub.providers.hub import hub_provider

    specs = operations.tool_definitions()
    assert {spec["name"] for spec in specs} == set(operations.OPERATION_REGISTRY)
    for spec in specs:
        assert server._REGISTRY[spec["name"]] is hub_provider
        assert spec["name"].startswith("agent_hub_")
        assert spec["annotations"].keys() == {
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        }
        assert spec["outputSchema"] == operations.COMMON_OUTPUT_SCHEMA


def _dispatch_names(module):
    """Read the dispatch contract without executing auth, network, or destructive tools."""
    tree = ast.parse(inspect.getsource(module.dispatch_tool))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    names.add(key.value)
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "name"
        ):
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    names.add(comparator.value)
    return names


def test_every_listed_tool_has_a_dispatch_handler():
    # A previous Grok regression exposed login tools through tools/list without
    # wiring them into dispatch_tool. Compare the two contracts statically so
    # auth/network tools do not need to run during this coverage check.
    for module in _OWNERS:
        listed = {spec["name"] for spec in module.tool_definitions()}
        handled = _dispatch_names(module)
        assert listed <= handled, f"{module.__name__}: missing handlers {sorted(listed - handled)}"


def test_modern_protocol_completion_and_discover():
    modern = "2026-07-28"
    meta = {
        "io.modelcontextprotocol/protocolVersion": modern,
        "io.modelcontextprotocol/clientInfo": {"name": "t"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    # antigravity tool under modern: unified == legacy package (byte-equal)
    msg = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "google_antigravity_consent_status", "arguments": {}, "_meta": meta},
    }
    assert json.dumps(server.handle_request(msg), sort_keys=True) == json.dumps(
        antigravity.handle_request(msg), sort_keys=True
    )
    # tools/list under modern gets resultType/ttl (server-wide now, not just antigravity)
    lst = server.handle_request(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {"_meta": meta}}
    )
    assert lst["result"]["resultType"] == "complete"
    assert lst["result"]["ttlMs"] == 300_000
    # server/discover only under modern
    disc = server.handle_request(
        {"jsonrpc": "2.0", "id": 7, "method": "server/discover", "params": {"_meta": meta}}
    )
    assert disc["result"]["serverInfo"]["name"] == "agent-hub"
    assert modern in disc["result"]["supportedVersions"]
    no_meta = server.handle_request({"jsonrpc": "2.0", "id": 8, "method": "server/discover"})
    assert no_meta["error"]["code"] == -32602
    # initialize/ping rejected under modern (stateless)
    init_modern = server.handle_request(
        {"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {"_meta": meta}}
    )
    assert init_modern["error"]["code"] == -32601


def test_streaming_progress_available_to_adapters():
    # A leaf tool call carries the core stream emitter; non-streaming tools ignore it.
    from agent_hub.providers.antigravity import antigravity_provider

    emitted = []
    res = antigravity_provider.call(
        "google_antigravity_consent_status", {}, progress=lambda m, p: emitted.append((m, p))
    )
    assert "content" in res  # ignored progress, normal result
    assert emitted == []


def _call(mod_or_server, name, args):
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    return mod_or_server.handle_request(msg)


def test_every_toolscall_result_is_mcp_compliant():
    # Every tool's tools/call result MUST carry content[] — strict clients (Codex
    # under the modern protocol) reject raw payloads with a response-format error.
    for name in [
        "agent_hub_list_workflows",
        "orchestrate_list_recipes",
        "claude_codex_consent_status",
        "grok_codex_consent_status",
        "google_antigravity_consent_status",
    ]:
        r = _call(server, name, {})["result"]
        assert isinstance(r.get("content"), list) and r["content"], name
        assert r["content"][0].get("type") == "text", name


def test_canonical_results_have_envelope_and_mcp_error_signal():
    listed = _call(server, "agent_hub_list_workflows", {})["result"]
    structured = listed["structuredContent"]
    assert listed["isError"] is False
    assert structured["success"] is True
    assert structured["operation"] == "list_workflows"
    assert {item["id"] for item in structured["data"]["workflows"]} == {
        "repo_document",
        "git_document",
        "research_brief",
        "deep_readme",
    }

    failed = _call(
        server,
        "agent_hub_auth_complete",
        {"provider": "claude"},
    )["result"]
    assert failed["isError"] is True
    assert failed["structuredContent"]["success"] is False
    assert failed["structuredContent"]["error"]["message"]


def test_google_mcp_result_is_unwrapped_before_canonical_envelope():
    wrapped = {
        "content": [{"type": "text", "text": "ok"}],
        "structuredContent": {
            "text": "ok",
            "success": True,
            "model": "gemini-test",
            "usage": {"total_tokens": 3},
        },
        "isError": False,
    }
    raw = operations._unwrap_mcp_result(wrapped)
    result = operations.envelope("chat", raw, provider="gemini")
    assert result["text"] == "ok"
    assert result["model"] == "gemini-test"
    assert result["usage"] == {"total_tokens": 3}

    failed = operations._unwrap_mcp_result(
        {
            "content": [{"type": "text", "text": "bad token"}],
            "structuredContent": {"error_type": "auth"},
            "isError": True,
        }
    )
    assert failed["success"] is False
    assert failed["error"] == "bad token"


def test_gemini_status_treats_unprobed_configured_session_as_ready(monkeypatch):
    monkeypatch.setattr(
        operations.google_security,
        "consent_status",
        lambda: {"user_consent": True, "agy_session_enabled": True},
    )
    monkeypatch.setattr(
        operations.google_oauth,
        "login_status",
        lambda: {"credentials_readable": True, "expired": False},
    )
    monkeypatch.setattr(
        operations.google_provider,
        "status",
        lambda probe=False: {
            "configured": True,
            "healthy": None,
            "auth_method": "plugin_oauth_login",
        },
    )
    result = operations.dispatch_tool("agent_hub_status", {"provider": "gemini", "probe": False})
    state = result["data"]["providers"]["gemini"]
    assert state["authenticated"] is True
    assert state["ready"] is True
    assert state["warnings"] == []


def test_leaf_result_wraps_but_preserves_structured_payload():
    # claude/grok raw payloads are wrapped in content[] but keep their fields on top.
    for owner, name in [
        (claude, "claude_codex_consent_status"),
        (grok, "grok_codex_consent_status"),
    ]:
        wrapped = _call(server, name, {})["result"]
        raw = owner.dispatch_tool(name, {})
        assert "content" in wrapped
        for k, v in raw.items():
            assert wrapped.get(k) == v, f"{name}.{k}"


def test_content_having_providers_stay_byte_equal_to_legacy():
    # orchestrate + antigravity already produced content[]; the adapter path is byte-equal.
    for owner, name in [
        (orchestrate, "orchestrate_list_recipes"),
        (antigravity, "google_antigravity_consent_status"),
    ]:
        assert json.dumps(_call(server, name, {}), sort_keys=True) == json.dumps(
            _call(owner, name, {}), sort_keys=True
        ), name


def test_unknown_tool_errors():
    resp = _call(server, "does_not_exist", {})
    assert resp["error"]["code"] == -32602


def test_initialize_and_list_and_prompts():
    init = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )
    assert init["result"]["serverInfo"]["name"] == "agent-hub"
    assert init["result"]["capabilities"]["resources"]["listChanged"] is True
    # prompts/resources delegate to orchestrate
    prompts = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "prompts/list"})
    assert prompts == orchestrate.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "prompts/list"}
    )
