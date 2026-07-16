"""End-to-end broker tests against a mock leaf MCP server (no real credentials)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from orchestrate_codex import broker, leaf_client, leaves

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
