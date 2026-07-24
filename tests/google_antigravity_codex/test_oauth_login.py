from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
import json
import threading
import urllib.error
from unittest.mock import patch

import pytest

from google_antigravity_codex import (
    account,
    agy_auth,
    consent_cli,
    oauth_login,
    security,
)


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
    pending = json.loads(
        oauth_login.pending_file_path().read_text(encoding="utf-8")
    )
    assert pending["version"] == oauth_login.PENDING_VERSION
    assert pending["flow_id"] == result["flow_id"]
    assert pending["consent_revision"]


def test_clear_pending_login_is_idempotent(consent_and_paths):
    started = oauth_login.start_login(use_local_redirect=False)

    assert oauth_login.clear_pending_login(expected_flow_id="other-flow") is False
    assert oauth_login.pending_file_path().is_file()
    assert (
        oauth_login.clear_pending_login(expected_flow_id=started["flow_id"])
        is True
    )
    assert oauth_login.clear_pending_login() is False
    assert not oauth_login.pending_file_path().exists()


@pytest.mark.parametrize(
    "payload",
    [
        "{not-json",
        json.dumps({"created_at": 1.0, "state": "legacy"}),
    ],
)
def test_clear_unusable_pending_login_recovers_invalid_or_legacy_state(
    consent_and_paths,
    payload,
):
    oauth_login.pending_file_path().write_text(payload, encoding="utf-8")

    assert oauth_login.clear_unusable_pending_login() is True
    assert not oauth_login.pending_file_path().exists()


def test_clear_unusable_pending_login_preserves_active_owned_flow(
    consent_and_paths,
):
    started = oauth_login.start_login(use_local_redirect=False)

    assert oauth_login.clear_unusable_pending_login() is False
    pending = json.loads(oauth_login.pending_file_path().read_text(encoding="utf-8"))
    assert pending["flow_id"] == started["flow_id"]


def test_start_login_does_not_overwrite_an_active_pending_flow(
    consent_and_paths,
):
    first = oauth_login.start_login(use_local_redirect=False)
    before = oauth_login.pending_file_path().read_text(encoding="utf-8")

    with pytest.raises(oauth_login.OAuthLoginError) as duplicate:
        oauth_login.start_login(use_local_redirect=False)

    assert duplicate.value.code == "oauth_login_in_progress"
    assert oauth_login.pending_file_path().read_text(encoding="utf-8") == before
    assert first["auth_url"].startswith("https://accounts.google.com/")


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
    token_path = oauth_login.token_file_path()
    data = json.loads(token_path.read_text(encoding="utf-8"))
    assert data["access"] == "access-from-google"
    assert data["refresh"] == "refresh-from-google"
    assert not oauth_login.pending_file_path().is_file()


def test_complete_login_reports_cleanup_warning_after_token_commit(
    consent_and_paths,
):
    oauth_login.start_login(use_local_redirect=False)
    token_payload = {
        "access_token": "access-from-google",
        "refresh_token": "refresh-from-google",
        "expires_in": 3600,
    }

    with patch.object(oauth_login, "_exchange", return_value=token_payload), patch.object(
        oauth_login.Path,
        "unlink",
        side_effect=OSError("busy"),
    ):
        result = oauth_login.complete_login("auth-code-123", probe=False)

    assert result["success"] is True
    assert "pending_cleanup_failed" in result["warnings"]
    assert oauth_login.token_file_path().is_file()
    assert oauth_login.pending_file_path().is_file()


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
    assert result["token_file_present"] is True
    assert oauth_login.token_file_path().is_file()


def test_exchange_error_does_not_expose_response_body(consent_and_paths):
    client = oauth_login.resolve_oauth_clients()[0]
    error = urllib.error.HTTPError(
        oauth_login.TOKEN_ENDPOINT,
        400,
        "Bad Request",
        {},
        BytesIO(b'{"error":"invalid_grant","detail":"sentinel-auth-code"}'),
    )

    with patch.object(
        oauth_login.urllib.request,
        "urlopen",
        side_effect=error,
    ):
        with pytest.raises(oauth_login.OAuthLoginError) as raised:
            oauth_login._exchange(  # noqa: SLF001
                client=client,
                code="public-code",
                verifier="public-verifier",
                redirect_uri=oauth_login.EXTERNAL_REDIRECT,
            )

    assert raised.value.code == "oauth_token_exchange_failed"
    assert "HTTP 400" in str(raised.value)
    assert "sentinel-auth-code" not in str(raised.value)
    assert "invalid_grant" not in str(raised.value)


def test_complete_login_state_mismatch(consent_and_paths):
    started = oauth_login.start_login(use_local_redirect=False)
    bad = f"https://antigravity.google/oauth-callback?code=x&state=not-{started['state']}"
    with pytest.raises(oauth_login.OAuthLoginError) as raised:
        oauth_login.complete_login(bad, probe=False)
    assert raised.value.code == "oauth_state_mismatch"


def test_callback_state_validation_does_not_consume_pending_login(
    consent_and_paths,
):
    started = oauth_login.start_login(use_local_redirect=True)
    valid = (
        f"http://localhost:{oauth_login.LOCAL_PORT}/auth/callback"
        f"?code=x&state={started['state']}"
    )

    oauth_login.validate_callback_state(valid, require_state=True)

    assert oauth_login.pending_file_path().is_file()
    with pytest.raises(oauth_login.OAuthLoginError) as missing:
        oauth_login.validate_callback_state(
            f"http://localhost:{oauth_login.LOCAL_PORT}/auth/callback?code=x",
            require_state=True,
        )
    assert missing.value.code == "oauth_state_mismatch"
    assert oauth_login.pending_file_path().is_file()


def test_callback_state_rejects_replaced_flow(consent_and_paths):
    started = oauth_login.start_login(use_local_redirect=True)
    valid = (
        f"http://localhost:{oauth_login.LOCAL_PORT}/auth/callback"
        f"?code=x&state={started['state']}"
    )

    with pytest.raises(oauth_login.OAuthLoginError) as mismatch:
        oauth_login.validate_callback_state(
            valid,
            require_state=True,
            expected_flow_id="other-flow",
        )

    assert mismatch.value.code == "oauth_flow_replaced"
    assert oauth_login.pending_file_path().is_file()


def test_complete_login_does_not_commit_replaced_flow(consent_and_paths):
    started = oauth_login.start_login(use_local_redirect=False)

    def replace_flow(**_kwargs):
        pending = json.loads(
            oauth_login.pending_file_path().read_text(encoding="utf-8")
        )
        pending["flow_id"] = "replacement-flow"
        oauth_login.io_util.write_json_secure(
            oauth_login.pending_file_path(),
            pending,
        )
        return {
            "access_token": "discard-me",
            "refresh_token": "discard-refresh",
            "expires_in": 3600,
        }

    with patch.object(oauth_login, "_exchange", side_effect=replace_flow):
        with pytest.raises(oauth_login.OAuthLoginError) as replaced:
            oauth_login.complete_login(
                "auth-code",
                probe=False,
                expected_flow_id=started["flow_id"],
            )

    assert replaced.value.code == "oauth_flow_replaced"
    assert not oauth_login.token_file_path().exists()
    pending = json.loads(
        oauth_login.pending_file_path().read_text(encoding="utf-8")
    )
    assert pending["flow_id"] == "replacement-flow"


def test_complete_login_rejects_revoke_and_regrant(
    consent_and_paths,
    monkeypatch,
):
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_USER_CONSENT")
    consent_cli.grant()
    started = oauth_login.start_login(use_local_redirect=False)

    def change_consent(**_kwargs):
        consent_cli.revoke()
        consent_cli.grant()
        return {
            "access_token": "discard-me",
            "refresh_token": "discard-refresh",
            "expires_in": 3600,
        }

    with patch.object(oauth_login, "_exchange", side_effect=change_consent):
        with pytest.raises(oauth_login.OAuthLoginError) as changed:
            oauth_login.complete_login(
                "auth-code",
                probe=False,
                expected_flow_id=started["flow_id"],
            )

    assert changed.value.code == "consent_changed"
    assert not oauth_login.token_file_path().exists()
    assert oauth_login.pending_file_path().is_file()


def test_complete_login_cancelled_at_commit_guard_does_not_save_tokens(
    consent_and_paths,
):
    started = oauth_login.start_login(use_local_redirect=False)
    gate = threading.Lock()
    gate.acquire()
    reached_guard = threading.Event()
    cancel_event = threading.Event()
    errors: list[oauth_login.OAuthLoginError] = []

    @contextmanager
    def commit_guard():
        reached_guard.set()
        with gate:
            yield

    def complete() -> None:
        try:
            oauth_login.complete_login(
                "auth-code",
                probe=False,
                expected_flow_id=started["flow_id"],
                cancel_event=cancel_event,
                commit_guard=commit_guard,
            )
        except oauth_login.OAuthLoginError as exc:
            errors.append(exc)

    with patch.object(
        oauth_login,
        "_exchange",
        return_value={
            "access_token": "must-not-be-saved",
            "refresh_token": "must-not-be-saved",
            "expires_in": 3600,
        },
    ):
        worker = threading.Thread(target=complete)
        worker.start()
        assert reached_guard.wait(timeout=5)
        cancel_event.set()
        gate.release()
        worker.join(timeout=5)

    assert len(errors) == 1
    assert errors[0].code == "oauth_login_cancelled"
    assert not oauth_login.token_file_path().exists()
    assert oauth_login.pending_file_path().is_file()


def test_pending_login_expires(consent_and_paths, monkeypatch):
    oauth_login.start_login()
    pending = json.loads(oauth_login.pending_file_path().read_text(encoding="utf-8"))
    pending["created_at"] = 1.0
    oauth_login.pending_file_path().write_text(json.dumps(pending), encoding="utf-8")
    with pytest.raises(oauth_login.OAuthLoginError) as raised:
        oauth_login.complete_login("code", probe=False)
    assert raised.value.code == "oauth_pending_expired"


def test_refresh_access_token_saves_new_access(consent_and_paths):
    oauth_login.save_tokens(
        access_token="old-access",
        refresh_token="old-refresh",
        expires_in=3600,
    )
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
    data = json.loads(oauth_login.token_file_path().read_text(encoding="utf-8"))
    assert data["access"] == "fresh-access"


def test_refresh_rejects_credentials_deleted_before_revision_snapshot(
    consent_and_paths,
):
    oauth_login.save_tokens(
        access_token="old-access",
        refresh_token="old-refresh",
        expires_in=3600,
    )
    credential_path = oauth_login.token_file_path()
    expected_revision = oauth_login.file_revision(credential_path)
    account.logout({})
    client = oauth_login.OAuthClient(
        client_id="test-client.apps.googleusercontent.com",
        client_secret="test-secret",
        label="test",
    )

    with patch.object(oauth_login.urllib.request, "urlopen") as request:
        with pytest.raises(oauth_login.OAuthLoginError) as changed:
            oauth_login.refresh_access_token(
                refresh_token="old-refresh",
                client=client,
                expected_credential_revision=expected_revision,
            )

    assert changed.value.code == "credentials_changed"
    request.assert_not_called()
    assert not credential_path.exists()


def test_refresh_does_not_recreate_credentials_deleted_during_network_call(
    consent_and_paths,
):
    oauth_login.save_tokens(
        access_token="old-access",
        refresh_token="old-refresh",
        expires_in=3600,
    )
    client = oauth_login.OAuthClient(
        client_id="test-client.apps.googleusercontent.com",
        client_secret="test-secret",
        label="test",
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            account.logout({})
            return json.dumps(
                {
                    "access_token": "must-not-be-saved",
                    "refresh_token": "must-not-be-saved",
                    "expires_in": 1800,
                }
            ).encode()

    with patch.object(oauth_login.urllib.request, "urlopen", return_value=FakeResponse()):
        with pytest.raises(oauth_login.OAuthLoginError) as changed:
            oauth_login.refresh_access_token(
                refresh_token="old-refresh",
                client=client,
            )

    assert changed.value.code == "credentials_changed"
    assert not oauth_login.token_file_path().exists()


def test_refresh_fences_and_updates_actual_canonical_token_with_missing_override(
    consent_and_paths,
    monkeypatch,
):
    canonical = agy_auth.plugin_token_path()
    oauth_login.save_tokens(
        access_token="old-access",
        refresh_token="old-refresh",
        expires_in=3600,
        destination=canonical,
    )
    override = consent_and_paths / "missing-override.json"
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE", str(override))
    expected_revision = oauth_login.file_revision(canonical)
    client = oauth_login.OAuthClient(
        client_id="test-client.apps.googleusercontent.com",
        client_secret="test-secret",
        label="test",
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "access_token": "fresh-access",
                    "refresh_token": "fresh-refresh",
                    "expires_in": 1800,
                }
            ).encode()

    with patch.object(
        oauth_login.urllib.request,
        "urlopen",
        return_value=FakeResponse(),
    ):
        result = oauth_login.refresh_access_token(
            refresh_token="old-refresh",
            client=client,
            expected_credential_revision=expected_revision,
        )

    assert result["success"] is True
    assert not override.exists()
    assert json.loads(canonical.read_text(encoding="utf-8"))["access"] == "fresh-access"


def test_refresh_does_not_revive_canonical_token_through_missing_override(
    consent_and_paths,
    monkeypatch,
):
    canonical = agy_auth.plugin_token_path()
    oauth_login.save_tokens(
        access_token="old-access",
        refresh_token="old-refresh",
        expires_in=3600,
        destination=canonical,
    )
    override = consent_and_paths / "missing-override.json"
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE", str(override))
    client = oauth_login.OAuthClient(
        client_id="test-client.apps.googleusercontent.com",
        client_secret="test-secret",
        label="test",
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            account.logout({})
            return json.dumps(
                {
                    "access_token": "must-not-be-saved",
                    "refresh_token": "must-not-be-saved",
                    "expires_in": 1800,
                }
            ).encode()

    with patch.object(
        oauth_login.urllib.request,
        "urlopen",
        return_value=FakeResponse(),
    ):
        with pytest.raises(oauth_login.OAuthLoginError) as changed:
            oauth_login.refresh_access_token(
                refresh_token="old-refresh",
                client=client,
            )

    assert changed.value.code == "credentials_changed"
    assert not canonical.exists()
    assert not override.exists()


def test_login_status_finds_canonical_token_when_override_is_missing(
    consent_and_paths,
    monkeypatch,
):
    canonical = agy_auth.plugin_token_path()
    canonical.write_text(
        json.dumps(
            {
                "access": "canonical-access",
                "refresh": "canonical-refresh",
                "expires": 4102444800000,
            }
        ),
        encoding="utf-8",
    )
    canonical.chmod(0o600)
    monkeypatch.setenv(
        "GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE",
        str(consent_and_paths / "missing-override.json"),
    )

    state = oauth_login.login_status()

    assert state["token_file_present"] is True
    assert state["credentials_readable"] is True
    assert "canonical-access" not in json.dumps(state)


def test_login_status_reports_malformed_local_token(
    consent_and_paths,
):
    token = agy_auth.plugin_token_path()
    token.write_text("{broken", encoding="utf-8")
    token.chmod(0o600)

    state = oauth_login.login_status()

    assert state["token_file_present"] is True
    assert state["credentials_readable"] is False


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
    assert result["token_file_present"] is True
    assert oauth_login.token_file_path().is_file()


def test_interactive_login_does_not_overwrite_existing_flow(consent_and_paths):
    oauth_login.start_login(use_local_redirect=False)
    before = oauth_login.pending_file_path().read_text(encoding="utf-8")

    with pytest.raises(oauth_login.OAuthLoginError) as active:
        oauth_login.run_interactive_login(
            use_local_server=False,
            input_fn=lambda _prompt: "unused",
            print_fn=lambda *_args: None,
            open_browser=False,
        )

    assert active.value.code == "oauth_login_in_progress"
    assert oauth_login.pending_file_path().read_text(encoding="utf-8") == before


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
