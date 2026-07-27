from __future__ import annotations

import pytest

from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.routing import (
    AUTO_MAX_PRIOR_FRACTION,
    ROUTING_PROFILE_WEIGHTS,
    blend_statistics,
    profile_weights,
)
from agent_hub.v2.routing_prior import (
    PRIOR_DEFAULT_WEIGHT,
    apply_routing_prior_update,
    load_routing_prior,
    prepare_routing_prior_update,
    safe_load_routing_prior,
    seed_routing_prior,
)

_CLOCK = 1_800_000_000.0


def _clock():
    return _CLOCK


def _stats(**overrides):
    base = {
        "quality": 0.5,
        "reliability": 0.5,
        "failure_rate": None,
        "latency_ms": None,
        "total_tokens": None,
        "success_weight": 0.0,
        "failure_weight": 0.0,
        "observed_weight": 0.0,
        "quality_weight": 0.0,
        "latency_weight": 0.0,
        "tokens_weight": 0.0,
    }
    base.update(overrides)
    return base


def _entry(**overrides):
    base = {
        "capability": "write",
        "provider": "claude",
        "model": "",
        "source": "user_estimate",
        "quality": 0.9,
        "reliability": 0.95,
        "latency_ms": None,
        "total_tokens": None,
        "effective_weight": 6.0,
    }
    base.update(overrides)
    return base


def test_seed_template_ships_no_provider_performance_numbers():
    seeded = seed_routing_prior(collected_at="2026-07-27T00:00:00+00:00")

    assert seeded["entries"]
    for entry in seeded["entries"]:
        assert entry["source"] == "unset"
        # The contract of the template: no benchmark value ever ships in source.
        assert set(entry) == {"capability", "provider", "model", "source"}


def test_unset_entries_carry_no_weight_so_routing_is_unchanged(tmp_path):
    path = tmp_path / "routing_prior.toml"
    proposal = prepare_routing_prior_update(patch={}, expected_revision=0, path=path, clock=_clock)
    snapshot = apply_routing_prior_update(
        proposal=proposal,
        proposal_sha256=proposal["proposal_sha256"],
        path=path,
        clock=_clock,
    )

    assert snapshot.public()["active_entry_count"] == 0
    assert snapshot.lookup(capability="write", provider="claude", model=None) is None
    # Identical to today's behaviour: no prior contribution at all.
    assert blend_statistics(_stats(), None) == blend_statistics(
        _stats(),
        snapshot.lookup(capability="write", provider="claude", model=None),
    )


def test_prior_share_decays_as_observations_accumulate():
    prior = _entry(reliability=0.95, quality=0.9)

    cold = blend_statistics(_stats(), prior)
    warm = blend_statistics(
        _stats(success_weight=30.0, failure_weight=0.0, quality_weight=30.0, quality=0.4),
        prior,
    )

    assert cold["evidence"]["kind"] == "prior"
    assert warm["evidence"]["kind"] == "blended"
    assert warm["evidence"]["prior_fraction"] < cold["evidence"]["prior_fraction"]
    # Observed quality 0.4 pulls the blended value well below the 0.9 prior.
    assert warm["quality"] < 0.5


def test_a_few_bad_observations_override_an_optimistic_prior():
    prior = _entry(reliability=0.97)

    blended = blend_statistics(_stats(success_weight=0.0, failure_weight=15.0), prior)

    assert blended["reliability"] == pytest.approx(6.0 * 0.97 / 21.0, rel=1e-6)
    assert blended["reliability"] < 0.3
    assert blended["failure_rate"] == pytest.approx(1.0 - blended["reliability"], rel=1e-6)


def test_prior_dominated_evidence_exceeds_the_auto_guard_fraction():
    prior = _entry()

    thin = blend_statistics(_stats(success_weight=1.0), prior)

    assert thin["evidence"]["prior_fraction"] > AUTO_MAX_PRIOR_FRACTION


def test_quality_placeholder_contributes_no_uncertainty():
    without_evidence = blend_statistics(_stats(), None)
    with_evidence = blend_statistics(_stats(quality_weight=9.0, quality=0.8), None)

    assert without_evidence["evidence"]["quality_sd"] == 0.0
    assert with_evidence["evidence"]["quality_sd"] > 0.0


def test_model_wildcard_survives_a_default_model_change(tmp_path):
    path = tmp_path / "routing_prior.toml"
    proposal = prepare_routing_prior_update(
        patch={
            "collected_at": "2026-07-27T00:00:00+00:00",
            "entries": [
                {
                    "capability": "write",
                    "provider": "claude",
                    "model": "",
                    "source": "user_estimate",
                    "quality": 0.8,
                }
            ],
        },
        expected_revision=0,
        path=path,
        clock=_clock,
    )
    snapshot = apply_routing_prior_update(
        proposal=proposal,
        proposal_sha256=proposal["proposal_sha256"],
        path=path,
        clock=_clock,
    )

    assert snapshot.lookup(capability="write", provider="claude", model="claude-opus-5") is not None
    assert snapshot.lookup(capability="write", provider="claude", model="anything-else") is not None
    assert snapshot.lookup(capability="review", provider="claude", model=None) is None


def test_prepare_apply_is_revision_and_digest_fenced(tmp_path):
    path = tmp_path / "routing_prior.toml"
    proposal = prepare_routing_prior_update(patch={}, expected_revision=0, path=path, clock=_clock)

    with pytest.raises(HubV2Error) as digest:
        apply_routing_prior_update(
            proposal=proposal, proposal_sha256="wrong", path=path, clock=_clock
        )
    assert digest.value.code == "proposal_digest_conflict"

    applied = apply_routing_prior_update(
        proposal=proposal,
        proposal_sha256=proposal["proposal_sha256"],
        path=path,
        clock=_clock,
    )
    assert applied.revision == 1

    with pytest.raises(HubV2Error) as stale:
        prepare_routing_prior_update(patch={}, expected_revision=0, path=path, clock=_clock)
    assert stale.value.code == "routing_prior_revision_conflict"


def test_a_broken_prior_file_never_breaks_routing(tmp_path):
    path = tmp_path / "routing_prior.toml"
    path.write_text("this is not valid toml = = =\n", encoding="utf-8")

    with pytest.raises(HubV2Error):
        load_routing_prior(path, clock=_clock)

    snapshot = safe_load_routing_prior(path, clock=_clock)

    assert snapshot.state == "invalid"
    assert snapshot.reason_code == "invalid_routing_prior"
    assert snapshot.entries == ()
    assert snapshot.lookup(capability="write", provider="claude", model=None) is None


def test_stale_priors_decay_instead_of_vanishing(tmp_path):
    path = tmp_path / "routing_prior.toml"
    old = _CLOCK - 200 * 86400.0
    from datetime import datetime, timezone

    collected = datetime.fromtimestamp(old, tz=timezone.utc).isoformat()
    proposal = prepare_routing_prior_update(
        patch={
            "collected_at": collected,
            "stale_after_days": 90.0,
            "entries": [
                {
                    "capability": "write",
                    "provider": "claude",
                    "model": "",
                    "source": "user_estimate",
                    "quality": 0.8,
                }
            ],
        },
        expected_revision=0,
        path=path,
        clock=_clock,
    )
    snapshot = apply_routing_prior_update(
        proposal=proposal,
        proposal_sha256=proposal["proposal_sha256"],
        path=path,
        clock=_clock,
    )

    entry = snapshot.lookup(capability="write", provider="claude", model=None)
    assert snapshot.stale is True
    assert entry is not None
    assert 0.0 < entry["effective_weight"] < PRIOR_DEFAULT_WEIGHT


def test_routing_profiles_change_the_weights_they_advertise():
    balanced = profile_weights("quality_balanced")
    latency = profile_weights("latency_first")
    cost = profile_weights("cost_first")

    assert balanced["quality"] == 0.60
    assert latency["latency"] > balanced["latency"]
    assert cost["token_efficiency"] > balanced["token_efficiency"]
    for weights in ROUTING_PROFILE_WEIGHTS.values():
        assert sum(weights.values()) == pytest.approx(1.0)

    with pytest.raises(HubV2Error) as unknown:
        profile_weights("does_not_exist")
    assert unknown.value.code == "invalid_routing_profile"


def test_prior_rejects_unknown_fields_and_out_of_range_values(tmp_path):
    path = tmp_path / "routing_prior.toml"
    path.write_text(
        'schema = "agent_hub_routing_prior_v1"\nrevision = 0\nsurprise = 1\n',
        encoding="utf-8",
    )
    with pytest.raises(HubV2Error) as unknown:
        load_routing_prior(path, clock=_clock)
    assert unknown.value.code == "invalid_routing_prior"

    path.write_text(
        'schema = "agent_hub_routing_prior_v1"\nrevision = 0\n\n'
        "[[entries]]\n"
        'capability = "write"\nprovider = "claude"\nmodel = ""\n'
        'source = "user_estimate"\nquality = 4.2\n',
        encoding="utf-8",
    )
    with pytest.raises(HubV2Error) as out_of_range:
        load_routing_prior(path, clock=_clock)
    assert out_of_range.value.code == "invalid_routing_prior"


def _record(store, *, provider, capability, success, quality, latency_ms, count):
    from agent_hub.v2.routing import routing_context
    from agent_hub.v2.contracts import TASK_SCHEMA

    task = {
        "schema": TASK_SCHEMA,
        "intent": "Write the fixture.",
        "capability": capability,
        "inline_input": "x",
    }
    for _ in range(count):
        store.record_routing_sample(
            context=routing_context(task, model=""),
            provider=provider,
            model="",
            capability=capability,
            success=success,
            quality=quality,
            latency_ms=latency_ms,
            total_tokens=1_000,
            signal_weight=3.0,
        )


def test_auto_promotes_on_observed_evidence_at_the_lowered_sample_floor(tmp_path):
    from agent_hub.v2.contracts import TASK_SCHEMA
    from agent_hub.v2.routing import AUTO_MIN_OBSERVED_SAMPLES, route
    from agent_hub.v2.store import HubStore

    store = HubStore(tmp_path / "state.sqlite3")
    _record(
        store,
        provider="claude",
        capability="write",
        success=True,
        quality=0.95,
        latency_ms=1_000,
        count=AUTO_MIN_OBSERVED_SAMPLES + 1,
    )
    _record(
        store,
        provider="gpt",
        capability="write",
        success=True,
        quality=0.50,
        latency_ms=1_000,
        count=AUTO_MIN_OBSERVED_SAMPLES + 1,
    )

    decision = route(
        store=store,
        task={
            "schema": TASK_SCHEMA,
            "intent": "Write the fixture.",
            "capability": "write",
            "inline_input": "x",
        },
        planner_provider="gpt",
        routing_mode="auto",
        provider_allowlist=["claude", "gpt"],
        readiness={"claude": True, "gpt": True},
    )

    # 20 samples per candidate was unreachable at this project's usage rate; 5 is.
    assert decision["selected_provider"] == "claude"
    assert decision["reason_code"] == "statistical_auto"
    assert decision["evidence_kind"] == "observed"


def test_auto_refuses_to_promote_on_prior_evidence_alone(tmp_path):
    from datetime import datetime, timezone

    from agent_hub.v2.contracts import TASK_SCHEMA
    from agent_hub.v2.routing import AUTO_MIN_OBSERVED_SAMPLES, route
    from agent_hub.v2.store import HubStore

    store = HubStore(tmp_path / "state.sqlite3")
    for provider in ("claude", "gpt"):
        _record(
            store,
            provider=provider,
            capability="write",
            success=True,
            quality=None,
            latency_ms=1_000,
            count=AUTO_MIN_OBSERVED_SAMPLES + 1,
        )

    path = tmp_path / "routing_prior.toml"
    proposal = prepare_routing_prior_update(
        patch={
            "collected_at": datetime.fromtimestamp(_CLOCK, tz=timezone.utc).isoformat(),
            "prior_weight": 30.0,
            "entries": [
                {
                    "capability": "write",
                    "provider": "claude",
                    "model": "",
                    "source": "user_estimate",
                    "quality": 0.99,
                    "reliability": 0.99,
                }
            ],
        },
        expected_revision=0,
        path=path,
        clock=_clock,
    )
    prior = apply_routing_prior_update(
        proposal=proposal,
        proposal_sha256=proposal["proposal_sha256"],
        path=path,
        clock=_clock,
    )

    decision = route(
        store=store,
        task={
            "schema": TASK_SCHEMA,
            "intent": "Write the fixture.",
            "capability": "write",
            "inline_input": "x",
        },
        planner_provider="gpt",
        routing_mode="auto",
        provider_allowlist=["claude", "gpt"],
        readiness={"claude": True, "gpt": True},
        prior=prior,
    )

    # The prior makes claude look best, but assumption alone must not promote it.
    assert decision["selected_provider"] == "gpt"
    assert decision["reason_code"] == "prior_evidence_insufficient"
