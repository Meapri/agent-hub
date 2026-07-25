from __future__ import annotations

import json
import stat
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

    seen = {}

    def fake_request(method, url, headers, body, timeout):
        seen.update(body)
        return chat_fake_response(body)

    with patch.object(chat.api, "http_json", side_effect=fake_request):
        result = chat.run_chat({"prompt": "hello", "model": models.DEFAULT_MODEL})
    assert result["success"] is True
    assert "hello" in result["text"]
    assert seen["max_tokens"] == chat.DEFAULT_MAX_TOKENS == 128000


def test_chat_clamps_haiku_output_to_model_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CODEX_USER_CONSENT", "1")
    monkeypatch.setenv(auth.API_KEY_ENV, "test-key-not-real")
    seen = {}

    def fake_request(method, url, headers, body, timeout):
        seen.update(body)
        return chat_fake_response(body)

    with patch.object(chat.api, "http_json", side_effect=fake_request):
        result = chat.run_chat(
            {
                "prompt": "hello",
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 131072,
            }
        )

    assert seen["max_tokens"] == 65536
    assert "max_tokens_clamped_for_model:131072->65536" in result["warnings"]


def test_chat_marks_max_token_response_incomplete(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CODEX_USER_CONSENT", "1")
    monkeypatch.setenv(auth.API_KEY_ENV, "test-key-not-real")
    payload = chat_fake_response({"model": models.DEFAULT_MODEL})
    payload["stop_reason"] = "max_tokens"

    with patch.object(chat.api, "http_json", return_value=payload):
        result = chat.run_chat({"prompt": "hello"})

    assert result["success"] is False
    assert "incomplete_finish_reason:max_tokens" in result["warnings"]


def test_chat_maps_reasoning_effort_to_adaptive_thinking(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CODEX_USER_CONSENT", "1")
    monkeypatch.setenv(auth.API_KEY_ENV, "test-key-not-real")
    seen = {}

    def fake_request(method, url, headers, body, timeout):
        seen.update(body)
        return chat_fake_response(body)

    with patch.object(chat.api, "http_json", side_effect=fake_request):
        chat.run_chat(
            {"prompt": "inspect", "model": "claude-opus-4-8", "reasoning_effort": "high"}
        )

    assert seen["output_config"] == {"effort": "high"}
    assert seen["thinking"] == {"type": "adaptive"}


def test_chat_rejects_reasoning_effort_for_unsupported_model(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CODEX_USER_CONSENT", "1")
    with pytest.raises(ValueError, match="not supported"):
        chat.run_chat(
            {"prompt": "inspect", "model": "claude-haiku-3", "reasoning_effort": "high"}
        )


def test_chat_marks_unexecuted_tool_use_incomplete(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CODEX_USER_CONSENT", "1")
    monkeypatch.setenv(auth.API_KEY_ENV, "test-key-not-real")
    payload = chat_fake_response({"model": models.DEFAULT_MODEL})
    payload["stop_reason"] = "tool_use"
    payload["content"].append(
        {"type": "tool_use", "id": "tool_1", "name": "bash", "input": {"cmd": "ls"}}
    )

    with patch.object(chat.api, "http_json", return_value=payload):
        result = chat.run_chat({"prompt": "review"})

    assert result["success"] is False
    assert result["finish_reason"] == "tool_use"
    assert "incomplete_finish_reason:tool_use" in result["warnings"]


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


def test_auth_status_never_resolves_or_refreshes_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        auth.subscription_auth,
        "status",
        lambda: {
            "logged_in": True,
            "token_valid": False,
            "mode": "subscription_oauth",
            "source": "test",
        },
    )
    monkeypatch.setattr(
        auth,
        "resolve_auth",
        lambda: pytest.fail("status must not resolve or refresh credentials"),
    )

    state = auth.status()

    assert state["credentials_present"] is True
    assert state["configured"] is True
    assert state["ready"] is False
    assert state["active_mode"] is None


def test_strict_refresh_writes_atomically_and_keeps_credentials_private(
    monkeypatch,
    tmp_path,
):
    from claude_codex import subscription_auth

    credential_path = tmp_path / ".claude" / ".credentials.json"
    monkeypatch.setenv("CLAUDE_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CODEX_USER_CONSENT", "1")
    monkeypatch.setattr(
        subscription_auth,
        "credentials_file_path",
        lambda: credential_path,
    )
    monkeypatch.setattr(subscription_auth, "read_from_keychain", lambda: None)
    subscription_auth.write_credentials("old-access", "old-refresh", 1)
    monkeypatch.setattr(
        subscription_auth,
        "refresh_token_pure",
        lambda refresh_token: {
            "access_token": "fresh-access",
            "refresh_token": f"{refresh_token}-rotated",
            "expires_at_ms": 4_102_444_800_000,
        },
    )

    result = subscription_auth.refresh_access_token()

    stored = json.loads(credential_path.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert result["access_token"] == "fresh-access"
    assert stored["accessToken"] == "fresh-access"
    assert stored["refreshToken"] == "old-refresh-rotated"
    assert stat.S_IMODE(credential_path.stat().st_mode) == 0o600
    assert list(credential_path.parent.glob("*.tmp")) == []


def test_strict_refresh_does_not_overwrite_newer_claude_login(
    monkeypatch,
    tmp_path,
):
    from claude_codex import subscription_auth

    credential_path = tmp_path / ".claude" / ".credentials.json"
    monkeypatch.setenv("CLAUDE_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CODEX_USER_CONSENT", "1")
    monkeypatch.setattr(
        subscription_auth,
        "credentials_file_path",
        lambda: credential_path,
    )
    monkeypatch.setattr(subscription_auth, "read_from_keychain", lambda: None)
    subscription_auth.write_credentials("old-access", "old-refresh", 1)

    def refresh_then_relogin(_refresh_token):
        subscription_auth.write_credentials(
            "new-login-access",
            "new-login-refresh",
            4_102_444_800_000,
        )
        return {
            "access_token": "must-not-win",
            "refresh_token": "must-not-win",
            "expires_at_ms": 4_102_444_800_000,
        }

    monkeypatch.setattr(
        subscription_auth,
        "refresh_token_pure",
        refresh_then_relogin,
    )

    result = subscription_auth.refresh_access_token()

    stored = json.loads(credential_path.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert result["access_token"] == "new-login-access"
    assert stored["accessToken"] == "new-login-access"
    assert "must-not-win" not in str(stored)
