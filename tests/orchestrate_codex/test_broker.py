"""End-to-end broker tests against a mock leaf MCP server (no real credentials)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from orchestrate_codex import broker, leaf_client, leaves, results, runner

_MOCK = str(Path(__file__).resolve().parent / "mock_leaf.py")


def _leaves(tool_key: str, *, fail: bool = False, tool_name: str = "") -> dict:
    env = {"MOCK_LEAF_TOOL": tool_name or tool_key}
    if fail:
        env["MOCK_LEAF_FAIL"] = "1"
    return {tool_key: {"command": sys.executable, "args": [_MOCK], "env": env}}


def test_leaf_client_roundtrip():
    reg = _leaves("mock_chat")
    spec = reg["mock_chat"]
    with leaf_client.LeafClient("mock", spec["command"], spec["args"], env=spec["env"]) as c:
        assert "mock_chat" in c.list_tools()
        ok, text = c.call_tool("mock_chat", {"prompt": "hi"})
        assert ok is True
        assert "MOCK[mock_chat]" in text and "hi" in text
        structured = c.call_tool_result("mock_chat", {"prompt": "again"})
        assert structured.provider == "mock"
        assert structured.model == "mock-model"
        assert structured.usage == {"prompt_tokens": 3, "completion_tokens": 2}
        assert structured.finish_reason == "stop"
        assert structured.warnings == ("mock-warning",)
        assert structured.artifacts == ({"name": "mock.txt", "sha256": "a" * 64},)
        assert structured.provenance == {
            "policy_mode": "auto",
            "request_sha256": "b" * 64,
        }


def test_broker_runs_direct_chat_end_to_end(tmp_path):
    # Map the chat leaf to the mock; bind direct_chat's chat tool to it.
    reg = {"claude_codex_chat": {"command": sys.executable, "args": [_MOCK],
                                 "env": {"MOCK_LEAF_TOOL": "claude_codex_chat"}}}
    out = broker.run_auto(
        "direct_chat", args={"prompt": "hello"}, project_root=str(tmp_path), leaves=reg
    )
    assert out["ok"] is True
    assert out["status"] == "completed"
    assert out["leaf_calls"] == 1
    assert "MOCK[claude_codex_chat]" in out["artifact"]
    persisted = out["state"]["steps"][0]
    assert persisted["result"]["schema"] == results.RESULT_SCHEMA
    assert persisted["result"]["provider"] == "mock"
    assert persisted["result"]["model"] == "mock-model"
    assert persisted["result"]["text_ref"] == "result_text"
    assert "text" not in persisted["result"]
    assert "diagnostics" not in json.dumps(persisted)
    assert "must-not-persist" not in json.dumps(persisted)
    assert out["trace"][0]["usage"]["prompt_tokens"] == 3
    runner._RUNS.clear()
    restored = runner.get_run(out["run_id"])
    assert restored["steps"][0]["result"]["usage"]["completion_tokens"] == 2


def test_broker_runs_gpt_as_a_fixed_primary_binding(tmp_path):
    reg = {
        "openai_codex_chat": {
            "command": sys.executable,
            "args": [_MOCK],
            "env": {"MOCK_LEAF_TOOL": "openai_codex_chat"},
        }
    }
    out = broker.run_auto(
        "direct_chat",
        args={"prompt": "hello"},
        bindings={"chat": "openai_codex_chat"},
        project_root=str(tmp_path),
        leaves=reg,
    )
    assert out["ok"] is True
    assert out["trace"][0]["tool"] == "openai_codex_chat"
    assert "MOCK[openai_codex_chat]" in out["artifact"]
    assert "openai_codex_chat" in runner.FALLBACK_CHAINS["write"]


def test_broker_rotates_on_leaf_failure(tmp_path):
    # Primary write leaf fails; fallback chat leaf succeeds -> run still completes.
    reg = {
        "google_antigravity_write": {"command": sys.executable, "args": [_MOCK],
                                     "env": {"MOCK_LEAF_TOOL": "google_antigravity_write",
                                             "MOCK_LEAF_FAIL": "1", "MOCK_LEAF_MSG": "429 quota"}},
        "claude_codex_chat": {"command": sys.executable, "args": [_MOCK],
                              "env": {"MOCK_LEAF_TOOL": "claude_codex_chat"}},
    }
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n', encoding="utf-8")
    out = broker.run_auto("durable_readme", args={"prompt": "readme"}, project_root=str(tmp_path), leaves=reg)
    assert out["ok"] is True
    tools_called = [t["tool"] for t in out["trace"]]
    assert "google_antigravity_write" in tools_called  # tried primary
    assert "claude_codex_chat" in tools_called          # rotated to fallback
    assert "MOCK[claude_codex_chat]" in out["artifact"]


def test_broker_treats_soft_error_as_failure(tmp_path):
    # Grok hands back an HTTP 503 as "successful" text; broker must rotate to the fallback
    # instead of using the error string as the answer.
    reg = {
        "grok_codex_chat": {"command": sys.executable, "args": [_MOCK],
                            "env": {"MOCK_LEAF_TOOL": "grok_codex_chat", "MOCK_LEAF_SOFT": "1",
                                    "MOCK_LEAF_MSG": "HTTP 503: upstream connect error, connection refused"}},
        "claude_codex_chat": {"command": sys.executable, "args": [_MOCK],
                              "env": {"MOCK_LEAF_TOOL": "claude_codex_chat"}},
    }
    out = broker.run_auto("direct_chat", args={"prompt": "hi"}, project_root=str(tmp_path),
                          bindings={"chat": "grok_codex_chat"}, leaves=reg)
    tools = [t["tool"] for t in out["trace"]]
    assert "grok_codex_chat" in tools  # tried grok
    assert any(t.get("soft_error") for t in out["trace"])  # detected the 503
    assert out["ok"] is True and "MOCK[claude_codex_chat]" in out["artifact"]  # rotated + succeeded
    attempts = out["state"]["steps"][0]["attempt_results"]
    assert [item["success"] for item in attempts] == [False, True]


def test_broker_accepts_legacy_tuple_only_client(tmp_path):
    class LegacyClient:
        def call_tool(self, tool, arguments, *, timeout=None):
            return True, f"legacy:{tool}:{arguments['prompt']}"

    out = broker.run_auto(
        "direct_chat",
        args={"prompt": "hello"},
        project_root=str(tmp_path),
        client_resolver=lambda _tool: LegacyClient(),
    )

    assert out["ok"] is True
    assert out["artifact"].startswith("legacy:claude_codex_chat:")
    assert out["state"]["steps"][0]["result"]["schema"] == results.RESULT_SCHEMA


def test_broker_claims_action_before_provider_dispatch(tmp_path):
    observed = []

    class InspectingClient:
        def call_tool_result(self, tool, arguments, *, timeout=None):
            run_id = runner.store.list_run_ids()[0]
            persisted = runner.store.load_strict(run_id)
            observed.append(
                {
                    "tool": tool,
                    "arguments": arguments,
                    "lease": persisted.get("_lease"),
                }
            )
            return results.OperationResult.from_result(
                {
                    "success": True,
                    "text": "claimed answer",
                    "provider": "mock",
                    "model": "mock-model",
                }
            )

    out = broker.run_auto(
        "direct_chat",
        args={"prompt": "hello"},
        project_root=str(tmp_path),
        client_resolver=lambda _tool: InspectingClient(),
    )

    assert out["ok"] is True
    assert len(observed) == 1
    assert observed[0]["lease"]["base_revision"] == 0
    assert out["trace"][0]["action_id"]
    assert "_lease" not in runner.store.load_strict(out["run_id"])


def test_structured_result_redacts_secrets_and_bounds_metadata():
    raw = {
        "content": [{"type": "text", "text": "Bearer secret-token"}],
        "structuredContent": {
            "success": False,
            "error_type": "auth",
            "status_code": 401,
            "text": "sk-super-secret-value",
            "usage": {"prompt_tokens": 1, "label": "drop"},
            "warnings": ["access_token=\"secret\""],
            "artifacts": [{"name": "x", "uri": "data:text/plain;base64,c2VjcmV0"}],
            "diagnostics": {"refresh_token": "secret"},
        },
        "isError": True,
    }

    normalized = leaf_client._interpret_result(raw).as_dict()

    assert "secret-token" not in json.dumps(normalized)
    assert "super-secret" not in json.dumps(normalized)
    assert normalized["error"]["type"] == "auth"
    assert normalized["error"]["code"] == 401
    assert normalized["usage"] == {"prompt_tokens": 1}
    assert normalized["artifacts"] == [{"name": "x"}]
    assert "diagnostics" not in normalized


def test_broker_no_leaves_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATE_CODEX_LEAVES", str(tmp_path / "nope.json"))
    out = broker.run_auto("direct_chat", args={"prompt": "hi"}, project_root=str(tmp_path))
    assert out["ok"] is False
    assert "no leaf servers configured" in out["error"]


def test_broker_bad_request_does_not_rotate(tmp_path):
    # A 400/bad_request from the only leaf must fail fast, not exhaust fallbacks.
    reg = {"claude_codex_chat": {"command": sys.executable, "args": [_MOCK],
                                 "env": {"MOCK_LEAF_TOOL": "claude_codex_chat",
                                         "MOCK_LEAF_FAIL": "1", "MOCK_LEAF_MSG": "400 invalid argument"}}}
    out = broker.run_auto("direct_chat", args={"prompt": "hi"}, project_root=str(tmp_path), leaves=reg)
    assert out["ok"] is False
    assert out["leaf_calls"] == 1  # did not retry other providers


def test_leaves_config_loading(tmp_path, monkeypatch):
    cfg = tmp_path / "leaves.json"
    cfg.write_text(json.dumps({"google_antigravity": {"command": "python3", "args": ["x.py"]}}), encoding="utf-8")
    monkeypatch.setenv("ORCHESTRATE_CODEX_LEAVES", str(cfg))
    assert leaves.configured() is True
    spec = leaves.resolve_launch("google_antigravity_write")  # provider-prefix match
    assert spec and spec["command"] == "python3"
    assert leaves.resolve_launch("unknown_tool") is None
