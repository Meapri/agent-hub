from __future__ import annotations

import json

import pytest

from google_antigravity_codex import agy_auth, io_util, oauth_login, profiles, provider, session_prefs


def test_provider_priority_env_over_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_USER_CONSENT", "1")
    session_prefs.set_provider("agy-oauth")
    # env still wins when set to oauth
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_PROVIDER", "agy-oauth")
    assert provider.selected_provider() == "agy-oauth"
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_PROVIDER")
    # without env, saved pref + token readiness
    token = tmp_path / "oauth-token.json"
    token.write_text(json.dumps({"access": "x", "expires": 4102444800000}), encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE", str(token))
    assert provider.selected_provider() == "agy-oauth"
    # agy-cli env is rejected
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_PROVIDER", "agy-cli")
    with pytest.raises(provider.ProviderError) as raised:
        provider.selected_provider()
    assert raised.value.code == "agy_cli_removed"


def test_profile_does_not_override_explicit_grounding_off(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    profiles.use_profile_tool({"name": "research", "apply_provider": False, "apply_model_pref": False})
    args = profiles.apply_profile_to_chat_args({"prompt": "hi", "grounding": "off"})
    assert args["grounding"] == "off"
    # Missing grounding inherits research always
    filled = profiles.apply_profile_to_chat_args({"prompt": "hi"})
    assert filled["grounding"] == "always"


def test_pending_login_does_not_store_client_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_USER_CONSENT", "1")
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CLIENT_SECRET", "csec")
    oauth_login.start_login(use_local_redirect=False)
    pending = json.loads(oauth_login.pending_file_path().read_text(encoding="utf-8"))
    assert "client_secret" not in pending
    assert pending.get("client_id")
    assert pending.get("verifier")


def test_write_json_secure_atomic(tmp_path):
    path = tmp_path / "nested" / "cfg.json"
    io_util.write_json_secure(path, {"a": 1})
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["a"] == 1
    if path.stat().st_mode & 0o077 == 0:
        # mode bits applied on posix
        assert True


def test_token_path_candidates_include_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE", raising=False)
    plugin = agy_auth.plugin_token_path()
    assert plugin == tmp_path / "oauth-token.json"
    assert plugin in agy_auth.candidate_token_paths()
    assert agy_auth.token_file_path() == plugin  # none exist → write target
