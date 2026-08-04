"""A provider that expires must be able to come back on its own.

Observed on this machine: grok reported `logged_in: true, refreshable: true,
ready: false` and stayed that way. Refreshing only ever happened as a side
effect of a call landing inside a narrow window before expiry, and there was no
timer anywhere -- `daemon.py` had one loop, for lease recovery. So an expired
provider was excluded from routing, which meant it was never called, which
meant it never refreshed, which meant it stayed excluded. Spawning one fresh
worker refreshed it and everything went green.

The other half is the credentials Agent Hub does not own. gpt reported
`account_present: false` because Codex was signed out, and routing said "No
ready provider satisfies the task policy" -- true, and useless, when one
command fixes it.
"""

from __future__ import annotations

import pytest

from agent_hub.v2 import provider_runtime
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.provider_selection import select_provider
from agent_hub.v2.service import HubService

STALE = {"consent": True, "configured": True, "refreshable": True, "ready": False}
FRESH = {"consent": True, "configured": True, "refreshable": False, "ready": True}


# --- one entry point, because two callers need it ---------------------------


def test_agent_hub_renews_only_what_it_owns():
    for provider in ("grok", "gemini"):
        assert provider_runtime.login_instruction(provider) is None
    for provider, owner in (("gpt", "Codex"), ("claude", "Claude Code")):
        assert provider_runtime.login_instruction(provider)["owner"] == owner


@pytest.mark.parametrize(("provider", "command"), [("gpt", "codex login"), ("claude", "claude")])
def test_renewing_a_foreign_credential_names_its_owner(provider, command):
    with pytest.raises(HubV2Error) as refused:
        provider_runtime.renew_auth(provider)

    assert refused.value.code == "provider_login_required"
    assert command in refused.value.safe_details["command"]


def test_a_refresh_failure_never_repeats_the_provider_message(monkeypatch):
    monkeypatch.setattr(
        provider_runtime.grok_oauth,
        "force_refresh_access_token",
        lambda: (_ for _ in ()).throw(RuntimeError("token endpoint said 401 for user@example.com")),
    )

    with pytest.raises(HubV2Error) as failed:
        provider_runtime.renew_auth("grok")

    assert failed.value.code == "provider_refresh_failed"
    assert "example.com" not in failed.value.message
    assert failed.value.safe_details["reason_code"] == "RuntimeError"


# --- the invoke path makes good on invocation_ready -------------------------


def test_a_stale_owned_credential_is_renewed_before_the_call(monkeypatch):
    """Routing lets a refreshable provider through, so something has to renew
    it. Nothing did: the call went out with the expired token."""

    renewed: list[str] = []
    monkeypatch.setattr(
        provider_runtime,
        "status",
        lambda provider, **_: {"data": {"providers": {provider: STALE}}},
    )
    monkeypatch.setattr(provider_runtime, "renew_auth", lambda provider: renewed.append(provider))

    provider_runtime.ensure_usable_auth("grok")

    assert renewed == ["grok"]


def test_a_ready_provider_is_not_renewed_on_every_call(monkeypatch):
    renewed: list[str] = []
    monkeypatch.setattr(
        provider_runtime,
        "status",
        lambda provider, **_: {"data": {"providers": {provider: FRESH}}},
    )
    monkeypatch.setattr(provider_runtime, "renew_auth", lambda provider: renewed.append(provider))

    provider_runtime.ensure_usable_auth("grok")

    assert renewed == []


def test_a_signed_out_foreign_credential_says_what_to_run(monkeypatch):
    monkeypatch.setattr(
        provider_runtime,
        "status",
        lambda provider, **_: {
            "data": {"providers": {provider: {"ready": False, "logged_in": False}}}
        },
    )

    with pytest.raises(HubV2Error) as refused:
        provider_runtime.ensure_usable_auth("gpt")

    assert refused.value.code == "provider_login_required"
    assert refused.value.safe_details["command"] == "codex login"
    assert "codex login" in refused.value.message


# --- and the daemon renews on a clock rather than on demand -----------------


class _RenewalWorker:
    calls: list[tuple[str, str]] = []
    state = dict(STALE)

    def __init__(self, provider):
        self.provider = provider

    def request(self, method, params=None, timeout=30.0, request_id=None):
        type(self).calls.append((self.provider, method))
        if method == "status":
            return {"success": True, "data": {"providers": {self.provider: dict(self.state)}}}
        if method == "renew":
            return {"success": True, "data": {}}
        raise AssertionError(method)

    def cancel(self):
        return True


def _service(tmp_path, worker):
    from agent_hub.v2.crypto import ArtifactCipher, StaticKeyProvider
    from agent_hub.v2.store import HubStore

    return HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=worker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )


def test_the_renewal_pass_refreshes_stale_owned_credentials(tmp_path):
    _RenewalWorker.calls = []
    _RenewalWorker.state = dict(STALE)

    outcomes = _service(tmp_path, _RenewalWorker).renew_owned_credentials()

    assert outcomes == {"grok": "renewed", "gemini": "renewed"}
    assert ("grok", "renew") in _RenewalWorker.calls


def test_the_renewal_pass_leaves_foreign_credentials_alone(tmp_path):
    """Attempting one would only produce a failure the user cannot act on
    differently, and it would touch another application's session."""

    _RenewalWorker.calls = []
    _RenewalWorker.state = dict(STALE)

    outcomes = _service(tmp_path, _RenewalWorker).renew_owned_credentials()

    assert "gpt" not in outcomes
    assert "claude" not in outcomes
    assert not [item for item in _RenewalWorker.calls if item[0] in {"gpt", "claude"}]


def test_a_fresh_credential_is_not_refreshed(tmp_path):
    _RenewalWorker.calls = []
    _RenewalWorker.state = dict(FRESH)

    outcomes = _service(tmp_path, _RenewalWorker).renew_owned_credentials()

    assert set(outcomes.values()) == {"not_due"}
    assert not [item for item in _RenewalWorker.calls if item[1] == "renew"]


def test_one_failing_provider_does_not_stop_the_others(tmp_path):
    class _HalfBroken(_RenewalWorker):
        def request(self, method, params=None, timeout=30.0, request_id=None):
            if self.provider == "grok" and method == "renew":
                raise HubV2Error("provider_refresh_failed", "no", scope="provider")
            return super().request(method, params=params, timeout=timeout)

    _RenewalWorker.calls = []
    _RenewalWorker.state = dict(STALE)

    outcomes = _service(tmp_path, _HalfBroken).renew_owned_credentials()

    assert outcomes["grok"] == "provider_refresh_failed"
    assert outcomes["gemini"] == "renewed"


def test_the_daemon_runs_the_pass_on_a_clock():
    """The loop is the fix. Renewing only on demand is what created the trap."""

    import inspect

    from agent_hub.v2 import daemon

    source = inspect.getsource(daemon.HubDaemon)
    assert "_renewal_loop" in source
    assert "renew_owned_credentials" in source
    # A pass has to land before the shortest credential Agent Hub owns expires.
    assert daemon.CREDENTIAL_RENEWAL_INTERVAL_SECONDS < 3600
    # And the thread must be joined, like the recovery thread beside it.
    assert "self._renewal_thread.join" in inspect.getsource(daemon.HubDaemon.close)


# --- routing says which sign-in is missing ----------------------------------


def _selection(**overrides):
    base = {
        "task": {"schema": "task_v2", "capability": "chat", "intent": "hi", "inline_input": ""},
        "planner_provider": "gpt",
        "provider_allowlist": ["gpt", "claude"],
        "readiness": {"gpt": False, "claude": False, "grok": False, "gemini": False},
    }
    return select_provider(**{**base, **overrides})


def test_routing_names_the_sign_in_when_that_is_all_that_is_wrong():
    with pytest.raises(HubV2Error) as refused:
        _selection(login_commands={"gpt": "codex login", "claude": "claude auth login --claudeai"})

    assert refused.value.code == "provider_login_required"
    assert "codex login" in refused.value.message
    assert set(refused.value.safe_details["providers"]) == {"gpt", "claude"}


def test_a_policy_exclusion_still_reports_itself():
    """Only signed-out candidates may be reported as a sign-in problem, or a
    genuine policy or capability exclusion gets the wrong remedy."""

    with pytest.raises(HubV2Error) as refused:
        _selection(login_commands={"gpt": "codex login"})

    assert refused.value.code == "no_eligible_provider"


def test_nothing_changes_when_no_commands_are_supplied():
    with pytest.raises(HubV2Error) as refused:
        _selection()

    assert refused.value.code == "no_eligible_provider"


@pytest.mark.parametrize(
    "capability", ["chat", "review", "decide", "vision", "search", "write", "image"]
)
def test_every_capability_checks_auth_before_calling_out(monkeypatch, capability):
    """Having the guard is not the same as calling it.

    Removing the one line from `invoke` left every test above still passing,
    which is the shape of defect this repository keeps producing: the rule
    exists, nothing checks that it is wired in.
    """

    checked: list[str] = []
    monkeypatch.setattr(provider_runtime, "ensure_usable_auth", lambda p: checked.append(p))
    for name in ("chat", "search", "write", "generate_image"):
        monkeypatch.setattr(provider_runtime, name, lambda *_a, **_k: {"success": True})

    provider_runtime.invoke("grok", capability, {})

    assert checked == ["grok"]
