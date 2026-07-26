from __future__ import annotations

from threading import Barrier

import pytest

from agent_hub import consistency
from agent_hub.core import parallel


def _policy_root(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# Rules\n\n- Preserve facts.\n",
        encoding="utf-8",
    )
    return str(tmp_path)


def test_policy_injection_and_provenance_are_stable(tmp_path):
    root = _policy_root(tmp_path)
    first, first_meta = consistency.prepare_provider_call(
        {"prompt": "hello", "project_root": root, "policy_mode": "required"}
    )
    second, second_meta = consistency.prepare_provider_call(
        {"prompt": "hello", "project_root": root, "policy_mode": "required"}
    )

    assert "<agent-hub-canonical-policy" in first["system"]
    assert "Preserve facts." in first["system"]
    assert first == second
    assert first_meta["policy_sha256"] == second_meta["policy_sha256"]
    assert first_meta["request_sha256"] == second_meta["request_sha256"]


def test_policy_loader_is_fail_closed_and_confined(tmp_path):
    with pytest.raises(ValueError, match="canonical policy is required"):
        consistency.load_policy(project_root=str(tmp_path), required=True)
    outside = tmp_path.parent / "outside-policy.md"
    outside.write_text("private", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="inside project_root"):
            consistency.load_policy(
                project_root=str(tmp_path),
                policy_file=str(outside),
                required=True,
            )
    finally:
        outside.unlink(missing_ok=True)


def test_decision_parser_is_strict():
    parsed = consistency.parse_decision(
        '{"schema":"decision_v1","label":"ACCEPT","confidence":0.9,"rationale":"checked"}',
        ["ACCEPT", "REJECT"],
    )
    assert parsed["label"] == "ACCEPT"
    with pytest.raises(ValueError, match="label is not"):
        consistency.parse_decision(
            '{"schema":"decision_v1","label":"MAYBE","confidence":0.9}',
            ["ACCEPT", "REJECT"],
        )


def test_parallel_runner_overlaps_calls_and_isolates_failure():
    barrier = Barrier(2)

    def call(value):
        barrier.wait(timeout=2)
        return value

    outcomes = parallel.run_ordered(
        [lambda: call("first"), lambda: call("second")],
        execution="parallel",
        max_workers=2,
    )
    assert [item.value for item in outcomes] == ["first", "second"]

    def broken():
        raise RuntimeError("provider down")

    isolated = parallel.run_ordered([lambda: "ok", broken], max_workers=2)
    assert isolated[0].value == "ok"
    assert isolated[1].value is None
    assert isinstance(isolated[1].error, RuntimeError)
