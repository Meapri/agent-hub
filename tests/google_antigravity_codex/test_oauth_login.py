from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from google_antigravity_codex import oauth_login, security


@pytest.fixture
def consent_and_paths(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(config))
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_USER_CONSENT", "1")
    monkeypatch.setenv(
        "GOOGLE_ANTIGRAVITY_CLIENT_ID",
        "test-client.apps.googleusercontent.com",
    )
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CLIENT_SECRET", "test-secret")
    return config


def test_start_login_returns_auth_url_without_secrets(consent_and_paths):
    result = oauth_login.start_login(use_local_redirect=True)

    assert result["success"] is True
    assert "accounts.google.com" in result["auth_url"]
    assert "code_challenge=" in result["auth_url"]
    assert "test-secret" not in json.dumps(result)
    assert oauth_login.pending_file_path().is_file()


def test_complete_login_exchanges_code_and_saves_tokens(consent_and_paths):
    oauth_login.start_login(use_local_redirect=False)

    def fake_exchange(*, client, code, verifier, redirect_uri):
        assert code == "auth-code-123"
        assert client.client_id.startswith("test-client")
        assert verifier
        assert redirect_uri == oauth_login.EXTERNAL_REDIRECT
        return {
            "access_token": "access-from-google",
            "refresh_token": "refresh-from-google",
            "expires_in": 3600,
        }

    with patch.object(oauth_login, "_exchange", side_effect=fake_exchange), patch.object(
        oauth_login, "_probe_login", return_value={"success": True, "method": "list_models", "model_count": 3}
    ):
        result = oauth_login.complete_login("auth-code-123")

    assert result["success"] is True
    assert result["access_token_present"] is True
    assert result["refresh_token_present"] is True
    assert result["probe"]["success"] is True
    assert "Probe OK" in result["text"]
    token_path = Path(result["token_file"])
    data = json.loads(token_path.read_text(encoding="utf-8"))
    assert data["access"] == "access-from-google"
    assert data["refresh"] == "refresh-from-google"
    assert not oauth_login.pending_file_path().is_file()


def test_complete_login_probe_failure_still_saves_tokens(consent_and_paths):
    oauth_login.start_login(use_local_redirect=False)

    with patch.object(
        oauth_login,
        "_exchange",
        return_value={"access_token": "a", "refresh_token": "r", "expires_in": 3600},
    ), patch.object(
        oauth_login,
        "_probe_login",
        return_value={"success": False, "error_type": "network", "error": "down"},
    ):
        result = oauth_login.complete_login("code", probe=True)

    assert result["success"] is True
    assert "login_probe_failed" in result.get("warnings", [])
    assert Path(result["token_file"]).is_file()


def test_complete_login_state_mismatch(consent_and_paths):
    started = oauth_login.start_login(use_local_redirect=False)
    bad = f"https://antigravity.google/oauth-callback?code=x&state=not-{started['state']}"
    with pytest.raises(oauth_login.OAuthLoginError) as raised:
        oauth_login.complete_login(bad, probe=False)
    assert raised.value.code == "oauth_state_mismatch"


def test_pending_login_expires(consent_and_paths, monkeypatch):
    oauth_login.start_login()
    pending = json.loads(oauth_login.pending_file_path().read_text(encoding="utf-8"))
    pending["created_at"] = 1.0
    oauth_login.pending_file_path().write_text(json.dumps(pending), encoding="utf-8")
    with pytest.raises(oauth_login.OAuthLoginError) as raised:
        oauth_login.complete_login("code", probe=False)
    assert raised.value.code == "oauth_pending_expired"


def test_refresh_access_token_saves_new_access(consent_and_paths):
    client = oauth_login.OAuthClient(
        client_id="test-client.apps.googleusercontent.com",
        client_secret="test-secret",
        label="test",
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"access_token": "fresh-access", "expires_in": 1800, "refresh_token": "r2"}
            ).encode()

    with patch.object(oauth_login.urllib.request, "urlopen", return_value=FakeResponse()):
        result = oauth_login.refresh_access_token(refresh_token="old-refresh", client=client)

    assert result["success"] is True
    data = json.loads(Path(result["token_file"]).read_text(encoding="utf-8"))
    assert data["access"] == "fresh-access"


def test_interactive_login_with_pasted_code(consent_and_paths):
    lines = ["auth-code-interactive"]

    def fake_input(_prompt: str) -> str:
        return lines.pop(0)

    with patch.object(
        oauth_login,
        "_exchange",
        return_value={"access_token": "ia", "refresh_token": "ir", "expires_in": 3600},
    ):
        result = oauth_login.run_interactive_login(
            use_local_server=False,
            input_fn=fake_input,
            print_fn=lambda *_: None,
            open_browser=False,
        )

    assert result["success"] is True
    assert Path(result["token_file"]).is_file()


def test_start_requires_consent(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_USER_CONSENT", raising=False)
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_ENABLE_AGY_SESSION", raising=False)
    # Ensure no consent file
    assert security.agy_session_enabled() is False

    with pytest.raises(oauth_login.OAuthLoginError) as raised:
        oauth_login.start_login()
    assert raised.value.code == "consent_required"


def test_login_tools_registered_in_mcp():
    from google_antigravity_codex import mcp_server

    names = {tool["name"] for tool in mcp_server.tool_definitions()}
    assert "google_antigravity_login_start" in names
    assert "google_antigravity_login_complete" in names
    assert "google_antigravity_login_status" in names
