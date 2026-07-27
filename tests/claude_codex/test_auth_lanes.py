"""Which credential the claude provider actually uses, and whether it says so.

Two lanes exist -- subscription OAuth and an Anthropic API key -- and they are
not interchangeable: they are billed against different things and send
different headers. The resolver falls back between them, which is the right
behaviour (serving the request beats failing it) and also the dangerous one,
because a caller who asked for one lane and silently got the other finds out
from a bill.

Before this file the repository had exactly one lane test, covering the
subscription-preferred happy path. The fallback, the api_key mode, the
OAuth-shaped-key branch, and the file-based key were all uncovered.
"""

from __future__ import annotations

import pytest

from claude_codex import auth


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No ambient credentials: every test states what exists."""

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODEX_AUTH_MODE", raising=False)
    monkeypatch.setattr(auth.paths, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(auth.subscription_auth, "resolve_access_token", lambda: None)
    monkeypatch.setattr(
        auth.subscription_auth,
        "status",
        lambda: {"logged_in": False, "token_valid": False},
    )


def _with_subscription(monkeypatch, *, valid=True):
    monkeypatch.setattr(
        auth.subscription_auth,
        "resolve_access_token",
        lambda: {"access_token": "oauth-token", "source": "keychain"},
    )
    monkeypatch.setattr(
        auth.subscription_auth,
        "status",
        lambda: {"logged_in": True, "token_valid": valid, "has_refresh_token": True},
    )


# --- which lane is chosen ----------------------------------------------------


def test_subscription_is_preferred_by_default(monkeypatch):
    _with_subscription(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-real-key")

    resolved = auth.resolve_auth()

    assert resolved["mode"] == "subscription_oauth"
    assert resolved.get("lane_substituted") is None


def test_api_key_mode_uses_the_key_when_one_exists(monkeypatch):
    _with_subscription(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODEX_AUTH_MODE", "api_key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-real-key")

    resolved = auth.resolve_auth()

    assert resolved["mode"] == "api_key"
    assert resolved["api_key"] == "sk-ant-api-real-key"


def test_a_key_from_the_config_file_is_used_when_the_environment_has_none(monkeypatch, tmp_path):
    # This is the lane that works inside the sandboxed worker: the environment
    # allowlist does not pass ANTHROPIC_API_KEY through, but the config file is
    # readable there.
    monkeypatch.setenv("CLAUDE_CODEX_AUTH_MODE", "api_key")
    (tmp_path / "api-key").write_text("sk-ant-api-file-key\n", encoding="utf-8")

    resolved = auth.resolve_auth()

    assert resolved["mode"] == "api_key"
    assert resolved["api_key"] == "sk-ant-api-file-key"
    assert resolved["source"] == str(tmp_path / "api-key")


@pytest.mark.parametrize("prefix", ["sk-ant-oat", "sk-ant-ort"])
def test_an_oauth_shaped_key_is_treated_as_the_oauth_lane(monkeypatch, prefix):
    # These are subscription tokens that happen to arrive through the API key
    # variable. Sending them with an x-api-key header would simply fail.
    monkeypatch.setenv("CLAUDE_CODEX_AUTH_MODE", "api_key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", f"{prefix}01-token")

    resolved = auth.resolve_auth()

    assert resolved["mode"] == "subscription_oauth"
    assert resolved["access_token"] == f"{prefix}01-token"


# --- and whether the substitution is admitted --------------------------------


def test_falling_back_to_the_subscription_is_reported_not_hidden(monkeypatch):
    """The defect this file exists for.

    Asking for api_key with no key available used to return a subscription
    context indistinguishable from one that had been chosen.
    """

    _with_subscription(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODEX_AUTH_MODE", "api_key")

    resolved = auth.resolve_auth()

    assert resolved["mode"] == "subscription_oauth"
    assert resolved["requested_mode"] == "api_key"
    assert resolved["lane_substituted"] is True


def test_status_admits_the_substitution_too(monkeypatch):
    _with_subscription(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODEX_AUTH_MODE", "api_key")

    reported = auth.status()

    assert reported["ready"] is True
    assert reported["requested_mode"] == "api_key"
    assert reported["lane_substituted"] is True
    assert "not the requested api_key" in reported["text"]


def test_status_stays_quiet_when_the_lane_is_the_requested_one(monkeypatch):
    _with_subscription(monkeypatch)

    reported = auth.status()

    assert reported["lane_substituted"] is False
    assert "not the requested" not in reported["text"]


def test_no_credentials_at_all_fails_rather_than_substituting(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODEX_AUTH_MODE", "api_key")

    with pytest.raises(RuntimeError):
        auth.resolve_auth()
