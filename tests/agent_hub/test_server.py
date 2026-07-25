"""Canonical Agent Hub MCP server contracts."""

from __future__ import annotations

import ast
import inspect

import pytest

from agent_hub import server
from agent_hub import operations
from claude_codex import mcp_server as claude
from grok_codex import mcp_server as grok
from google_antigravity_codex import mcp_server as antigravity
from openai_codex import mcp_server as openai
from orchestrate_codex import mcp_server as orchestrate

_OWNERS = [orchestrate, claude, grok, antigravity, openai]


def test_only_canonical_tools_are_public_or_callable():
    canonical = {t["name"] for t in operations.tool_definitions()}
    assert {t["name"] for t in server.tool_definitions()} == canonical
    assert len(canonical) == 37
    assert set(server._REGISTRY) == canonical

    for old_name in (
        "orchestrate_list_recipes",
        "claude_codex_chat",
        "grok_codex_chat",
        "google_antigravity_chat",
        "openai_codex_chat",
    ):
        assert _call(server, old_name, {})["error"]["code"] == -32602


def test_workflows_resolve_internal_leaf_adapters_without_public_tools():
    from agent_hub.core.inprocess import make_resolver

    resolve = make_resolver()
    assert resolve("claude_codex_chat") is not None
    assert resolve("grok_codex_chat") is not None
    assert resolve("google_antigravity_chat") is not None
    assert resolve("openai_codex_chat") is not None
    assert resolve("orchestrate_list_recipes") is None


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
    # Canonical tools/call results receive modern completion metadata.
    msg = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "agent_hub_list_workflows", "arguments": {}, "_meta": meta},
    }
    called = server.handle_request(msg)
    assert called["result"]["resultType"] == "complete"
    assert called["result"]["structuredContent"]["success"] is True
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
    for name in ["agent_hub_list_workflows", "agent_hub_get_workflow"]:
        args = {"workflow_id": "repo_document"} if name.endswith("get_workflow") else {}
        r = _call(server, name, args)["result"]
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
        "adaptive",
    }

    failed = _call(
        server,
        "agent_hub_auth_complete",
        {"provider": "claude"},
    )["result"]
    assert failed["isError"] is True
    assert failed["structuredContent"]["success"] is False
    assert failed["structuredContent"]["error"]["message"]

    start = _call(
        server,
        "agent_hub_auth_start",
        {"provider": "gpt"},
    )["result"]
    assert start["isError"] is True
    assert start["structuredContent"]["success"] is False
    assert start["structuredContent"]["error"]["type"] == "provider_gui_required"


def test_invalid_run_id_is_a_canonical_tool_error_not_a_jsonrpc_error():
    response = _call(
        server,
        "agent_hub_get_run",
        {"run_id": "../../outside"},
    )

    assert "error" not in response
    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["success"] is False
    assert result["structuredContent"]["error"]["type"] == "ValueError"


def test_canonical_jsonrpc_rejects_v2_fixed_state_without_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    state = operations.runner.start_run(
        "direct_chat",
        args={"prompt": "hello"},
        project_root=str(tmp_path),
    )
    state.pop("run_id")

    response = _call(
        server,
        "agent_hub_continue_workflow",
        {
            "state": state,
            "stage_id": "chat",
            "result_text": "done",
            "success": True,
        },
    )

    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["type"] == "ValueError"


def test_canonical_jsonrpc_rejects_explicit_legacy_v1_state_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    state = operations.runner.start_run(
        "direct_chat",
        args={"prompt": "hello"},
        project_root=str(tmp_path),
    )
    for key in ("run_id", "run_kind", "state_schema_version", "store_revision", "handoff"):
        state.pop(key, None)
    state["state_schema_version"] = 1

    response = _call(
        server,
        "agent_hub_continue_workflow",
        {
            "state": state,
            "stage_id": "chat",
            "result_text": "done",
            "success": True,
        },
    )

    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["type"] == "ValueError"


def test_canonical_fixed_continue_enforces_expected_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    state = operations.runner.start_run(
        "direct_chat",
        args={"prompt": "hello"},
        project_root=str(tmp_path),
    )
    first = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {
            "run_id": state["run_id"],
            "stage_id": "chat",
            "result_text": "first",
            "expected_revision": 0,
        },
    )
    stale = operations.dispatch_tool(
        "agent_hub_continue_workflow",
        {
            "run_id": state["run_id"],
            "stage_id": "chat",
            "result_text": "stale",
            "expected_revision": 0,
        },
    )

    assert first["success"] is True
    assert stale["success"] is False
    assert stale["error"]["type"] == "run_revision_conflict"
    assert operations.runner.get_run(state["run_id"])["artifacts"]["draft"] == "first"


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
        lambda: {
            "credentials_readable": True,
            "expired": False,
            "token_file_present": True,
            "pending_login": True,
        },
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
    assert state["local_credentials_present"] is True
    assert state["pending_login_present"] is True


def test_gemini_status_probe_uses_read_only_provider_probe(monkeypatch):
    monkeypatch.setattr(
        operations.google_security,
        "consent_status",
        lambda: {"user_consent": True, "agy_session_enabled": True},
    )

    def provider_status(*, probe=False):
        assert probe is True
        return {
            "configured": True,
            "healthy": True,
            "auth_method": "plugin_oauth_login",
        }

    monkeypatch.setattr(operations.google_provider, "status", provider_status)
    monkeypatch.setattr(
        operations.google_oauth,
        "login_status",
        lambda: {
            "credentials_readable": True,
            "expired": False,
        },
    )

    result = operations.dispatch_tool(
        "agent_hub_status",
        {"provider": "gemini", "probe": True},
    )

    state = result["data"]["providers"]["gemini"]
    assert state["authenticated"] is True
    assert state["ready"] is True
    assert state["warnings"] == []


def test_auth_lifecycle_normalizes_ready_credentials_as_logged_in():
    lifecycle = operations._auth_lifecycle(
        account_present=False,
        logged_in=False,
        auth_ready=True,
        refresh_supported=False,
    )

    assert lifecycle == {
        "account_present": True,
        "logged_in": True,
        "auth_ready": True,
        "refresh_supported": False,
        "refreshable": False,
        "relogin_required": False,
    }


@pytest.mark.parametrize(
    ("subscription", "active_mode", "expected_present", "expected_pending"),
    [
        (
            {
                "logged_in": False,
                "token_file_present": False,
                "pending_login_present": False,
            },
            "api_key",
            False,
            False,
        ),
        (
            {
                "logged_in": False,
                "token_file_present": True,
                "pending_login_present": True,
            },
            None,
            True,
            True,
        ),
    ],
)
def test_grok_status_preserves_plugin_owned_local_state(
    monkeypatch,
    tmp_path,
    subscription,
    active_mode,
    expected_present,
    expected_pending,
):
    monkeypatch.setenv("AGENT_HUB_CONFIG_DIR", str(tmp_path / "agent-hub"))
    monkeypatch.setattr(
        operations.grok_security,
        "consent_status",
        lambda: {"user_consent": True},
    )
    monkeypatch.setattr(
        operations.grok_auth,
        "status",
        lambda: {
            "configured": bool(active_mode),
            "ready": bool(active_mode),
            "credentials_present": bool(active_mode),
            "active_mode": active_mode,
            "subscription": subscription,
        },
    )

    result = operations.dispatch_tool(
        "agent_hub_status",
        {"provider": "grok"},
    )

    state = result["data"]["providers"]["grok"]
    assert state["local_credentials_present"] is expected_present
    assert state["pending_login_present"] is expected_pending


def test_all_provider_status_never_calls_credential_refresh(monkeypatch):
    from claude_codex import subscription_auth as claude_subscription

    def forbidden(*_args, **_kwargs):
        pytest.fail("read-only status must not refresh or write credentials")

    monkeypatch.setattr(
        operations.claude_security,
        "consent_status",
        lambda: {"user_consent": True},
    )
    monkeypatch.setattr(
        claude_subscription,
        "status",
        lambda: {
            "logged_in": True,
            "token_valid": False,
            "has_refresh_token": True,
            "mode": "subscription_oauth",
            "source": "test",
        },
    )
    monkeypatch.setattr(operations.claude_auth, "get_api_key", lambda: "")
    monkeypatch.setattr(claude_subscription, "resolve_access_token", forbidden)
    monkeypatch.setattr(claude_subscription, "refresh_token_pure", forbidden)
    monkeypatch.setattr(claude_subscription, "write_credentials", forbidden)

    monkeypatch.setattr(
        operations.grok_security,
        "consent_status",
        lambda: {"user_consent": True},
    )
    monkeypatch.setattr(
        operations.grok_auth.oauth_login,
        "status",
        lambda: {
            "logged_in": True,
            "token_valid": False,
            "has_refresh_token": True,
            "mode": "subscription_oauth",
            "source": "test",
        },
    )
    monkeypatch.setattr(operations.grok_auth, "get_api_key", lambda: "")
    monkeypatch.setattr(
        operations.grok_auth.oauth_login,
        "resolve_access_token",
        forbidden,
    )
    monkeypatch.setattr(
        operations.grok_auth.oauth_login,
        "refresh_tokens",
        forbidden,
    )
    monkeypatch.setattr(
        operations.grok_auth.oauth_login,
        "save_tokens",
        forbidden,
    )

    monkeypatch.setattr(
        operations.google_security,
        "consent_status",
        lambda: {"user_consent": True, "agy_session_enabled": True},
    )
    monkeypatch.setattr(
        operations.google_provider,
        "status",
        lambda probe=False: {
            "configured": False,
            "healthy": False,
            "auth_method": "plugin_oauth_login",
            "error_type": "agy_token_expired",
        },
    )
    monkeypatch.setattr(
        operations.google_oauth,
        "login_status",
        lambda: {
            "token_file_present": True,
            "credentials_readable": True,
            "refresh_token_present": True,
            "expired": True,
        },
    )
    monkeypatch.setattr(
        operations.google_oauth,
        "refresh_access_token",
        forbidden,
    )

    refresh_args = []

    def gpt_status(*, refresh=False):
        refresh_args.append(refresh)
        return {
            "logged_in": True,
            "configured": True,
            "auth_mode": "chatgpt",
            "plan_type": "pro",
        }

    monkeypatch.setattr(
        operations.openai_security,
        "consent_status",
        lambda: {"user_consent": True},
    )
    monkeypatch.setattr(operations.openai_auth, "status", gpt_status)

    result = operations.dispatch_tool(
        "agent_hub_status",
        {"provider": "all", "probe": True},
    )

    assert result["success"] is True
    assert refresh_args == [False]
    assert result["data"]["providers"]["claude"]["warnings"] == [
        "auth_refresh_available"
    ]
    assert result["data"]["providers"]["claude"]["refreshable"] is True
    assert result["data"]["providers"]["grok"]["warnings"] == [
        "auth_refresh_available"
    ]
    assert result["data"]["providers"]["grok"]["refreshable"] is True
    assert result["data"]["providers"]["gemini"]["refreshable"] is True
    assert result["data"]["providers"]["gpt"]["refresh_supported"] is False
    assert result["data"]["providers"]["gpt"]["refreshable"] is False
    status_spec = next(
        item
        for item in operations.tool_definitions()
        if item["name"] == "agent_hub_status"
    )
    assert status_spec["annotations"]["readOnlyHint"] is True
    assert status_spec["annotations"]["idempotentHint"] is True


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
