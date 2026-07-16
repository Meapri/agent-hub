from __future__ import annotations

from unittest.mock import patch

import pytest

from claude_codex import auth, chat, models, security
from claude_codex.mcp_server import dispatch_tool, handle_request, tool_definitions


def chat_fake_response(body):
    return {
        "id": "msg_test",
        "model": (body or {}).get("model", "claude-sonnet-4-6"),
        "content": [{"type": "text", "text": "hello from mock"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }


def test_tool_definitions_include_chat():
    names = {t["name"] for t in tool_definitions()}
    assert "claude_codex_chat" in names
    assert "claude_codex_list_models" in names
    assert "claude_codex_doctor" in names


def test_consent_gate_blocks_chat(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("CLAUDE_CODEX_USER_CONSENT", raising=False)
    with pytest.raises(RuntimeError, match="consent"):
        chat.run_chat({"prompt": "hi"})


def test_consent_grant_and_status(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("CLAUDE_CODEX_USER_CONSENT", raising=False)
    assert security.user_consent_enabled() is False
    security.grant_consent()
    assert security.user_consent_enabled() is True
    st = security.consent_status()
    assert st["user_consent"] is True


def test_chat_builds_and_parses(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CODEX_USER_CONSENT", "1")
    monkeypatch.setenv(auth.API_KEY_ENV, "test-key-not-real")

    def fake_request(method, url, headers, body, timeout):
        return chat_fake_response(body)

    with patch.object(chat.api, "http_json", side_effect=fake_request):
        result = chat.run_chat({"prompt": "hello", "model": models.DEFAULT_MODEL})
    assert result["success"] is True
    assert "hello" in result["text"]


def test_mcp_initialize_and_tools_list():
    init = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
    )
    assert init["result"]["serverInfo"]["name"]
    listed = handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert any(t["name"] == "claude_codex_chat" for t in listed["result"]["tools"])


def test_dispatch_doctor(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CODEX_USER_CONSENT", "1")
    out = dispatch_tool("claude_codex_doctor", {})
    assert "consent" in out


def test_to_anthropic_system_split():
    system, msgs = chat.to_anthropic_messages(
        [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    )
    assert "be brief" in system
    assert msgs[0]["role"] == "user"


def test_subscription_fingerprint_adds_billing_header():
    from claude_codex import subscription_fingerprint as fp

    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 16,
        "system": "Be brief.",
        "messages": [{"role": "user", "content": "hello world test"}],
    }
    out, headers = fp.apply_subscription_fingerprint(body)
    assert isinstance(out["system"], list)
    assert out["system"][0]["text"].startswith("x-anthropic-billing-header:")
    assert any("Claude Code" in (b.get("text") or "") for b in out["system"] if isinstance(b, dict))
    assert headers.get("x-stainless-lang") == "js"


def test_auth_prefers_subscription_when_token(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CODEX_AUTH_MODE", "subscription")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from claude_codex import auth, subscription_auth

    monkeypatch.setattr(
        subscription_auth,
        "resolve_access_token",
        lambda: {"access_token": "sk-ant-oat-test", "mode": "subscription_oauth", "source": "test"},
    )
    ctx = auth.resolve_auth()
    assert ctx["mode"] == "subscription_oauth"
