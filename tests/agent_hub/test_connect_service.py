from __future__ import annotations

import json
import stat
import threading
import time

import pytest

from agent_hub import connect_service
from agent_hub import provider_settings
from agent_hub.connect_service import ConnectionError, ConnectionManager
from grok_codex import oauth_login as grok_oauth
from google_antigravity_codex import model_prefs as google_model_prefs
from google_antigravity_codex import oauth_login as google_oauth


def _state(
    *,
    consent: bool = False,
    authenticated: bool = False,
    ready: bool = False,
    logged_in: bool | None = None,
    refreshable: bool = False,
    relogin_required: bool = False,
    default_model: str = "test-model",
    model_overridden: bool = False,
    model_source: str = "provider_default",
    model_override_scope: str | None = None,
):
    logged_in = authenticated if logged_in is None else logged_in
    return {
        "consent": consent,
        "configured": logged_in,
        "authenticated": authenticated,
        "logged_in": logged_in,
        "auth_ready": authenticated,
        "account_present": logged_in,
        "refresh_supported": refreshable,
        "refreshable": refreshable,
        "relogin_required": relogin_required,
        "ready": ready,
        "auth_mode": "subscription_oauth" if logged_in else None,
        "default_model": default_model,
        "base_default_model": "test-model",
        "model_overridden": model_overridden,
        "model_source": model_source,
        "model_override_scope": model_override_scope,
        "warnings": [],
        "identity": {"email": "private@example.com"},
        "capabilities": {
            "chat": {"supported": True},
            "write": {"supported": True},
            "search": {"supported": False},
        },
    }


def _reader(states):
    def read(provider="all", *, probe=False):
        selected = states if provider == "all" else {provider: states[provider]}
        return {"providers": selected, "probe": probe}

    return read


def test_status_is_redacted_and_summarized():
    states = {
        "claude": _state(authenticated=True),
        "grok": _state(),
        "gemini": _state(),
        "gpt": _state(authenticated=True),
    }
    manager = ConnectionManager(status_reader=_reader(states))

    result = manager.status()

    assert result["summary"] == {
        "ready": 0,
        "authenticated": 2,
        "connected": 2,
        "refreshable": 0,
        "relogin_required": 0,
        "consent_required": 2,
        "total": 4,
    }
    claude = result["providers"]["claude"]
    assert claude["label"] == "Claude"
    assert claude["capabilities"] == ["대화", "문서 작성"]
    assert claude["login_ready"] is True
    assert claude["login_transport"] == "external_cli"
    assert result["providers"]["grok"]["login_transport"] == "browser"
    assert "identity" not in claude
    assert "email" not in str(result)


def test_status_distinguishes_expired_refreshable_and_relogin_required_accounts():
    refreshable = _state(
        consent=True,
        authenticated=False,
        logged_in=True,
        refreshable=True,
    )
    relogin = _state(
        consent=True,
        authenticated=False,
        logged_in=True,
        relogin_required=True,
    )
    manager = ConnectionManager(
        status_reader=_reader({"claude": refreshable, "gemini": relogin})
    )

    result = manager.status()

    claude = result["providers"]["claude"]
    gemini = result["providers"]["gemini"]
    assert claude["logged_in"] is True
    assert claude["auth_ready"] is False
    assert claude["refreshable"] is True
    assert claude["connection_state"] == "refreshable"
    assert gemini["logged_in"] is True
    assert gemini["refreshable"] is False
    assert gemini["relogin_required"] is True
    assert gemini["connection_state"] == "relogin_required"
    assert result["summary"]["connected"] == 2
    assert result["summary"]["refreshable"] == 1
    assert result["summary"]["relogin_required"] == 1


def test_auth_connection_state_remains_independent_from_consent():
    manager = ConnectionManager(
        status_reader=_reader(
            {
                "claude": _state(
                    consent=False,
                    authenticated=False,
                    logged_in=True,
                    refreshable=True,
                ),
                "grok": _state(
                    consent=False,
                    authenticated=False,
                    logged_in=True,
                    relogin_required=True,
                ),
            }
        )
    )

    result = manager.status()

    assert result["providers"]["claude"]["connection_state"] == "refreshable"
    assert result["providers"]["grok"]["connection_state"] == "relogin_required"


def test_status_normalizes_lifecycle_invariants_from_provider_state():
    inconsistent = _state(
        consent=True,
        authenticated=True,
        logged_in=False,
        refreshable=True,
        relogin_required=True,
        ready=True,
    )
    manager = ConnectionManager(
        status_reader=_reader({"gpt": inconsistent})
    )

    provider = manager.status("gpt")["providers"]["gpt"]

    assert provider["auth_ready"] is True
    assert provider["logged_in"] is True
    assert provider["account_present"] is True
    assert provider["refreshable"] is False
    assert provider["relogin_required"] is False
    assert provider["connection_state"] == "ready"


def test_status_redacts_unsafe_provider_warning():
    state = _state()
    state["warnings"] = [
        "consent_required",
        "request failed: access_token=secret",
    ]
    manager = ConnectionManager(status_reader=_reader({"gpt": state}))

    warnings = manager.status("gpt")["providers"]["gpt"]["warnings"]

    assert warnings == ["consent_required", "provider_warning"]
    assert "secret" not in str(warnings)


def test_status_exposes_only_safe_settings_error_code():
    state = _state()
    state["settings_error"] = "settings_invalid"
    manager = ConnectionManager(status_reader=_reader({"claude": state}))

    public = manager.status("claude")["providers"]["claude"]

    assert public["settings_error"] == "settings_invalid"


def test_local_logout_capability_is_separate_from_current_auth_mode():
    api_key = _state(authenticated=True)
    api_key["auth_mode"] = "api_key"
    oauth = _state(authenticated=True)
    oauth["auth_mode"] = "subscription_oauth"
    oauth["local_credentials_present"] = True

    key_status = ConnectionManager(
        status_reader=_reader({"grok": api_key})
    ).status("grok")
    oauth_status = ConnectionManager(
        status_reader=_reader({"grok": oauth})
    ).status("grok")

    assert key_status["providers"]["grok"]["session_label"] == "xAI API key"
    assert key_status["providers"]["grok"]["supports_local_logout"] is True
    assert key_status["providers"]["grok"]["local_credentials_present"] is False
    assert oauth_status["providers"]["grok"]["supports_local_logout"] is True
    assert oauth_status["providers"]["grok"]["local_credentials_present"] is True


def test_gpt_api_key_state_identifies_official_chatgpt_relogin_boundary():
    api_key = _state(
        consent=True,
        authenticated=False,
        logged_in=True,
        relogin_required=True,
    )
    api_key["auth_mode"] = "apiKey"
    api_key["warnings"] = ["codex_api_key_mode_not_subscription"]
    manager = ConnectionManager(status_reader=_reader({"gpt": api_key}))

    provider = manager.status("gpt")["providers"]["gpt"]

    assert provider["session_label"] == "Codex API key"
    assert provider["connection_state"] == "relogin_required"
    assert provider["refresh_supported"] is False


@pytest.mark.parametrize("provider", ["grok", "gemini"])
def test_broken_local_login_state_remains_removable(provider):
    state = _state(authenticated=False)
    state["local_credentials_present"] = True
    manager = ConnectionManager(status_reader=_reader({provider: state}))

    public = manager.status(provider)["providers"][provider]

    assert public["authenticated"] is False
    assert public["supports_local_logout"] is True
    assert public["local_credentials_present"] is True


def test_offline_model_catalog_does_not_call_provider(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"claude": _state(default_model="claude-sonnet-5")})
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.operations.dispatch_tool",
        lambda *_args, **_kwargs: pytest.fail("offline catalog must stay local"),
    )

    catalog = manager.models("claude")

    assert catalog["source"] == "curated"
    assert catalog["refreshed"] is False
    assert any(item["id"] == "claude-sonnet-5" for item in catalog["models"])
    assert all(item["selectable"] for item in catalog["models"])


def test_live_model_catalog_is_bounded_redacted_and_text_only(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader(
            {
                "grok": _state(
                    consent=True,
                    authenticated=True,
                    ready=True,
                    default_model="grok-live",
                )
            }
        )
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.operations.dispatch_tool",
        lambda *_args, **_kwargs: {
            "success": True,
            "data": {
                "models": {
                    "grok": {
                        "success": True,
                        "source": "live",
                        "text_models": [
                            {
                                "id": "grok-live",
                                "display": "Grok Live",
                                "access_token": "secret",
                            },
                            {"id": "grok-live", "display": "duplicate"},
                            {"id": "grok-\u202esecret", "display": "unsafe"},
                        ],
                        "image_models": [
                            {"id": "grok-imagine-image", "display": "Image"}
                        ],
                    }
                }
            },
        },
    )

    catalog = manager.models("grok", refresh=True)

    assert catalog["refreshed"] is True
    assert catalog["models"] == [
        {
            "id": "grok-live",
            "display": "Grok Live",
            "source": "provider",
            "selectable": True,
        }
    ]
    assert "secret" not in str(catalog)


@pytest.mark.parametrize(
    ("provider", "source", "model_ids"),
    [
        (
            "gpt",
            "codex-app-server",
            ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5"],
        ),
        (
            "gemini",
            "agy-oauth",
            ["gemini-3.6-flash-high", "gemini-3.6-flash-medium"],
        ),
    ],
)
def test_ready_provider_live_catalog_keeps_dynamic_models(
    monkeypatch,
    provider,
    source,
    model_ids,
):
    manager = ConnectionManager(
        status_reader=_reader(
            {
                provider: _state(
                    consent=True,
                    authenticated=True,
                    ready=True,
                    default_model=model_ids[0],
                )
            }
        )
    )
    calls = []

    def list_models(name, arguments):
        calls.append((name, dict(arguments)))
        return {
            "success": True,
            "data": {
                "models": {
                    provider: {
                        "success": True,
                        "source": source,
                        "text_models": [
                            {"id": model_id, "display": model_id}
                            for model_id in model_ids
                        ],
                    }
                }
            },
        }

    monkeypatch.setattr(
        "agent_hub.connect_service.operations.dispatch_tool",
        list_models,
    )

    catalog = manager.models(provider, refresh=True)

    assert catalog["refreshed"] is True
    assert [item["id"] for item in catalog["models"]] == model_ids
    assert calls == [
        (
            "agent_hub_list_models",
            {"provider": provider, "probe": True},
        )
    ]


def test_live_catalog_disambiguates_duplicate_display_names(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader(
            {
                "gpt": _state(
                    consent=True,
                    authenticated=True,
                    ready=True,
                    default_model="gpt-5.6-luna",
                )
            }
        )
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.operations.dispatch_tool",
        lambda *_args, **_kwargs: {
            "success": True,
            "data": {
                "models": {
                    "gpt": {
                        "success": True,
                        "source": "codex-app-server",
                        "text_models": [
                            {
                                "id": "codex-auto-review",
                                "display": "GPT-5.6-Luna",
                            },
                            {
                                "id": "gpt-5.6-luna",
                                "display": "GPT-5.6-Luna",
                            },
                        ],
                    }
                }
            },
        },
    )

    catalog = manager.models("gpt", refresh=True)

    assert [item["display"] for item in catalog["models"]] == [
        "GPT-5.6-Luna (codex-auto-review)",
        "GPT-5.6-Luna (gpt-5.6-luna)",
    ]


@pytest.mark.parametrize(
    "live_payload",
    [
        {
            "success": True,
            "source": "curated",
            "warnings": ["live_list_failed"],
            "text_models": [{"id": "not-live"}],
        },
        {
            "success": True,
            "source": "live",
            "warnings": [],
            "text_models": [],
        },
    ],
)
def test_live_model_refresh_falls_back_when_live_catalog_is_unavailable(
    monkeypatch,
    live_payload,
):
    manager = ConnectionManager(
        status_reader=_reader(
            {
                "claude": _state(
                    consent=True,
                    authenticated=True,
                    ready=True,
                    default_model="claude-sonnet-5",
                )
            }
        )
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.operations.dispatch_tool",
        lambda *_args, **_kwargs: {
            "success": True,
            "data": {"models": {"claude": live_payload}},
        },
    )

    catalog = manager.models("claude", refresh=True)

    assert catalog["source"] == "curated"
    assert catalog["refreshed"] is False
    assert catalog["live_unavailable"] is True
    assert any(item["selectable"] for item in catalog["models"])


def test_equivalent_catalog_reads_keep_the_same_revision():
    manager = ConnectionManager(
        status_reader=_reader(
            {"claude": _state(default_model="claude-sonnet-5")}
        )
    )

    first = manager.models("claude")
    second = manager.models("claude")

    assert first["catalog_revision"] == second["catalog_revision"]


def test_login_start_invalidates_existing_model_catalog(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader(
            {
                "claude": _state(
                    consent=True,
                    authenticated=True,
                    ready=True,
                    default_model="claude-sonnet-5",
                )
            }
        )
    )
    catalog = manager.models("claude")
    requested = next(item["id"] for item in catalog["models"] if item["selectable"])
    monkeypatch.setattr(
        manager,
        "_start_external_login",
        lambda *_args: {"success": True, "provider": "claude"},
    )

    manager.start_login("claude")

    with pytest.raises(ConnectionError) as error:
        manager.set_default_model(
            "claude",
            requested,
            catalog_revision=catalog["catalog_revision"],
        )
    assert error.value.code == "model_catalog_stale"


def test_duplicate_refresh_reuses_job_and_never_exposes_provider_payload(monkeypatch):
    state = _state(
        consent=True,
        authenticated=False,
        logged_in=True,
        refreshable=True,
        default_model="claude-sonnet-5",
    )
    manager = ConnectionManager(status_reader=_reader({"claude": state}))
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def refresh_provider(provider, *, cancel_event, commit_guard):
        calls.append(provider)
        entered.set()
        assert release.wait(timeout=5)
        with commit_guard():
            assert cancel_event.is_set() is False
            state.update(
                {
                    "authenticated": True,
                    "auth_ready": True,
                    "refreshable": False,
                    "ready": True,
                }
            )
        return {
            "access_token": "secret-access-token",
            "refresh_token": "secret-refresh-token",
        }

    monkeypatch.setattr(manager, "_refresh_provider", refresh_provider)

    first = manager.start_refresh("claude")
    assert entered.wait(timeout=5)
    second = manager.start_refresh("claude")
    release.set()
    deadline = time.time() + 5
    while manager.job(first["id"])["state"] == "working" and time.time() < deadline:
        time.sleep(0.01)
    finished = manager.job(first["id"])

    assert second["id"] == first["id"]
    assert calls == ["claude"]
    assert finished["state"] == "complete"
    assert "secret" not in str(first)
    assert "secret" not in str(finished)


def test_refresh_requires_consent_and_refreshable_session():
    missing_consent = ConnectionManager(
        status_reader=_reader(
            {
                "grok": _state(
                    authenticated=False,
                    logged_in=True,
                    refreshable=True,
                )
            }
        )
    )
    relogin = ConnectionManager(
        status_reader=_reader(
            {
                "grok": _state(
                    consent=True,
                    authenticated=False,
                    logged_in=True,
                    relogin_required=True,
                )
            }
        )
    )

    with pytest.raises(ConnectionError) as consent_error:
        missing_consent.start_refresh("grok")
    with pytest.raises(ConnectionError) as refresh_error:
        relogin.start_refresh("grok")

    assert consent_error.value.code == "consent_required"
    assert refresh_error.value.code == "refresh_unavailable"


def test_refresh_failure_is_redacted_and_keeps_relogin_available(monkeypatch):
    state = _state(
        consent=True,
        authenticated=False,
        logged_in=True,
        refreshable=True,
    )
    manager = ConnectionManager(status_reader=_reader({"gemini": state}))

    def fail_refresh(*_args, **_kwargs):
        raise RuntimeError("access_token=secret refresh_token=private")

    monkeypatch.setattr(manager, "_refresh_provider", fail_refresh)

    started = manager.start_refresh("gemini")
    deadline = time.time() + 5
    job = manager.job(started["id"])
    while job["state"] == "working" and time.time() < deadline:
        time.sleep(0.01)
        job = manager.job(started["id"])

    assert job["state"] == "failed"
    assert "secret" not in str(job)
    assert "private" not in str(job)
    assert manager.status("gemini")["providers"]["gemini"]["refreshable"] is True


def test_manager_close_cancels_refresh_before_provider_commit(monkeypatch):
    state = _state(
        consent=True,
        authenticated=False,
        logged_in=True,
        refreshable=True,
    )
    manager = ConnectionManager(status_reader=_reader({"grok": state}))
    entered = threading.Event()
    release = threading.Event()
    committed = threading.Event()

    def refresh_provider(_provider, *, cancel_event, commit_guard):
        entered.set()
        assert release.wait(timeout=5)
        with commit_guard():
            assert cancel_event.is_set() is False
            committed.set()
        return {"success": True}

    monkeypatch.setattr(manager, "_refresh_provider", refresh_provider)

    started = manager.start_refresh("grok")
    assert entered.wait(timeout=5)
    manager.close()
    release.set()
    deadline = time.time() + 5
    while manager.job(started["id"])["state"] != "cancelled" and time.time() < deadline:
        time.sleep(0.01)

    assert manager.job(started["id"])["state"] == "cancelled"
    assert committed.is_set() is False


@pytest.mark.parametrize("kind", ["login", "refresh"])
def test_auth_job_completion_invalidates_catalog_loaded_during_job(kind):
    manager = ConnectionManager(
        status_reader=_reader(
            {
                "grok": _state(
                    consent=True,
                    authenticated=True,
                    ready=True,
                    default_model="grok-4.5",
                )
            }
        )
    )
    job = manager._create_job(  # noqa: SLF001 - auth generation regression boundary
        "grok",
        kind=kind,
        state="working",
        message="working",
    )
    catalog = manager.models("grok")
    requested = next(item["id"] for item in catalog["models"] if item["selectable"])

    manager._update_job(job.id, state="complete", message="done")  # noqa: SLF001

    with pytest.raises(ConnectionError) as error:
        manager.set_default_model(
            "grok",
            requested,
            catalog_revision=catalog["catalog_revision"],
        )
    assert error.value.code == "model_catalog_stale"


def test_auth_change_cannot_resurrect_inflight_model_catalog(monkeypatch):
    state = _state(
        consent=True,
        authenticated=True,
        ready=True,
        default_model="grok-4.5",
    )
    manager = ConnectionManager(status_reader=_reader({"grok": state}))
    entered = threading.Event()
    release = threading.Event()
    original_payload = connect_service._local_model_payload
    errors = []

    def blocked_payload(provider):
        entered.set()
        assert release.wait(timeout=5)
        return original_payload(provider)

    def load_models():
        try:
            manager.models("grok")
        except Exception as exc:  # noqa: BLE001 - thread result capture
            errors.append(exc)

    monkeypatch.setattr(connect_service, "_local_model_payload", blocked_payload)
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_security.revoke_consent",
        lambda: True,
    )
    worker = threading.Thread(target=load_models)
    worker.start()
    assert entered.wait(timeout=5)

    manager.revoke_consent("grok", confirmation="disconnect:grok")
    release.set()
    worker.join(timeout=5)

    assert len(errors) == 1
    assert isinstance(errors[0], ConnectionError)
    assert errors[0].code == "model_catalog_stale"
    assert "grok" not in manager._model_catalogs  # noqa: SLF001


def test_local_credential_removal_invalidates_model_catalog(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader(
            {
                "grok": _state(
                    consent=True,
                    authenticated=True,
                    ready=True,
                    default_model="grok-4.5",
                )
            }
        )
    )
    catalog = manager.models("grok")
    requested = next(item["id"] for item in catalog["models"] if item["selectable"])
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.clear_tokens",
        lambda: True,
    )

    manager.remove_local_credentials(
        "grok",
        confirmation="forget-local:grok",
    )

    with pytest.raises(ConnectionError) as error:
        manager.set_default_model(
            "grok",
            requested,
            catalog_revision=catalog["catalog_revision"],
        )
    assert error.value.code == "model_catalog_stale"


def test_gemini_credential_removal_failure_is_fail_closed(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"gemini": _state(authenticated=False)})
    )
    manager.models("gemini")
    monkeypatch.setattr(
        "agent_hub.connect_service.google_account.logout",
        lambda _args: {
            "success": False,
            "removed": True,
            "removed_count": 1,
            "failed_count": 1,
            "error_type": "credential_removal_failed",
        },
    )

    with pytest.raises(ConnectionError) as error:
        manager.remove_local_credentials(
            "gemini",
            confirmation="forget-local:gemini",
        )

    assert error.value.code == "credential_removal_failed"
    assert "gemini" not in manager._model_catalogs  # noqa: SLF001


def test_grok_partial_credential_removal_invalidates_catalog(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"grok": _state(authenticated=False)})
    )
    manager.models("grok")
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.clear_tokens",
        lambda: (_ for _ in ()).throw(PermissionError("private path")),
    )

    with pytest.raises(ConnectionError) as error:
        manager.remove_local_credentials(
            "grok",
            confirmation="forget-local:grok",
        )

    assert error.value.code == "credential_removal_failed"
    assert "private path" not in str(error.value)
    assert "grok" not in manager._model_catalogs  # noqa: SLF001


def test_unavailable_selected_model_is_display_only():
    manager = ConnectionManager(
        status_reader=_reader(
            {"gpt": _state(default_model="gpt-legacy", model_overridden=True)}
        )
    )

    catalog = manager.models("gpt")
    selected = next(item for item in catalog["models"] if item["id"] == "gpt-legacy")

    assert selected["selectable"] is False
    with pytest.raises(ConnectionError) as error:
        manager.set_default_model(
            "gpt",
            "gpt-legacy",
            catalog_revision=catalog["catalog_revision"],
        )
    assert error.value.code == "model_not_available"


def test_model_save_requires_matching_catalog_revision():
    manager = ConnectionManager(
        status_reader=_reader({"claude": _state(default_model="claude-sonnet-5")})
    )
    manager.models("claude")

    with pytest.raises(ConnectionError) as error:
        manager.set_default_model(
            "claude",
            "claude-fable-5",
            catalog_revision="stale",
        )

    assert error.value.code == "model_catalog_stale"


def test_model_save_and_reset_preserve_other_provider_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_CONFIG_DIR", str(tmp_path / "config"))
    provider_settings.update("claude", {"temperature": 0.2})

    def read(provider="all", *, probe=False):
        saved = provider_settings.get("claude")
        state = _state(
            default_model=str(saved.get("model") or "claude-sonnet-5"),
            model_overridden=bool(saved.get("model")),
            model_source="saved" if saved.get("model") else "provider_default",
        )
        return {
            "providers": {"claude": state}
            if provider == "all"
            else {provider: state},
            "probe": probe,
        }

    manager = ConnectionManager(status_reader=read)
    catalog = manager.models("claude")
    saved = manager.set_default_model(
        "claude",
        "claude-fable-5",
        catalog_revision=catalog["catalog_revision"],
    )

    assert saved["selected_model"] == "claude-fable-5"
    assert provider_settings.get("claude") == {
        "temperature": 0.2,
        "model": "claude-fable-5",
    }
    assert stat.S_IMODE(provider_settings.settings_path().stat().st_mode) == 0o600

    reset = manager.reset_default_model(
        "claude",
        confirmation="reset-model:claude",
    )

    assert reset["selected_model"] == "claude-sonnet-5"
    assert provider_settings.get("claude") == {"temperature": 0.2}


def test_gemini_model_save_and_reset_use_chat_scope(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "GOOGLE_ANTIGRAVITY_CONFIG_DIR",
        str(tmp_path / "google"),
    )

    def read(provider="all", *, probe=False):
        prefs = google_model_prefs.load_prefs()
        chat = str((prefs.get("task_models") or {}).get("chat") or "")
        state = _state(
            default_model=chat or "gemini-3.5-flash-high",
            model_overridden=bool(chat),
            model_source="saved_chat" if chat else "provider_default",
            model_override_scope="task:chat" if chat else None,
        )
        return {
            "providers": {"gemini": state}
            if provider == "all"
            else {provider: state},
            "probe": probe,
        }

    manager = ConnectionManager(status_reader=read)
    catalog = manager.models("gemini")
    manager.set_default_model(
        "gemini",
        "gemini-3.1-pro-high",
        catalog_revision=catalog["catalog_revision"],
    )

    assert google_model_prefs.load_prefs()["task_models"]["chat"] == (
        "gemini-3.1-pro-high"
    )

    manager.reset_default_model(
        "gemini",
        confirmation="reset-model:gemini",
    )
    assert "chat" not in google_model_prefs.load_prefs()["task_models"]


def test_invalid_settings_file_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_CONFIG_DIR", str(tmp_path / "config"))
    path = provider_settings.settings_path()
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid"):
        provider_settings.update("gpt", {"model": "gpt-test"})

    assert path.read_text(encoding="utf-8") == "{broken"


def test_invalid_nested_provider_settings_are_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_CONFIG_DIR", str(tmp_path / "config"))
    path = provider_settings.settings_path()
    path.parent.mkdir(parents=True)
    original = '{"claude":"not-an-object"}'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="JSON objects"):
        provider_settings.update("claude", {"model": "claude-sonnet-5"})

    assert path.read_text(encoding="utf-8") == original
    assert provider_settings.inspect("claude") == ({}, "settings_invalid")


@pytest.mark.parametrize(
    "invalid",
    [
        {"model": ["claude-sonnet-5"]},
        {"temperature": True},
        {"max_tokens": False},
        {"api_mode": {"name": "responses"}},
        {"unknown": "value"},
    ],
)
def test_invalid_nested_provider_setting_values_are_not_overwritten(
    tmp_path,
    monkeypatch,
    invalid,
):
    monkeypatch.setenv("AGENT_HUB_CONFIG_DIR", str(tmp_path / "config"))
    path = provider_settings.settings_path()
    path.parent.mkdir(parents=True)
    original = json.dumps({"claude": invalid})
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError):
        provider_settings.update("gpt", {"model": "gpt-test"})

    assert path.read_text(encoding="utf-8") == original
    assert provider_settings.inspect("claude") == ({}, "settings_invalid")


@pytest.mark.parametrize(
    "changes",
    [
        {"model": ["claude-sonnet-5"]},
        {"temperature": True},
        {"temperature": float("inf")},
        {"max_tokens": False},
        {"max_tokens": 0},
    ],
)
def test_provider_setting_updates_reject_invalid_types_and_ranges(
    tmp_path,
    monkeypatch,
    changes,
):
    monkeypatch.setenv("AGENT_HUB_CONFIG_DIR", str(tmp_path / "config"))

    with pytest.raises(ValueError):
        provider_settings.update("claude", changes)

    assert provider_settings.inspect("claude") == ({}, None)


@pytest.mark.parametrize(
    ("provider", "env_name"),
    [
        ("claude", "CLAUDE_CODEX_CONFIG_DIR"),
        ("grok", "GROK_CODEX_CONFIG_DIR"),
        ("gemini", "GOOGLE_ANTIGRAVITY_CONFIG_DIR"),
        ("gpt", "OPENAI_CODEX_CONFIG_DIR"),
    ],
)
def test_gui_consent_requires_exact_visible_confirmation(
    tmp_path,
    monkeypatch,
    provider,
    env_name,
):
    config = tmp_path / provider
    monkeypatch.setenv(env_name, str(config))
    manager = ConnectionManager(status_reader=_reader({provider: _state()}))

    with pytest.raises(ConnectionError, match="동의 내용을 확인"):
        manager.grant_consent(provider, confirmation="yes")

    granted = manager.grant_consent(provider, confirmation=f"connect:{provider}")
    assert granted["consent"] is True
    assert (config / "user-consent.json").is_file()

    revoked = manager.revoke_consent(
        provider,
        confirmation=f"disconnect:{provider}",
    )
    assert revoked["consent"] is False
    assert not (config / "user-consent.json").exists()


def test_shared_official_logins_are_never_removed():
    manager = ConnectionManager(status_reader=_reader({"gpt": _state(authenticated=True)}))

    with pytest.raises(ConnectionError) as error:
        manager.remove_local_credentials("gpt", confirmation="forget-local:gpt")

    assert error.value.code == "shared_login_preserved"
    assert "공식 Codex" in str(error.value)


def test_login_start_requires_consent():
    manager = ConnectionManager(status_reader=_reader({"grok": _state()}))

    with pytest.raises(ConnectionError) as error:
        manager.start_login("grok")

    assert error.value.code == "consent_required"


def test_grok_login_returns_only_public_device_state(monkeypatch):
    states = {"grok": _state(consent=True)}
    manager = ConnectionManager(status_reader=_reader(states))
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.start_login",
        lambda **_kwargs: {
            "flow_id": "flow-grok-public",
            "verification_uri_complete": "https://accounts.x.ai/device?code=ABCD",
            "user_code": "ABCD",
            "device_code": "secret-device-code",
        },
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.complete_login",
        lambda **_kwargs: {"access_token": "secret-token"},
    )

    job = manager.start_login("grok")

    assert job["provider"] == "grok"
    assert job["action_url"].startswith("https://accounts.x.ai/")
    assert job["user_code"] == "ABCD"
    assert "device_code" not in job
    assert "flow_id" not in job
    assert "secret" not in str(job)

    deadline = time.time() + 1
    while manager.job(job["id"])["state"] != "complete" and time.time() < deadline:
        time.sleep(0.01)
    assert manager.job(job["id"])["state"] == "complete"


def test_grok_login_failure_does_not_expose_provider_error(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"grok": _state(consent=True)})
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.start_login",
        lambda **_kwargs: {
            "flow_id": "flow-grok-failure",
            "verification_uri": "https://auth.x.ai/device",
            "user_code": "ABCD",
        },
    )

    def fail_login(**_kwargs):
        raise RuntimeError("access_token=secret-token")

    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.complete_login",
        fail_login,
    )

    started = manager.start_login("grok")
    deadline = time.time() + 1
    job = manager.job(started["id"])
    while job["state"] != "failed" and time.time() < deadline:
        time.sleep(0.01)
        job = manager.job(started["id"])

    assert job["state"] == "failed"
    assert "secret-token" not in str(job)


def test_login_adapter_without_flow_id_fails_closed(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"grok": _state(consent=True)})
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.start_login",
        lambda **_kwargs: {
            "verification_uri": "https://accounts.x.ai/device",
            "user_code": "ABCD",
        },
    )

    with pytest.raises(ConnectionError) as error:
        manager.start_login("grok")

    assert error.value.code == "oauth_flow_id_missing"


def test_login_rejects_untrusted_provider_url(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"grok": _state(consent=True)})
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.start_login",
        lambda **_kwargs: {
            "flow_id": "flow-grok-untrusted",
            "verification_uri": "https://attacker.example/device",
            "user_code": "ABCD",
        },
    )

    with pytest.raises(ConnectionError) as error:
        manager.start_login("grok")

    assert error.value.code == "login_url_invalid"


def test_login_rejects_trusted_host_on_nonstandard_port(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"grok": _state(consent=True)})
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.start_login",
        lambda **_kwargs: {
            "flow_id": "flow-grok-port",
            "verification_uri": "https://accounts.x.ai:8443/device",
            "user_code": "ABCD",
        },
    )

    with pytest.raises(ConnectionError) as error:
        manager.start_login("grok")

    assert error.value.code == "login_url_invalid"


def test_grok_existing_pending_login_has_actionable_error(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"grok": _state(consent=True)})
    )

    def already_running(**_kwargs):
        raise grok_oauth.LoginInProgressError("active")

    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.start_login",
        already_running,
    )

    with pytest.raises(ConnectionError) as error:
        manager.start_login("grok")

    assert error.value.code == "login_in_progress"
    assert "다른 창" in str(error.value)


def test_gemini_falls_back_to_pasted_callback_when_local_port_is_busy(monkeypatch):
    states = {"gemini": _state(consent=True)}
    manager = ConnectionManager(status_reader=_reader(states))

    class BusyServer:
        def __init__(self, *_args, **_kwargs):
            raise OSError("busy")

    seen = {}

    def start_login(*, use_local_redirect):
        seen["local"] = use_local_redirect
        return {
            "flow_id": "flow-gemini-paste",
            "auth_url": "https://accounts.google.com/o/oauth2/auth",
        }

    monkeypatch.setattr("agent_hub.connect_service.http.server.HTTPServer", BusyServer)
    monkeypatch.setattr(
        "agent_hub.connect_service.google_oauth.start_login",
        start_login,
    )

    job = manager.start_login("gemini")

    assert seen["local"] is False
    assert job["requires_code"] is True
    assert job["action_url"].startswith("https://accounts.google.com/")


def test_gemini_existing_pending_login_has_actionable_error(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"gemini": _state(consent=True)})
    )

    class BusyServer:
        def __init__(self, *_args, **_kwargs):
            raise OSError("busy")

    def already_running(**_kwargs):
        raise google_oauth.OAuthLoginError(
            "active",
            code="oauth_login_in_progress",
        )

    monkeypatch.setattr(
        "agent_hub.connect_service.http.server.HTTPServer",
        BusyServer,
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.google_oauth.start_login",
        already_running,
    )

    with pytest.raises(ConnectionError) as error:
        manager.start_login("gemini")

    assert error.value.code == "login_in_progress"
    assert "다른 창" in str(error.value)


def test_gemini_local_callback_keeps_manual_completion_fallback(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"gemini": _state(consent=True)})
    )

    class StubServer:
        def __init__(self, *_args, **_kwargs):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr("agent_hub.connect_service.http.server.HTTPServer", StubServer)
    monkeypatch.setattr(
        "agent_hub.connect_service.google_oauth.start_login",
        lambda **_kwargs: {
            "flow_id": "flow-gemini-local",
            "auth_url": "https://accounts.google.com/o/oauth2/auth",
        },
    )
    monkeypatch.setattr(manager, "_install_google_callback", lambda *_args: None)

    job = manager.start_login("gemini")

    assert job["requires_code"] is True


def test_duplicate_provider_login_reuses_active_job(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"grok": _state(consent=True)})
    )
    release = threading.Event()
    calls = {"start": 0}

    def start_login(**_kwargs):
        calls["start"] += 1
        return {
            "flow_id": "flow-grok-duplicate",
            "verification_uri": "https://accounts.x.ai/device",
            "user_code": "ABCD",
        }

    def complete_login(*, cancel_event, expected_flow_id, commit_guard):
        assert expected_flow_id == "flow-grok-duplicate"
        assert callable(commit_guard)
        release.wait(timeout=1)
        if cancel_event.is_set():
            raise RuntimeError("cancelled")
        return {"success": True}

    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.start_login",
        start_login,
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.complete_login",
        complete_login,
    )

    first = manager.start_login("grok")
    second = manager.start_login("grok")

    assert second["id"] == first["id"]
    assert calls["start"] == 1
    manager.close()
    release.set()


def test_manager_close_clears_only_pending_login_it_started(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"grok": _state(consent=True)})
    )
    release = threading.Event()
    cleared = []
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.start_login",
        lambda **_kwargs: {
            "flow_id": "flow-grok-close",
            "verification_uri": "https://accounts.x.ai/device",
            "user_code": "ABCD",
        },
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.complete_login",
        lambda **_kwargs: release.wait(timeout=1),
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.clear_pending_login",
        lambda **kwargs: cleared.append(kwargs["expected_flow_id"]) or True,
    )

    manager.start_login("grok")
    manager.close()
    manager.close()
    release.set()

    assert cleared == ["flow-grok-close"]


def test_manager_close_fences_gemini_token_commit(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"gemini": _state(consent=True)})
    )
    entered = threading.Event()
    release = threading.Event()
    committed = threading.Event()

    class BusyServer:
        def __init__(self, *_args, **_kwargs):
            raise OSError("busy")

    monkeypatch.setattr(
        "agent_hub.connect_service.http.server.HTTPServer",
        BusyServer,
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.google_oauth.start_login",
        lambda **_kwargs: {
            "flow_id": "flow-gemini-close-commit",
            "auth_url": "https://accounts.google.com/o/oauth2/auth",
        },
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.google_oauth.clear_pending_login",
        lambda **_kwargs: True,
    )

    def complete_login(
        _value,
        *,
        probe,
        expected_flow_id,
        cancel_event,
        commit_guard,
    ):
        assert probe is False
        assert expected_flow_id == "flow-gemini-close-commit"
        entered.set()
        assert release.wait(timeout=5)
        with commit_guard():
            if cancel_event.is_set():
                raise google_oauth.OAuthLoginError(
                    "cancelled",
                    code="oauth_login_cancelled",
                )
            committed.set()
        return {"success": True}

    monkeypatch.setattr(
        "agent_hub.connect_service.google_oauth.complete_login",
        complete_login,
    )

    job = manager.start_login("gemini")
    manager.complete_login("gemini", job["id"], "callback-code")
    assert entered.wait(timeout=5)

    manager.close()
    release.set()

    deadline = time.time() + 5
    while (
        job["id"] in manager._cancel_events  # noqa: SLF001
        and time.time() < deadline
    ):
        time.sleep(0.01)

    assert committed.is_set() is False
    assert manager.job(job["id"])["state"] == "cancelled"


def test_create_job_rejects_after_close():
    manager = ConnectionManager(status_reader=_reader({"grok": _state()}))
    manager.close()

    with pytest.raises(ConnectionError) as error:
        manager._create_job(  # noqa: SLF001
            "grok",
            kind="login",
            state="waiting",
            message="waiting",
        )

    assert error.value.code == "manager_closed"
    assert manager._jobs == {}  # noqa: SLF001


def test_close_during_grok_start_cleans_unregistered_flow(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"grok": _state(consent=True)})
    )
    entered = threading.Event()
    release = threading.Event()
    cleared = []
    errors = []

    def blocked_start(**_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return {
            "flow_id": "flow-grok-close-race",
            "verification_uri": "https://accounts.x.ai/device",
            "user_code": "ABCD",
        }

    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.start_login",
        blocked_start,
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.clear_pending_login",
        lambda **kwargs: cleared.append(kwargs["expected_flow_id"]) or True,
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.complete_login",
        lambda **_kwargs: pytest.fail("completion worker must not start"),
    )

    def start() -> None:
        try:
            manager.start_login("grok")
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    worker = threading.Thread(target=start)
    worker.start()
    assert entered.wait(timeout=5)
    manager.close()
    release.set()
    worker.join(timeout=5)

    assert len(errors) == 1
    assert isinstance(errors[0], ConnectionError)
    assert errors[0].code == "manager_closed"
    assert cleared == ["flow-grok-close-race"]
    assert manager._jobs == {}  # noqa: SLF001
    assert manager._owned_pending_flows == {}  # noqa: SLF001


def test_close_during_gemini_start_closes_callback_and_flow(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"gemini": _state(consent=True)})
    )
    entered = threading.Event()
    release = threading.Event()
    cleared = []
    errors = []
    closed = []

    class StubServer:
        def __init__(self, *_args, **_kwargs):
            pass

        def server_close(self):
            closed.append(True)

    def blocked_start(**_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return {
            "flow_id": "flow-gemini-close-race",
            "auth_url": "https://accounts.google.com/o/oauth2/auth",
        }

    monkeypatch.setattr(
        "agent_hub.connect_service.http.server.HTTPServer",
        StubServer,
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.google_oauth.start_login",
        blocked_start,
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.google_oauth.clear_pending_login",
        lambda **kwargs: cleared.append(kwargs["expected_flow_id"]) or True,
    )
    monkeypatch.setattr(
        manager,
        "_install_google_callback",
        lambda *_args: pytest.fail("callback worker must not start"),
    )

    def start() -> None:
        try:
            manager.start_login("gemini")
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    worker = threading.Thread(target=start)
    worker.start()
    assert entered.wait(timeout=5)
    manager.close()
    release.set()
    worker.join(timeout=5)

    assert len(errors) == 1
    assert isinstance(errors[0], ConnectionError)
    assert errors[0].code == "manager_closed"
    assert cleared == ["flow-gemini-close-race"]
    assert closed == [True]
    assert manager._jobs == {}  # noqa: SLF001
    assert manager._callback_servers == {}  # noqa: SLF001


def test_closed_external_job_does_not_spawn_default_process(monkeypatch):
    manager = ConnectionManager(status_reader=_reader({"gpt": _state()}))
    job = manager._create_job(  # noqa: SLF001
        "gpt",
        kind="login",
        state="working",
        message="working",
    )
    manager.close()
    spawned = []

    def fail_spawn(*_args, **_kwargs):
        spawned.append(True)
        pytest.fail("process must not start")

    monkeypatch.setattr(
        "agent_hub.connect_service.subprocess.Popen",
        fail_spawn,
    )

    manager._run_external_login(job.id, ["/usr/local/bin/codex", "login"])  # noqa: SLF001

    assert spawned == []
    assert manager.job(job.id)["state"] == "cancelled"


def test_close_terminates_default_external_process_and_keeps_job_cancelled(
    monkeypatch,
):
    manager = ConnectionManager(
        status_reader=_reader(
            {"gpt": _state(consent=True, authenticated=True, ready=True)}
        )
    )
    started = threading.Event()
    terminated = threading.Event()
    kill_signals = []

    class FakeProcess:
        pid = 4242

        def poll(self):
            return -15 if terminated.is_set() else None

        def wait(self, *, timeout):
            if timeout == 10 * 60:
                started.set()
                assert terminated.wait(timeout=5)
            return -15

        def terminate(self):
            terminated.set()

        def kill(self):
            terminated.set()

    monkeypatch.setattr(
        "agent_hub.connect_service.shutil.which",
        lambda _command: "/usr/local/bin/codex",
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.subprocess.Popen",
        lambda *_args, **_kwargs: FakeProcess(),
    )

    def killpg(pid, sig):
        kill_signals.append((pid, sig))
        terminated.set()

    monkeypatch.setattr("agent_hub.connect_service.os.killpg", killpg)

    job = manager.start_login("gpt")
    assert started.wait(timeout=5)
    manager.close()

    deadline = time.time() + 5
    while manager._external_processes and time.time() < deadline:  # noqa: SLF001
        time.sleep(0.01)

    assert manager.job(job["id"])["state"] == "cancelled"
    if connect_service.os.name == "posix":
        assert kill_signals == [(4242, connect_service.signal.SIGTERM)]
    else:
        assert terminated.is_set()


def test_close_during_injected_runner_keeps_cancelled_terminal_state(
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()

    def run(_command, **_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return type("Result", (), {"returncode": 0})()

    manager = ConnectionManager(
        status_reader=_reader(
            {"gpt": _state(consent=True, authenticated=True, ready=True)}
        ),
        command_runner=run,
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.shutil.which",
        lambda _command: "/usr/local/bin/codex",
    )

    job = manager.start_login("gpt")
    assert entered.wait(timeout=5)
    manager.close()
    release.set()

    deadline = time.time() + 5
    while manager.job(job["id"])["state"] != "cancelled" and time.time() < deadline:
        time.sleep(0.01)

    assert manager.job(job["id"])["state"] == "cancelled"


def test_stale_pending_cleanup_does_not_remove_replacement_flow(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"grok": _state(consent=True)})
    )
    cleared = []
    manager._owned_pending_flows["grok"] = "replacement-flow"  # noqa: SLF001
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.clear_pending_login",
        lambda **kwargs: cleared.append(kwargs["expected_flow_id"]) or True,
    )

    manager._clear_owned_pending("grok", "stale-flow")  # noqa: SLF001

    assert cleared == []
    assert manager._owned_pending_flows["grok"] == "replacement-flow"  # noqa: SLF001


def test_pending_cleanup_keeps_ownership_for_retry_after_oserror(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader({"grok": _state(consent=True)})
    )
    manager._owned_pending_flows["grok"] = "owned-flow"  # noqa: SLF001
    attempts = 0

    def clear_pending_login(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("busy")
        return True

    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.clear_pending_login",
        clear_pending_login,
    )

    manager._clear_owned_pending("grok", "owned-flow")  # noqa: SLF001
    assert manager._owned_pending_flows["grok"] == "owned-flow"  # noqa: SLF001

    manager._clear_owned_pending("grok", "owned-flow")  # noqa: SLF001
    assert "grok" not in manager._owned_pending_flows  # noqa: SLF001


def test_connection_test_can_diagnose_authenticated_not_ready_provider(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader(
            {"gemini": _state(consent=True, authenticated=True, ready=False)}
        )
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.operations.dispatch_tool",
        lambda *_args, **_kwargs: {
            "success": True,
            "data": {"models": {"gemini": {"success": False}}},
        },
    )

    started = manager.start_test("gemini")
    deadline = time.time() + 1
    job = manager.job(started["id"])
    while job["state"] == "working" and time.time() < deadline:
        time.sleep(0.01)
        job = manager.job(started["id"])

    assert job["state"] == "failed"


def test_gemini_connection_test_requires_a_real_selected_model_response(
    monkeypatch,
):
    manager = ConnectionManager(
        status_reader=_reader(
            {
                "gemini": _state(
                    consent=True,
                    authenticated=True,
                    ready=True,
                    default_model="gemini-3.6-flash-high",
                )
            }
        )
    )
    calls: list[tuple[str, dict]] = []

    def dispatch(name, args):
        calls.append((name, args))
        return {
            "success": True,
            "provider": "gemini",
            "model": "gemini-3.6-flash-high",
            "text": "AGENT_HUB_CONNECTION_OK",
            "data": {"capacity_fallback": False},
        }

    monkeypatch.setattr(
        "agent_hub.connect_service.operations.dispatch_tool",
        dispatch,
    )

    started = manager.start_test("gemini")
    deadline = time.time() + 1
    job = manager.job(started["id"])
    while job["state"] == "working" and time.time() < deadline:
        time.sleep(0.01)
        job = manager.job(started["id"])

    assert job["state"] == "complete"
    assert [name for name, _args in calls] == ["agent_hub_chat"]
    probe = calls[-1][1]
    assert probe["provider"] == "gemini"
    assert probe["model"] == "gemini-3.6-flash-high"
    assert probe["max_tokens"] == 512
    assert probe["retry_count"] == 0
    assert probe["retry_sleep_cap_sec"] == 0
    assert probe["timeout_sec"] == 30
    assert probe["policy_mode"] == "off"


def test_gemini_connection_test_fails_when_generation_fails(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader(
            {
                "gemini": _state(
                    consent=True,
                    authenticated=True,
                    ready=True,
                    default_model="gemini-3.6-flash-high",
                )
            }
        )
    )

    def dispatch(_name, _args):
        return {
            "success": False,
            "error": {
                "type": "antigravity_http_404",
                "message": "provider body omitted",
            },
        }

    monkeypatch.setattr(
        "agent_hub.connect_service.operations.dispatch_tool",
        dispatch,
    )

    started = manager.start_test("gemini")
    deadline = time.time() + 1
    job = manager.job(started["id"])
    while job["state"] == "working" and time.time() < deadline:
        time.sleep(0.01)
        job = manager.job(started["id"])

    assert job["state"] == "failed"
    assert "provider body omitted" not in str(job)


def test_gemini_connection_test_requires_a_selected_model(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader(
            {
                "gemini": _state(
                    consent=True,
                    authenticated=True,
                    ready=True,
                    default_model="",
                )
            }
        )
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.operations.dispatch_tool",
        lambda *_args, **_kwargs: pytest.fail("generation must not start"),
    )

    with pytest.raises(ConnectionError) as error:
        manager.start_test("gemini")

    assert error.value.code == "model_unavailable"


def test_gemini_connection_test_fences_late_success_after_auth_change(
    monkeypatch,
):
    manager = ConnectionManager(
        status_reader=_reader(
            {
                "gemini": _state(
                    consent=True,
                    authenticated=True,
                    ready=True,
                    default_model="gemini-3.6-flash-high",
                )
            }
        )
    )
    entered = threading.Event()
    release = threading.Event()

    def dispatch(_name, _args):
        entered.set()
        release.wait(timeout=1)
        return {
            "success": True,
            "provider": "gemini",
            "model": "gemini-3.6-flash-high",
            "text": "AGENT_HUB_CONNECTION_OK",
            "data": {"capacity_fallback": False},
        }

    monkeypatch.setattr(
        "agent_hub.connect_service.operations.dispatch_tool",
        dispatch,
    )

    started = manager.start_test("gemini")
    assert entered.wait(timeout=1)
    manager._invalidate_model_catalog("gemini")  # noqa: SLF001
    release.set()

    deadline = time.time() + 1
    job = manager.job(started["id"])
    while job["state"] == "working" and time.time() < deadline:
        time.sleep(0.01)
        job = manager.job(started["id"])

    assert job["state"] == "failed"
    assert "다시" in job["message"]


def test_close_during_gemini_connection_test_keeps_cancelled_state(
    monkeypatch,
):
    manager = ConnectionManager(
        status_reader=_reader(
            {
                "gemini": _state(
                    consent=True,
                    authenticated=True,
                    ready=True,
                    default_model="gemini-3.6-flash-high",
                )
            }
        )
    )
    entered = threading.Event()
    release = threading.Event()

    def dispatch(_name, _args):
        entered.set()
        release.wait(timeout=1)
        return {
            "success": True,
            "provider": "gemini",
            "model": "gemini-3.6-flash-high",
            "text": "AGENT_HUB_CONNECTION_OK",
            "data": {"capacity_fallback": False},
        }

    monkeypatch.setattr(
        "agent_hub.connect_service.operations.dispatch_tool",
        dispatch,
    )

    started = manager.start_test("gemini")
    assert entered.wait(timeout=1)
    manager.close()
    release.set()
    time.sleep(0.02)

    job = manager.job(started["id"])
    assert job["state"] == "cancelled"
    assert "AGENT_HUB_CONNECTION_OK" not in str(job)


def test_connection_test_rejects_curated_fallback_false_positive(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader(
            {"claude": _state(consent=True, authenticated=True, ready=True)}
        )
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.operations.dispatch_tool",
        lambda *_args, **_kwargs: {
            "success": True,
            "data": {
                "models": {
                    "claude": {
                        "success": True,
                        "source": "curated",
                        "warnings": ["live_list_failed"],
                        "text_models": [{"id": "claude-sonnet-5"}],
                    }
                }
            },
        },
    )

    started = manager.start_test("claude")
    deadline = time.time() + 1
    job = manager.job(started["id"])
    while job["state"] == "working" and time.time() < deadline:
        time.sleep(0.01)
        job = manager.job(started["id"])

    assert job["state"] == "failed"


def test_provider_rejects_connection_test_while_login_is_active(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader(
            {"grok": _state(consent=True, authenticated=True, ready=True)}
        )
    )
    release = threading.Event()
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.start_login",
        lambda **_kwargs: {
            "flow_id": "flow-grok-test-block",
            "verification_uri": "https://accounts.x.ai/device",
            "user_code": "ABCD",
        },
    )

    def complete_login(*, cancel_event, expected_flow_id, commit_guard):
        assert expected_flow_id == "flow-grok-test-block"
        assert callable(commit_guard)
        release.wait(timeout=1)
        if cancel_event.is_set():
            raise RuntimeError("cancelled")
        return {"success": True}

    monkeypatch.setattr(
        "agent_hub.connect_service.grok_oauth.complete_login",
        complete_login,
    )

    manager.start_login("grok")

    with pytest.raises(ConnectionError) as error:
        manager.start_test("grok")

    assert error.value.code == "provider_busy"
    manager.close()
    release.set()


def test_provider_rejects_disconnect_and_local_removal_while_job_is_active():
    manager = ConnectionManager(
        status_reader=_reader(
            {"grok": _state(consent=True, authenticated=True, ready=True)}
        )
    )
    manager._create_job(  # noqa: SLF001 - state-machine regression boundary
        "grok",
        kind="login",
        state="waiting",
        message="waiting",
    )

    with pytest.raises(ConnectionError) as disconnect:
        manager.revoke_consent(
            "grok",
            confirmation="disconnect:grok",
        )
    with pytest.raises(ConnectionError) as removal:
        manager.remove_local_credentials(
            "grok",
            confirmation="forget-local:grok",
        )

    assert disconnect.value.code == "provider_busy"
    assert removal.value.code == "provider_busy"


def test_provider_rejects_destructive_actions_while_login_is_starting(monkeypatch):
    manager = ConnectionManager(
        status_reader=_reader(
            {"grok": _state(consent=True, authenticated=True, ready=True)}
        )
    )
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def blocked_start():
        entered.set()
        release.wait(timeout=5)
        return {"success": True}

    monkeypatch.setattr(manager, "_start_grok_login", blocked_start)

    def start():
        try:
            manager.start_login("grok")
        except Exception as exc:  # noqa: BLE001 - surfaced in the assertion
            errors.append(exc)

    worker = threading.Thread(target=start)
    worker.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(ConnectionError) as disconnect:
            manager.revoke_consent(
                "grok",
                confirmation="disconnect:grok",
            )
        with pytest.raises(ConnectionError) as removal:
            manager.remove_local_credentials(
                "grok",
                confirmation="forget-local:grok",
            )
        with pytest.raises(ConnectionError) as grant:
            manager.grant_consent(
                "grok",
                confirmation="connect:grok",
            )
        with pytest.raises(ConnectionError) as reset:
            manager.reset_default_model(
                "grok",
                confirmation="reset-model:grok",
            )
    finally:
        release.set()
        worker.join(timeout=5)

    assert errors == []
    assert disconnect.value.code == "provider_busy"
    assert removal.value.code == "provider_busy"
    assert grant.value.code == "provider_busy"
    assert reset.value.code == "provider_busy"


def test_consent_check_and_login_reservation_are_serialized(monkeypatch):
    status_entered = threading.Event()
    allow_status = threading.Event()
    start_entered = threading.Event()
    allow_start = threading.Event()
    revoke_done = threading.Event()
    errors = []

    def reader(provider="all", *, probe=False):
        if threading.current_thread().name == "login-start":
            status_entered.set()
            allow_status.wait(timeout=5)
        return {
            "providers": {
                "grok": _state(consent=True, authenticated=True, ready=True)
            },
            "probe": probe,
        }

    manager = ConnectionManager(status_reader=reader)

    def blocked_start():
        start_entered.set()
        allow_start.wait(timeout=5)
        return {"success": True}

    monkeypatch.setattr(manager, "_start_grok_login", blocked_start)
    monkeypatch.setattr(
        "agent_hub.connect_service.grok_security.revoke_consent",
        lambda: True,
    )

    login_worker = threading.Thread(
        target=lambda: manager.start_login("grok"),
        name="login-start",
    )

    def revoke():
        try:
            manager.revoke_consent("grok", confirmation="disconnect:grok")
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
        finally:
            revoke_done.set()

    revoke_worker = threading.Thread(target=revoke)
    login_worker.start()
    assert status_entered.wait(timeout=5)
    revoke_worker.start()
    assert revoke_done.wait(timeout=0.05) is False
    allow_status.set()
    assert start_entered.wait(timeout=5)
    assert revoke_done.wait(timeout=5)
    allow_start.set()
    login_worker.join(timeout=5)
    revoke_worker.join(timeout=5)

    assert len(errors) == 1
    assert isinstance(errors[0], ConnectionError)
    assert errors[0].code == "provider_busy"


@pytest.mark.parametrize(
    ("provider", "binary", "argv", "fallback"),
    [
        (
            "claude",
            "/usr/local/bin/claude",
            ["/usr/local/bin/claude", "auth", "login", "--claudeai"],
            "claude auth login --claudeai",
        ),
        (
            "gpt",
            "/usr/local/bin/codex",
            ["/usr/local/bin/codex", "login"],
            "codex login",
        ),
    ],
)
def test_authenticated_provider_can_start_reauthentication(
    monkeypatch,
    provider,
    binary,
    argv,
    fallback,
):
    state = _state(consent=True, authenticated=True, ready=True)
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return type("Result", (), {"returncode": 0})()

    manager = ConnectionManager(
        status_reader=_reader({provider: state}),
        command_runner=run,
    )
    monkeypatch.setattr(
        "agent_hub.connect_service.shutil.which",
        lambda _command: binary,
    )

    job = manager.start_login(provider)
    deadline = time.time() + 1
    while manager.job(job["id"])["state"] == "working" and time.time() < deadline:
        time.sleep(0.01)

    finished = manager.job(job["id"])
    assert finished["state"] == "complete"
    assert finished["fallback_command"] == fallback
    assert commands == [argv]


def test_job_ids_do_not_expose_internal_results(monkeypatch):
    states = {"claude": _state(consent=True, authenticated=True, ready=True)}
    manager = ConnectionManager(status_reader=_reader(states))
    monkeypatch.setattr(
        "agent_hub.connect_service.operations.dispatch_tool",
        lambda *_args, **_kwargs: {
            "success": True,
            "data": {
                "models": {
                    "claude": {
                        "success": True,
                        "source": "live",
                        "text_models": [{"id": "claude-sonnet-5"}],
                        "token": "never-return",
                    }
                }
            },
        },
    )

    started = manager.start_test("claude")
    deadline = time.time() + 1
    job = manager.job(started["id"])
    while job["state"] != "complete" and time.time() < deadline:
        time.sleep(0.01)
        job = manager.job(started["id"])

    assert job["state"] == "complete"
    assert "token" not in job
    assert "never-return" not in str(job)


def test_login_job_can_only_be_claimed_once_and_terminal_state_is_stable():
    manager = ConnectionManager(status_reader=_reader({"gemini": _state()}))
    job = manager._create_job(  # noqa: SLF001 - state-machine regression boundary
        "gemini",
        kind="login",
        state="waiting",
        message="waiting",
        requires_code=True,
    )

    assert manager._claim_job(  # noqa: SLF001
        job.id,
        expected_state="waiting",
        state="working",
        message="working",
    )
    assert not manager._claim_job(  # noqa: SLF001
        job.id,
        expected_state="waiting",
        state="working",
        message="second",
    )
    manager._update_job(job.id, state="complete", message="done")  # noqa: SLF001
    manager._update_job(job.id, state="failed", message="late failure")  # noqa: SLF001

    finished = manager.job(job.id)
    assert finished["state"] == "complete"
    assert finished["message"] == "done"
