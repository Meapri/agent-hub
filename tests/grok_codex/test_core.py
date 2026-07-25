from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
import json
from pathlib import Path
import stat
import threading
import time
import urllib.error
from unittest.mock import patch

import pytest

from grok_codex import auth, chat, models, oauth_login, security
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


def test_consent_regrant_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("GROK_CODEX_USER_CONSENT", raising=False)
    path = security.grant_consent()
    before = path.read_bytes()
    revision = security.consent_revision()

    security.grant_consent()

    assert path.read_bytes() == before
    assert security.consent_revision() == revision


@pytest.mark.parametrize("invalid_version", ["oops", "1", True, 1.9])
def test_consent_invalid_version_is_disabled_and_regrant_repairs(
    monkeypatch,
    tmp_path,
    invalid_version,
):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("GROK_CODEX_USER_CONSENT", raising=False)
    path = security.consent_file_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"accepted": True, "version": invalid_version}),
        encoding="utf-8",
    )

    assert security.user_consent_enabled() is False

    security.grant_consent()

    assert security.user_consent_enabled() is True
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


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


def test_chat_maps_reasoning_effort_to_responses(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    monkeypatch.setenv(auth.API_KEY_ENV, "test-key-not-real")
    seen = {}

    def fake_request(method, url, headers, body, timeout):
        seen["url"] = url
        seen["body"] = body
        return {"model": "grok-4.5", "status": "completed", "output_text": "done"}

    with patch.object(chat.api, "http_json", side_effect=fake_request):
        result = chat.run_chat(
            {"prompt": "inspect", "model": "grok-4.5", "reasoning_effort": "high"}
        )

    assert seen["url"].endswith("/responses")
    assert seen["body"]["reasoning"] == {"effort": "high"}
    assert result["diagnostics"]["reasoning_effort"] == "high"


def test_responses_text_prefers_last_assistant_message_over_progress_output():
    payload = {
        "output_text": "Searching now...Final answer",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Searching now..."}],
            },
            {"type": "web_search_call", "status": "completed"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Final answer"}],
            },
        ],
    }

    assert chat._extract_responses_text(payload) == "Final answer"


def test_chat_rejects_reasoning_effort_for_unsupported_model(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    with pytest.raises(ValueError, match="not supported"):
        chat.run_chat({"prompt": "inspect", "model": "grok-4", "reasoning_effort": "high"})


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


def test_oauth_tools_are_dispatchable(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")

    status = dispatch_tool("grok_codex_login_status", {})
    assert status["success"] is False
    assert status["logged_in"] is False

    with patch.object(oauth_login, "start_login") as start_login:
        started = dispatch_tool("grok_codex_login_start", {})
    assert started["success"] is False
    assert started["error"] == "provider_gui_required"
    assert Path(started["next_action"]["command"]).is_absolute()
    assert Path(started["next_action"]["command"]).name == "agent-hub-connect"
    assert started["next_action"]["args"] == []
    start_login.assert_not_called()

    with patch.object(oauth_login, "complete_login") as complete_login:
        completed = dispatch_tool("grok_codex_login_complete", {})
    assert completed["success"] is False
    assert completed["error"] == "provider_gui_required"
    complete_login.assert_not_called()

    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    oauth_login.token_path().write_text('{"access_token":"test"}', encoding="utf-8")
    logged_out = dispatch_tool("grok_codex_logout", {})
    assert logged_out["success"] is False
    assert logged_out["error"] == "provider_gui_required"
    assert oauth_login.token_path().is_file()


def test_oauth_tool_annotations_match_credential_mutations():
    tools = {item["name"]: item for item in tool_definitions()}

    assert tools["grok_codex_login_status"]["annotations"]["readOnlyHint"] is True
    for name in (
        "grok_codex_login_start",
        "grok_codex_login_complete",
        "grok_codex_logout",
    ):
        assert tools[name]["annotations"]["readOnlyHint"] is True
        assert tools[name]["annotations"]["destructiveHint"] is False
        assert tools[name]["inputSchema"]["properties"] == {}


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
    assert st["token_file_present"] is False
    assert st["pending_login_present"] is False


def test_oauth_status_reports_malformed_local_state(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    oauth_login.token_path().parent.mkdir(parents=True)
    oauth_login.token_path().write_text("{broken", encoding="utf-8")
    oauth_login.pending_path().write_text("{}", encoding="utf-8")

    state = oauth_login.status()

    assert state["logged_in"] is False
    assert state["token_file_present"] is True
    assert state["pending_login_present"] is True


def test_oauth_status_reports_expiry_without_refresh(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    oauth_login.token_path().parent.mkdir(parents=True)
    oauth_login.token_path().write_text(
        (
            '{"access_token":"test","refresh_token":"refresh",'
            '"expires_in":3600,"last_refresh":"2020-01-01T00:00:00Z"}'
        ),
        encoding="utf-8",
    )

    state = oauth_login.status()

    assert state["logged_in"] is True
    assert state["token_valid"] is False
    assert state["refresh_recommended"] is True


def test_oauth_poll_rejects_untrusted_token_endpoint():
    with pytest.raises(RuntimeError, match="refusing non-xAI token_endpoint"):
        oauth_login.poll_device_token(
            token_endpoint="https://auth.x.ai.attacker.example/token",
            device_code="device",
            expires_in=1,
            poll_interval=1,
        )


def test_oauth_http_error_does_not_expose_response_body():
    response_body = BytesIO(
        b'{"error":"server_error","detail":"sentinel-refresh-token"}'
    )
    error = urllib.error.HTTPError(
        oauth_login.XAI_OAUTH_DEVICE_CODE_URL,
        400,
        "Bad Request",
        {},
        response_body,
    )

    with patch.object(
        oauth_login.urllib.request,
        "urlopen",
        side_effect=error,
    ):
        with pytest.raises(RuntimeError) as raised:
            oauth_login._http_json(  # noqa: SLF001
                "POST",
                oauth_login.XAI_OAUTH_DEVICE_CODE_URL,
                form={"client_id": "public-client"},
            )

    assert "HTTP 400" in str(raised.value)
    assert "sentinel-refresh-token" not in str(raised.value)
    assert "server_error" not in str(raised.value)


def test_oauth_poll_rechecks_cancellation_after_successful_response():
    cancel_event = threading.Event()

    def cancelled_response(*_args, **_kwargs):
        cancel_event.set()
        return {
            "access_token": "must-not-be-returned",
            "refresh_token": "must-not-be-returned",
        }

    with patch.object(oauth_login, "_http_json", side_effect=cancelled_response):
        with pytest.raises(RuntimeError, match="cancelled"):
            oauth_login.poll_device_token(
                token_endpoint="https://auth.x.ai/oauth/token",
                device_code="device",
                expires_in=5,
                poll_interval=1,
                cancel_event=cancel_event,
            )


def test_oauth_clear_tokens_also_removes_pending_login(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    oauth_login.token_path().parent.mkdir(parents=True)
    oauth_login.token_path().write_text('{"access_token":"test"}', encoding="utf-8")
    oauth_login.pending_path().write_text('{"device_code":"test"}', encoding="utf-8")

    assert oauth_login.clear_tokens() is True
    assert not oauth_login.token_path().exists()
    assert not oauth_login.pending_path().exists()


def test_oauth_refresh_does_not_recreate_deleted_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    oauth_login.save_tokens(
        {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_in": 3600,
        }
    )
    stored = json.loads(oauth_login.token_path().read_text(encoding="utf-8"))
    stored["last_refresh"] = "2020-01-01T00:00:00Z"
    oauth_login._write_private_json(oauth_login.token_path(), stored)

    def refresh_then_delete(*_args, **_kwargs):
        oauth_login.clear_tokens()
        return {
            "access_token": "must-not-be-saved",
            "refresh_token": "must-not-be-saved",
            "expires_in": 3600,
        }

    monkeypatch.setattr(oauth_login, "refresh_tokens", refresh_then_delete)

    assert oauth_login.resolve_access_token() is None
    assert not oauth_login.token_path().exists()


def test_strict_oauth_refresh_rotates_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    oauth_login.save_tokens(
        {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_in": 3600,
        }
    )
    stored = json.loads(oauth_login.token_path().read_text(encoding="utf-8"))
    stored["last_refresh"] = "2020-01-01T00:00:00Z"
    oauth_login._write_private_json(oauth_login.token_path(), stored)
    monkeypatch.setattr(
        oauth_login,
        "refresh_tokens",
        lambda refresh_token, **_kwargs: {
            "access_token": "fresh-access",
            "refresh_token": f"{refresh_token}-rotated",
            "expires_in": 3600,
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        },
    )

    result = oauth_login.force_refresh_access_token()

    refreshed = json.loads(oauth_login.token_path().read_text(encoding="utf-8"))
    assert result == {"success": True}
    assert refreshed["access_token"] == "fresh-access"
    assert refreshed["refresh_token"] == "old-refresh-rotated"


def test_routine_and_explicit_refresh_share_one_remote_exchange(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    oauth_login.save_tokens(
        {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_in": 3600,
        }
    )
    stored = json.loads(oauth_login.token_path().read_text(encoding="utf-8"))
    stored["last_refresh"] = "2020-01-01T00:00:00Z"
    oauth_login._write_private_json(oauth_login.token_path(), stored)
    exchange_started = threading.Event()
    release_exchange = threading.Event()
    explicit_started = threading.Event()
    calls = []
    results = {}
    errors = []

    def refresh_tokens(refresh_token, **_kwargs):
        calls.append(refresh_token)
        exchange_started.set()
        assert release_exchange.wait(timeout=5)
        return {
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
            "expires_in": 3600,
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        }

    def routine_refresh():
        try:
            results["routine"] = oauth_login.resolve_access_token()
        except Exception as exc:  # noqa: BLE001 - thread result capture
            errors.append(exc)

    def explicit_refresh():
        explicit_started.set()
        try:
            results["explicit"] = oauth_login.force_refresh_access_token()
        except Exception as exc:  # noqa: BLE001 - thread result capture
            errors.append(exc)

    monkeypatch.setattr(oauth_login, "refresh_tokens", refresh_tokens)
    routine = threading.Thread(target=routine_refresh)
    explicit = threading.Thread(target=explicit_refresh)
    routine.start()
    assert exchange_started.wait(timeout=5)
    explicit.start()
    assert explicit_started.wait(timeout=5)
    release_exchange.set()
    routine.join(timeout=5)
    explicit.join(timeout=5)

    assert errors == []
    assert calls == ["old-refresh"]
    assert results["routine"] == "fresh-access"
    assert results["explicit"] == {"success": True, "coalesced": True}


def test_strict_oauth_refresh_does_not_revive_deleted_credentials(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    oauth_login.save_tokens(
        {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_in": 3600,
        }
    )
    stored = json.loads(oauth_login.token_path().read_text(encoding="utf-8"))
    stored["last_refresh"] = "2020-01-01T00:00:00Z"
    oauth_login._write_private_json(oauth_login.token_path(), stored)

    def refresh_then_delete(*_args, **_kwargs):
        oauth_login.clear_tokens()
        return {
            "access_token": "must-not-be-saved",
            "refresh_token": "must-not-be-saved",
            "expires_in": 3600,
        }

    monkeypatch.setattr(oauth_login, "refresh_tokens", refresh_then_delete)

    with pytest.raises(RuntimeError, match="changed"):
        oauth_login.force_refresh_access_token()

    assert not oauth_login.token_path().exists()


def test_oauth_token_and_pending_files_are_written_private(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    oauth_login.save_tokens(
        {
            "access_token": "test",
            "refresh_token": "refresh",
            "expires_in": 3600,
        }
    )
    monkeypatch.setattr(
        oauth_login,
        "discovery",
        lambda: {"token_endpoint": "https://auth.x.ai/oauth/token"},
    )
    monkeypatch.setattr(
        oauth_login,
        "request_device_code",
        lambda: {
            "device_code": "device",
            "user_code": "CODE",
            "verification_uri": "https://accounts.x.ai/device",
            "expires_in": 900,
            "interval": 5,
        },
    )
    started = oauth_login.start_login(open_browser=False)

    assert stat.S_IMODE(oauth_login.token_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(oauth_login.pending_path().stat().st_mode) == 0o600
    assert list(oauth_login.token_path().parent.glob("*.tmp")) == []
    pending = json.loads(oauth_login.pending_path().read_text(encoding="utf-8"))
    assert pending["version"] == oauth_login.PENDING_VERSION
    assert pending["flow_id"] == started["flow_id"]
    assert pending["consent_revision"]


def test_oauth_start_does_not_overwrite_active_pending_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    monkeypatch.setattr(
        oauth_login,
        "discovery",
        lambda: {"token_endpoint": "https://auth.x.ai/oauth/token"},
    )
    monkeypatch.setattr(
        oauth_login,
        "request_device_code",
        lambda: {
            "device_code": "device",
            "user_code": "CODE",
            "verification_uri": "https://accounts.x.ai/device",
            "expires_in": 900,
            "interval": 5,
        },
    )
    oauth_login.start_login(open_browser=False)
    before = oauth_login.pending_path().read_text(encoding="utf-8")

    with pytest.raises(RuntimeError, match="already in progress"):
        oauth_login.start_login(open_browser=False)

    assert oauth_login.pending_path().read_text(encoding="utf-8") == before


def test_oauth_pending_clear_requires_matching_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    monkeypatch.setattr(
        oauth_login,
        "discovery",
        lambda: {"token_endpoint": "https://auth.x.ai/oauth/token"},
    )
    monkeypatch.setattr(
        oauth_login,
        "request_device_code",
        lambda: {
            "device_code": "device",
            "user_code": "CODE",
            "verification_uri": "https://accounts.x.ai/device",
            "expires_in": 900,
            "interval": 5,
        },
    )
    started = oauth_login.start_login(open_browser=False)

    assert oauth_login.clear_pending_login(expected_flow_id="other-flow") is False
    assert oauth_login.pending_path().is_file()
    assert (
        oauth_login.clear_pending_login(expected_flow_id=started["flow_id"])
        is True
    )


@pytest.mark.parametrize(
    "payload",
    [
        "{not-json",
        json.dumps({"created_at": time.time(), "device_code": "legacy"}),
    ],
)
def test_oauth_clear_unusable_pending_recovers_invalid_or_legacy_state(
    monkeypatch,
    tmp_path,
    payload,
):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    oauth_login.pending_path().parent.mkdir(parents=True, exist_ok=True)
    oauth_login.pending_path().write_text(payload, encoding="utf-8")

    assert oauth_login.clear_unusable_pending_login() is True
    assert not oauth_login.pending_path().exists()


def test_oauth_clear_unusable_pending_preserves_active_owned_flow(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    oauth_login._write_private_json(
        oauth_login.pending_path(),
        {
            "version": oauth_login.PENDING_VERSION,
            "flow_id": "owned-flow",
            "consent_revision": security.consent_revision(),
            "created_at": time.time(),
            "expires_in": 900,
        },
    )

    assert oauth_login.clear_unusable_pending_login() is False
    assert oauth_login.pending_path().is_file()


def test_oauth_complete_reports_cleanup_warning_after_token_commit(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    oauth_login._write_private_json(
        oauth_login.pending_path(),
        {
            "version": oauth_login.PENDING_VERSION,
            "flow_id": "owned-flow",
            "consent_revision": security.consent_revision(),
            "created_at": time.time(),
            "expires_in": 900,
            "interval": 1,
            "token_endpoint": "https://auth.x.ai/oauth/token",
            "device_code": "device",
        },
    )
    monkeypatch.setattr(
        oauth_login,
        "poll_device_token",
        lambda **_kwargs: {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 3600,
        },
    )
    original_unlink = oauth_login.Path.unlink

    def fail_pending_unlink(path, *args, **kwargs):
        if path == oauth_login.pending_path():
            raise OSError("busy")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        oauth_login.Path,
        "unlink",
        fail_pending_unlink,
    )

    result = oauth_login.complete_login(expected_flow_id="owned-flow")

    assert result["success"] is True
    assert result["warnings"] == ["pending_cleanup_failed"]
    assert oauth_login.token_path().is_file()
    assert oauth_login.pending_path().is_file()


def test_oauth_completion_cancelled_at_commit_guard_does_not_save_tokens(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_USER_CONSENT", "1")
    oauth_login._write_private_json(
        oauth_login.pending_path(),
        {
            "version": oauth_login.PENDING_VERSION,
            "flow_id": "owned-flow",
            "consent_revision": security.consent_revision(),
            "created_at": time.time(),
            "expires_in": 900,
            "interval": 1,
            "token_endpoint": "https://auth.x.ai/oauth/token",
            "device_code": "device",
        },
    )
    monkeypatch.setattr(
        oauth_login,
        "poll_device_token",
        lambda **_kwargs: {
            "access_token": "must-not-be-saved",
            "refresh_token": "must-not-be-saved",
            "expires_in": 3600,
        },
    )
    gate = threading.Lock()
    gate.acquire()
    reached_guard = threading.Event()
    cancel_event = threading.Event()
    errors: list[Exception] = []

    @contextmanager
    def commit_guard():
        reached_guard.set()
        with gate:
            yield

    def complete() -> None:
        try:
            oauth_login.complete_login(
                cancel_event=cancel_event,
                expected_flow_id="owned-flow",
                commit_guard=commit_guard,
            )
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    worker = threading.Thread(target=complete)
    worker.start()
    assert reached_guard.wait(timeout=5)
    cancel_event.set()
    gate.release()
    worker.join(timeout=5)

    assert len(errors) == 1
    assert "cancelled" in str(errors[0])
    assert not oauth_login.token_path().exists()
    assert oauth_login.pending_path().is_file()


def test_oauth_completion_does_not_commit_replaced_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    security.grant_consent()
    monkeypatch.setattr(
        oauth_login,
        "discovery",
        lambda: {"token_endpoint": "https://auth.x.ai/oauth/token"},
    )
    monkeypatch.setattr(
        oauth_login,
        "request_device_code",
        lambda: {
            "device_code": "device",
            "user_code": "CODE",
            "verification_uri": "https://accounts.x.ai/device",
            "expires_in": 900,
            "interval": 5,
        },
    )
    started = oauth_login.start_login(open_browser=False)

    def replace_flow(**_kwargs):
        pending = json.loads(oauth_login.pending_path().read_text(encoding="utf-8"))
        pending["flow_id"] = "replacement-flow"
        oauth_login._write_private_json(oauth_login.pending_path(), pending)
        return {
            "access_token": "discard-me",
            "refresh_token": "discard-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr(oauth_login, "poll_device_token", replace_flow)

    with pytest.raises(RuntimeError, match="replaced"):
        oauth_login.complete_login(expected_flow_id=started["flow_id"])

    assert not oauth_login.token_path().exists()
    pending = json.loads(oauth_login.pending_path().read_text(encoding="utf-8"))
    assert pending["flow_id"] == "replacement-flow"


def test_oauth_completion_rejects_revoke_and_regrant(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    security.grant_consent()
    monkeypatch.setattr(
        oauth_login,
        "discovery",
        lambda: {"token_endpoint": "https://auth.x.ai/oauth/token"},
    )
    monkeypatch.setattr(
        oauth_login,
        "request_device_code",
        lambda: {
            "device_code": "device",
            "user_code": "CODE",
            "verification_uri": "https://accounts.x.ai/device",
            "expires_in": 900,
            "interval": 5,
        },
    )
    started = oauth_login.start_login(open_browser=False)

    def change_consent(**_kwargs):
        security.revoke_consent()
        security.grant_consent()
        return {
            "access_token": "discard-me",
            "refresh_token": "discard-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr(oauth_login, "poll_device_token", change_consent)

    with pytest.raises(RuntimeError, match="consent changed"):
        oauth_login.complete_login(expected_flow_id=started["flow_id"])

    assert not oauth_login.token_path().exists()
    assert oauth_login.pending_path().is_file()


def test_auth_prefers_oauth_token(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("GROK_CODEX_AUTH_MODE", "subscription")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    from grok_codex import auth, oauth_login

    monkeypatch.setattr(oauth_login, "resolve_access_token", lambda: "oauth-access-token")
    ctx = auth.resolve_auth()
    assert ctx["mode"] == "subscription_oauth"


def test_auth_status_never_resolves_or_refreshes_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_CODEX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr(
        auth.oauth_login,
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
