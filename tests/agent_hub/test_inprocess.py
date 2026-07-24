"""In-process broker transport: consent non-bypass + routing invariants.

The autonomous broker used to spawn each leaf as a subprocess. In-process it
calls sibling adapters directly; the load-bearing invariant is that consent is
still enforced inside each adapter — the orchestrator gets no privileged bypass.
"""

from __future__ import annotations

import grok_codex.security as gsec

from agent_hub.core.inprocess import InProcessLeafClient, make_resolver
from agent_hub.providers.grok import grok_provider


def test_inprocess_call_preserves_consent_no_bypass(monkeypatch):
    monkeypatch.setattr(gsec._gate, "user_consent_enabled", lambda: False)
    client = InProcessLeafClient(grok_provider)
    ok, text = client.call_tool("grok_codex_chat", {"messages": [{"role": "user", "content": "hi"}]})
    assert ok is False
    assert "consent" in text.lower()


def test_inprocess_resolver_routes_leaves_only():
    resolve = make_resolver()
    assert resolve("grok_codex_chat") is not None      # leaf -> in-process client
    assert resolve("openai_codex_chat") is not None
    assert resolve("orchestrate_run") is None          # broker never recurses into orchestrate
    assert resolve("does_not_exist") is None


def test_inprocess_ok_result_interpreted():
    ok, text = InProcessLeafClient(grok_provider).call_tool("grok_codex_consent_status", {})
    assert ok is True
    assert text


def test_inprocess_structured_result_is_preserved():
    class Owner:
        def call(self, _tool, _arguments):
            return {
                "content": [{"type": "text", "text": "answer"}],
                "structuredContent": {
                    "success": True,
                    "text": "answer",
                    "provider": "gemini",
                    "model": "gemini-test",
                    "usage": {"total_tokens": 7},
                    "warnings": ["notice"],
                    "diagnostics": {"session_id": "drop-me"},
                },
                "isError": False,
            }

    result = InProcessLeafClient(Owner()).call_tool_result("chat", {})

    assert result.success is True
    assert result.provider == "gemini"
    assert result.model == "gemini-test"
    assert result.usage == {"total_tokens": 7}
    assert result.warnings == ("notice",)
    assert "drop-me" not in str(result.as_dict())
