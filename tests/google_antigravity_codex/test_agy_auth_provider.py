from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
from unittest.mock import patch

import pytest

from google_antigravity_codex import agy_auth, antigravity_api, provider


def write_token(path: Path, data: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(mode)


def enable(monkeypatch, token_file: Path) -> None:
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ENABLE_AGY_SESSION", "1")
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE", str(token_file))


def test_loads_proxy_compatible_top_level_and_nested_token_schema(tmp_path, monkeypatch):
    token_file = tmp_path / "oauth-token.json"
    enable(monkeypatch, token_file)
    write_token(
        token_file,
        {
            "token": {
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "expiry": "2099-01-01T00:00:00Z",
            },
            "project_id": "project-one",
        },
    )

    credentials = agy_auth.load_credentials()

    assert credentials.access_token == "access-secret"
    assert credentials.refresh_token == "refresh-secret"
    assert credentials.project_id == "project-one"
    assert credentials.expired is False
    assert "secret" not in repr(credentials)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_rejects_group_readable_token_export(tmp_path, monkeypatch):
    token_file = tmp_path / "oauth-token.json"
    enable(monkeypatch, token_file)
    write_token(token_file, {"access_token": "secret"}, mode=0o640)

    with pytest.raises(agy_auth.AgyAuthError) as raised:
        agy_auth.load_credentials()

    assert raised.value.code == "agy_token_file_permissions_invalid"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink contract")
def test_rejects_symlink_token_export(tmp_path, monkeypatch):
    target = tmp_path / "real-token.json"
    token_file = tmp_path / "oauth-token.json"
    enable(monkeypatch, token_file)
    write_token(target, {"access_token": "secret"})
    token_file.symlink_to(target)

    with pytest.raises(agy_auth.AgyAuthError) as raised:
        agy_auth.load_credentials()

    assert raised.value.code == "agy_token_file_unsafe"


def test_status_reports_only_presence_not_token_values(tmp_path, monkeypatch):
    token_file = tmp_path / "oauth-token.json"
    enable(monkeypatch, token_file)
    write_token(token_file, {"access": "access-secret", "refresh": "refresh-secret", "expires": 4102444800000})

    state = agy_auth.status()

    serialized = json.dumps(state)
    assert state["credentials_readable"] is True
    assert state["access_token_present"] is True
    assert state["refresh_token_present"] is True
    assert "access-secret" not in serialized
    assert "refresh-secret" not in serialized


def test_provider_auto_selects_oauth_when_token_present(tmp_path, monkeypatch):
    token_file = tmp_path / "oauth-token.json"
    enable(monkeypatch, token_file)
    write_token(token_file, {"access": "secret", "expires": 4102444800000})
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_PROVIDER", raising=False)
    assert provider.selected_provider() == "agy-oauth"


def test_agy_cli_provider_is_removed(monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_PROVIDER", "agy-cli")
    with pytest.raises(provider.ProviderError) as raised:
        provider.selected_provider()
    assert raised.value.code == "agy_cli_removed"


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_PROVIDER", "gemini-api")
    with pytest.raises(provider.ProviderError) as raised:
        provider.selected_provider()
    assert raised.value.code == "provider_invalid"


def test_oauth_capabilities_include_grounding_and_image(tmp_path, monkeypatch):
    token_file = tmp_path / "oauth-token.json"
    enable(monkeypatch, token_file)
    write_token(token_file, {"access": "secret", "expires": 4102444800000})
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_PROVIDER", "agy-oauth")
    assert provider.capabilities() == {
        "text": True,
        "native_grounding": True,
        "image": True,
        "hosted_tools": True,
    }
    assert provider.require_capability("image") == "agy-oauth"
    assert provider.require_capability("native_grounding") == "agy-oauth"


def test_provider_not_configured_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_USER_CONSENT", "1")
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_PROVIDER", raising=False)
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE", raising=False)
    with pytest.raises(provider.ProviderError) as raised:
        provider.selected_provider()
    assert raised.value.code == "provider_not_configured"


def test_direct_transport_uses_token_in_memory_and_maps_default_model():
    credentials = agy_auth.AgyCredentials(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at_ms=4102444800000,
        project_id="project-one",
    )
    seen = {}

    def fake_post(path, body, access_token, *, timeout):
        seen.update({"path": path, "body": body, "access_token": access_token, "timeout": timeout})
        return {"response": {"candidates": []}}

    with patch.object(antigravity_api.agy_auth, "valid_credentials", return_value=credentials), patch.object(
        antigravity_api, "_post", side_effect=fake_post
    ):
        payload = antigravity_api.generate_content(
            model="gemini-3.5-flash",
            request={"contents": [{"parts": [{"text": "hello"}]}]},
            max_retries=0,
        )

    assert seen["path"] == "/v1internal:generateContent"
    assert seen["body"]["model"] == "gemini-3.5-flash-high"
    assert seen["body"]["project"] == "project-one"
    assert seen["access_token"] == "access-secret"
    assert payload["_antigravity_diagnostics"]["auth_source"] == "agy-token-export"
    assert payload["_antigravity_diagnostics"]["auth_refreshed"] is False
    assert payload["_antigravity_diagnostics"]["capacity_fallback"] is False
    assert "access-secret" not in json.dumps(payload)


def test_direct_transport_capacity_fallback_to_next_model_tier():
    credentials = agy_auth.AgyCredentials(
        access_token="access-secret",
        expires_at_ms=4102444800000,
        project_id="project-one",
    )
    seen_models: list[str] = []

    def fake_post(path, body, access_token, *, timeout):
        del path, access_token, timeout
        seen_models.append(str(body.get("model")))
        if body.get("model") == "gemini-3-flash-agent":
            raise antigravity_api.AntigravityApiError(
                "capacity",
                code="antigravity_http_503",
                status_code=503,
            )
        return {"response": {"candidates": []}}

    with patch.object(antigravity_api.agy_auth, "valid_credentials", return_value=credentials), patch.object(
        antigravity_api, "_post", side_effect=fake_post
    ):
        payload = antigravity_api.generate_content(
            model="gemini-3.5-flash-high",
            request={"contents": [{"parts": [{"text": "hello"}]}]},
            max_retries=0,
        )

    assert seen_models[0] == "gemini-3-flash-agent"
    assert seen_models[1] == "gemini-3.5-flash-low"
    assert payload["_antigravity_diagnostics"]["capacity_fallback"] is True
    assert payload["_antigravity_diagnostics"]["used_model"] == "gemini-3.5-flash-low"


def test_direct_transport_refreshes_via_oauth_after_unauthorized():
    stale = agy_auth.AgyCredentials(
        access_token="stale-token",
        refresh_token="refresh-secret",
        expires_at_ms=4102444800000,
        project_id="project-one",
    )
    fresh = agy_auth.AgyCredentials(
        access_token="fresh-token",
        refresh_token="refresh-secret",
        expires_at_ms=4102444800000,
        project_id="project-one",
    )
    calls = {"n": 0}

    def fake_post(path, body, access_token, *, timeout):
        del path, body, timeout
        calls["n"] += 1
        if access_token == "stale-token":
            raise antigravity_api.AntigravityApiError("nope", code="antigravity_unauthorized", status_code=401)
        return {"response": {"candidates": []}}

    with patch.object(antigravity_api.agy_auth, "valid_credentials", return_value=stale), patch.object(
        antigravity_api.agy_auth, "force_refresh_credentials", return_value=fresh
    ), patch.object(antigravity_api, "_post", side_effect=fake_post):
        payload = antigravity_api.generate_content(
            model="gemini-3.5-flash",
            request={"contents": [{"parts": [{"text": "hello"}]}]},
            max_retries=0,
        )

    assert calls["n"] == 2
    assert payload["_antigravity_diagnostics"]["auth_refreshed"] is True


def test_http_error_does_not_include_request_body_or_token():
    class FailingOpener:
        def open(self, request, timeout):
            del request, timeout
            raise urllib.error.HTTPError(
                "https://cloudcode-pa.googleapis.com",
                401,
                "unauthorized",
                {},
                None,
            )

    with patch.object(antigravity_api.urllib.request, "build_opener", return_value=FailingOpener()):
        with pytest.raises(antigravity_api.AntigravityApiError) as raised:
            antigravity_api._post("/v1internal:test", {"secret": "body-secret"}, "access-secret", timeout=1)

    assert raised.value.code == "antigravity_unauthorized"
    assert "body-secret" not in str(raised.value)
    assert "access-secret" not in str(raised.value)
