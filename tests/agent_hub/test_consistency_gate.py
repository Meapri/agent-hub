from __future__ import annotations

from threading import Barrier

import pytest

from agent_hub import consistency, operations
from agent_hub.core import parallel


def _policy_root(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Rules\n\n- Preserve facts.\n", encoding="utf-8")
    return str(tmp_path)


def _decision(label: str) -> str:
    return '{"schema":"decision_v1","label":"' + label + '","confidence":0.9,"rationale":"checked"}'


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
    assert "governs behavior and process" in first["system"]
    assert "current repository evidence wins" in first["system"]
    assert first == second
    assert first_meta["policy_sha256"] == second_meta["policy_sha256"]
    assert first_meta["request_sha256"] == second_meta["request_sha256"]


def test_policy_loader_is_fail_closed_and_confined(tmp_path):
    with pytest.raises(ValueError, match="canonical policy is required"):
        consistency.load_policy(project_root=str(tmp_path), required=True)
    outside = tmp_path.parent / "outside-policy.md"
    outside.write_text("secret", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="inside project_root"):
            consistency.load_policy(
                project_root=str(tmp_path), policy_file=str(outside), required=True
            )
    finally:
        outside.unlink(missing_ok=True)


def test_decision_v1_parser_is_strict():
    labels = ["ACCEPT", "REJECT", "UNCERTAIN"]
    parsed = consistency.parse_decision(_decision("ACCEPT"), labels)
    assert parsed["label"] == "ACCEPT"
    fenced = consistency.parse_decision(f"```json\n{_decision('REJECT')}\n```", labels)
    assert fenced["label"] == "REJECT"
    with pytest.raises(ValueError, match="label is not"):
        consistency.parse_decision(_decision("MAYBE"), labels)
    with pytest.raises(ValueError, match="invalid decision_v1 JSON"):
        consistency.parse_decision("answer: ACCEPT", labels)
    with pytest.raises(ValueError, match="unsupported fields"):
        consistency.parse_decision(
            '{"schema":"decision_v1","label":"ACCEPT","confidence":1,"extra":true}',
            labels,
        )


def test_decision_prompt_prefers_the_minimal_complete_contract():
    prompt = consistency.decision_prompt("Review this.", ["ACCEPT", "REJECT"])

    assert '"confidence":<number 0..1>}' in prompt
    assert "Prefer the minimal contract" in prompt
    assert '"uncertainties":<optional' not in prompt


def test_decision_evaluation_passes_only_a_real_consensus():
    agreed = [
        {"success": True, "decision": {"label": "ACCEPT"}},
        {"success": True, "decision": {"label": "ACCEPT"}},
        {"success": True, "decision": {"label": "ACCEPT"}},
    ]
    report = consistency.evaluate_decisions(agreed)
    assert report["passed"] is True
    assert report["human_review"] is False
    assert report["agreement_score"] == 1.0
    assert report["coverage"] == 1.0

    split = [
        {"success": True, "decision": {"label": "ACCEPT"}},
        {"success": True, "decision": {"label": "REJECT"}},
        {"success": False},
    ]
    report = consistency.evaluate_decisions(split)
    assert report["passed"] is False
    assert report["human_review"] is True
    assert "provider_failure" in report["review_reasons"]
    assert "decision_disagreement" in report["review_reasons"]


def test_parallel_runner_overlaps_calls_but_preserves_order():
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


def test_parallel_runner_isolates_provider_exception():
    def broken():
        raise RuntimeError("provider down")

    outcomes = parallel.run_ordered([lambda: "ok", broken], max_workers=2)
    assert outcomes[0].value == "ok"
    assert outcomes[0].error is None
    assert outcomes[1].value is None
    assert str(outcomes[1].error) == "provider down"


def test_compare_consistency_gate_passes_with_shared_provenance(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)

    def fake_chat(provider, arguments):
        _, provenance = consistency.prepare_provider_call(arguments)
        return {
            "success": True,
            "text": _decision("ACCEPT"),
            "model": f"{provider}-test",
            "warnings": [],
            "consistency": provenance,
        }

    monkeypatch.setattr(operations, "_chat_raw", fake_chat)
    result = operations.dispatch_tool(
        "agent_hub_compare_models",
        {
            "prompt": "Is the fixture valid?",
            "providers": ["claude", "grok", "gemini"],
            "project_root": root,
            "consistency": {
                "enabled": True,
                "decision_labels": ["ACCEPT", "REJECT", "UNCERTAIN"],
            },
        },
    )
    report = result["data"]["consistency"]
    assert result["success"] is True
    assert report["decision"] == "ACCEPT"
    assert report["agreement_score"] == 1.0
    assert report["coverage"] == 1.0
    assert report["provenance_consistent"] is True
    assert report["policy_sha256"]
    assert report["request_sha256"]


def test_compare_consistency_gate_fails_closed_on_invalid_or_split_output(tmp_path, monkeypatch):
    root = _policy_root(tmp_path)
    answers = {
        "claude": _decision("ACCEPT"),
        "grok": _decision("REJECT"),
        "gemini": "not-json",
    }

    def fake_chat(provider, arguments):
        _, provenance = consistency.prepare_provider_call(arguments)
        return {
            "success": True,
            "text": answers[provider],
            "model": f"{provider}-test",
            "warnings": [],
            "consistency": provenance,
        }

    monkeypatch.setattr(operations, "_chat_raw", fake_chat)
    result = operations.dispatch_tool(
        "agent_hub_compare_models",
        {
            "prompt": "Decide",
            "project_root": root,
            "consistency": {"decision_labels": ["ACCEPT", "REJECT", "UNCERTAIN"]},
        },
    )
    report = result["data"]["consistency"]
    assert result["success"] is False
    assert result["error"] is not None
    assert report["human_review"] is True
    assert report["decision"] is None
    assert "invalid_contract" in report["review_reasons"]
    assert "decision_disagreement" in report["review_reasons"]
    assert "consistency_gate_human_review" in result["warnings"]


def test_raw_compare_stays_unscored(monkeypatch):
    monkeypatch.setattr(
        operations,
        "_chat_raw",
        lambda provider, _arguments: {
            "success": True,
            "text": provider,
            "model": provider,
            "warnings": [],
        },
    )
    result = operations.dispatch_tool(
        "agent_hub_compare_models", {"prompt": "open question", "execution": "sequential"}
    )
    assert result["success"] is True
    assert "consistency" not in result["data"]
    assert [item["provider"] for item in result["data"]["results"]] == [
        "claude",
        "grok",
        "gemini",
    ]


@pytest.mark.parametrize(
    ("failed_providers", "expected_success", "expected_status"),
    [
        (set(), True, "complete"),
        ({"grok"}, True, "partial"),
        ({"grok", "gemini"}, False, "insufficient"),
        ({"claude", "grok", "gemini"}, False, "failed"),
    ],
)
def test_raw_compare_requires_two_responses_and_reports_participants(
    monkeypatch, failed_providers, expected_success, expected_status
):
    def fake_chat(provider, _arguments):
        if provider in failed_providers:
            return {
                "success": False,
                "text": "",
                "model": f"{provider}-test",
                "error": "provider unavailable",
            }
        text = provider * 5000 if provider == "claude" else f"{provider} evidence"
        return {
            "success": True,
            "text": text,
            "model": f"{provider}-test",
            "warnings": [],
        }

    monkeypatch.setattr(operations, "_chat_raw", fake_chat)
    result = operations.dispatch_tool(
        "agent_hub_compare_models",
        {
            "prompt": "open question",
            "providers": ["claude", "grok", "gemini"],
            "execution": "sequential",
        },
    )

    data = result["data"]
    assert result["success"] is expected_success
    assert data["schema"] == "compare_result_v1"
    assert data["status"] == expected_status
    assert data["requested"] == 3
    assert data["succeeded"] == 3 - len(failed_providers)
    assert data["min_successes"] == 2
    assert data["call_usage"]["provider_calls"] == 3
    assert data["participants"] == data["results"]
    claude = data["participants"][0]
    if "claude" not in failed_providers:
        assert claude["original_chars"] > len(claude["text"])
        assert claude["text_truncated"] is True


def test_public_schema_exposes_parallel_and_gate_controls():
    spec = next(
        item for item in operations.tool_definitions() if item["name"] == "agent_hub_compare_models"
    )
    props = spec["inputSchema"]["properties"]
    assert props["execution"]["default"] == "parallel"
    assert props["max_concurrency"]["maximum"] == 4
    assert props["min_successes"]["default"] == 2
    assert props["consistency"]["required"] == ["decision_labels"]
    assert len(operations.tool_definitions()) == 30
