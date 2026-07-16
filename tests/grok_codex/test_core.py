from __future__ import annotations

from unittest.mock import patch

import pytest

from grok_codex import auth, chat, models, security
from grok_codex.mcp_server import dispatch_tool, handle_request, tool_definitions


def chat_fake_response(body):
    return {
        "id": "chatcmpl_test",
        "model": (body or {}).get("model", "grok-4"),
        "choices": [{"message": {"role": "assistant", "content": "hello from mock"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }


def test_tool_definitions_include_chat():
    names = {t["name"] for t in tool_definitions()}
    assert "grok_codex_chat" in names
    assert "grok_codex_list_models" in names
    assert "grok_codex_doctor" in names


def test_consent_gate_blocks_chat(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("GROK_CODEX_USER_CONSENT", raising=False)
    with pytest.raises(RuntimeError, match="consent"):
        chat.run_chat({"prompt": "hi"})


def test_consent_grant_and_status(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("GROK_CODEX_USER_CONSENT", raising=False)
    assert security.user_consent_enabled() is False
    security.grant_consent()
    assert security.user_consent_enabled() is True
    st = security.consent_status()
    assert st["user_consent"] is True


def test_chat_builds_and_parses(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    monkeypatch.setenv(auth.API_KEY_ENV, "test-key-not-real")

    seen = {}

    def fake_request(method, url, headers, body, timeout):
        seen.update(body)
        return chat_fake_response(body)

    with patch.object(chat.api, "http_json", side_effect=fake_request):
        result = chat.run_chat({"prompt": "hello", "model": models.DEFAULT_MODEL})
    assert result["success"] is True
    assert "hello" in result["text"]
    assert seen["max_tokens"] == chat.DEFAULT_MAX_TOKENS == 65536


def test_chat_marks_length_response_incomplete(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    monkeypatch.setenv(auth.API_KEY_ENV, "test-key-not-real")
    payload = chat_fake_response({"model": models.DEFAULT_MODEL})
    payload["choices"][0]["finish_reason"] = "length"

    with patch.object(chat.api, "http_json", return_value=payload):
        result = chat.run_chat({"prompt": "hello"})

    assert result["success"] is False
    assert "incomplete_finish_reason:length" in result["warnings"]


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
    assert any(t["name"] == "grok_codex_chat" for t in listed["result"]["tools"])


def test_dispatch_doctor(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    out = dispatch_tool("grok_codex_doctor", {})
    assert "consent" in out


def test_strips_reasoning_effort(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    monkeypatch.setenv(auth.API_KEY_ENV, "test-key")
    seen = {}

    def fake_http(method, url, headers, body, timeout):
        seen["body"] = body
        seen["headers"] = headers
        return chat_fake_response(body)

    with patch.object(chat.api, "http_json", side_effect=fake_http):
        chat.run_chat({"prompt": "hi", "api_mode": "chat"})
    assert "reasoningEffort" not in (seen.get("body") or {})
    assert "x-grok-conv-id" in (seen.get("headers") or {})


def test_oauth_status_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    from grok_codex import oauth_login

    st = oauth_login.status()
    assert st["logged_in"] is False


def test_auth_prefers_oauth_token(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_AUTH_MODE", "subscription")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    from grok_codex import auth, oauth_login

    monkeypatch.setattr(oauth_login, "resolve_access_token", lambda: "oauth-access-token")
    ctx = auth.resolve_auth()
    assert ctx["mode"] == "subscription_oauth"
