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
