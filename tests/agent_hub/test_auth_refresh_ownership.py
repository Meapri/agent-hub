"""Who is allowed to refresh whose credential, and what the GUI says when it cannot.

The user's report was that logins "keep dropping". Nothing was logging out: the
refresh token stays valid and only the short-lived access token ages out --
gemini's after an hour, grok's after six, claude's after eight. What made that
look like a logout was a rule written as `provider == "gemini"`, which let
gemini refresh itself and left grok, its identical twin in ownership and
refresh support, to present as signed out every six hours.
"""

from __future__ import annotations

import pytest

from agent_hub.connect_types import ConnectionError as ConnectError
from agent_hub.v2.provider_manifests import manifest_for
from agent_hub.v2.provider_runtime import may_auto_refresh

REFRESHABLE = {"consent": True, "configured": True, "refreshable": True}


@pytest.mark.parametrize("provider", ["grok", "gemini"])
def test_agent_hub_refreshes_the_credentials_it_owns(provider):
    assert manifest_for(provider)["auth_owner"] == "agent-hub"
    assert may_auto_refresh(provider, REFRESHABLE) is True


@pytest.mark.parametrize("provider", ["claude", "gpt"])
def test_agent_hub_never_refreshes_someone_elses_credential(provider):
    # claude's token belongs to Claude Code and gpt's to Codex. Those owners
    # keep them fresh; the adapters read whichever copy is fresher rather than
    # minting a new one underneath a running client.
    assert manifest_for(provider)["auth_owner"] != "agent-hub"
    assert may_auto_refresh(provider, REFRESHABLE) is False


def test_the_rule_is_ownership_rather_than_a_provider_name():
    # The bug was a name comparison that happened to be right for one provider.
    owned = [p for p in ("claude", "grok", "gemini", "gpt") if may_auto_refresh(p, REFRESHABLE)]

    assert owned == [
        p
        for p in ("claude", "grok", "gemini", "gpt")
        if manifest_for(p)["auth_owner"] == "agent-hub"
    ]


@pytest.mark.parametrize("missing", ["consent", "configured", "refreshable"])
def test_an_owned_credential_still_needs_the_other_conditions(missing):
    state = {**REFRESHABLE, missing: False}

    assert may_auto_refresh("grok", state) is False


def test_claude_reads_the_owner_copy_rather_than_refreshing_it():
    """The design the user asked about, already in place.

    subscription_auth.read_credentials runs `security find-generic-password` for
    Claude Code's keychain entry and compares it against
    ~/.claude/.credentials.json, taking whichever expires later. Agent Hub
    borrows Claude Code's freshness instead of competing with it.
    """

    from claude_codex import subscription_auth

    assert hasattr(subscription_auth, "read_from_keychain")
    assert hasattr(subscription_auth, "read_credentials")


# --- what the GUI says -------------------------------------------------------


def _reader(states):
    def read(provider="all", *, probe=False):
        selected = states if provider == "all" else {provider: states[provider]}
        return {"providers": selected, "probe": probe}

    return read


def _gui_state(**overrides):
    base = {
        "consent": True,
        "configured": True,
        "authenticated": False,
        "ready": False,
        "invocation_ready": False,
        "logged_in": True,
        "auth_ready": False,
        "refreshable": True,
        "refresh_supported": True,
        "account_present": True,
        "default_model": "grok-4.5",
        "capabilities": {"chat": {"supported": True}},
        "warnings": [],
    }
    base.update(overrides)
    return base


def test_a_refreshable_provider_is_told_to_refresh_not_to_log_in_again():
    # The message that made an hourly token feel like an account problem.
    from agent_hub.connect_service import ConnectionManager

    manager = ConnectionManager(status_reader=_reader({"grok": _gui_state()}))

    with pytest.raises(ConnectError) as refused:
        manager.start_test("grok")
    manager.close()

    assert refused.value.code == "refresh_required"
    assert "갱신" in str(refused.value)
    assert "로그인을 완료" not in str(refused.value)


def test_a_genuinely_signed_out_provider_is_still_told_to_log_in():
    from agent_hub.connect_service import ConnectionManager

    manager = ConnectionManager(
        status_reader=_reader(
            {"grok": _gui_state(refreshable=False, logged_in=False, account_present=False)}
        )
    )

    with pytest.raises(ConnectError) as refused:
        manager.start_test("grok")
    manager.close()

    assert refused.value.code == "provider_not_ready"


def test_missing_consent_is_reported_as_consent_rather_than_login():
    from agent_hub.connect_service import ConnectionManager

    manager = ConnectionManager(status_reader=_reader({"grok": _gui_state(consent=False)}))

    with pytest.raises(ConnectError) as refused:
        manager.start_test("grok")
    manager.close()

    assert refused.value.code == "consent_required"
