"""What a provider reports when a call fails, and what it must not report.

Three provider packages each had their own translator turning an exception into
a failure payload. openai_codex mapped the exception's code to a fixed sentence;
claude_codex and grok_codex put str(exc) straight into the response. An
exception string from an HTTP client carries whatever the client thought was
worth mentioning -- a request URL, a header fragment, a filesystem path.

agent_hub.v2 reads `error_type` off this payload to classify the failure
(provider_worker._raise_failed_payload), so the code has to stay stable. The
human-readable half never needed to come from the exception.
"""

from __future__ import annotations

import json

import pytest

from agent_hub.core.response import failure_payload
from claude_codex import mcp_server as claude_mcp
from grok_codex import mcp_server as grok_mcp
from openai_codex import mcp_server as openai_mcp


class _CodedError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def test_the_exception_message_never_reaches_the_payload():
    leaky = _CodedError(
        "POST https://api.example.com/v1?key=sk-real-secret failed for /Users/me/.ssh/id_rsa",
        "rate_limit",
    )

    payload = failure_payload(leaky, provider="xai", backend="xai-chat-completions")

    serialized = json.dumps(payload)
    assert "sk-real-secret" not in serialized
    assert "id_rsa" not in serialized
    assert "api.example.com" not in serialized


def test_the_code_survives_because_failure_classification_reads_it():
    payload = failure_payload(
        _CodedError("anything", "codex_timeout"),
        provider="gpt",
        backend="official-codex",
    )

    assert payload["error_type"] == "codex_timeout"
    assert payload["error"] == "codex_timeout"
    assert payload["success"] is False


def test_an_exception_with_no_code_falls_back_to_its_type():
    payload = failure_payload(RuntimeError("boom"), provider="anthropic", backend="x")

    assert payload["error_type"] == "RuntimeError"


def test_a_consent_failure_is_named_as_one_however_it_was_raised():
    # Consent is the one case where the message, not the code, identifies the
    # failure -- and the caller's remedy is completely different.
    payload = failure_payload(
        RuntimeError("Explicit consent required for this provider"),
        provider="gpt",
        backend="official-codex",
    )

    assert payload["error_type"] == "explicit_consent_required"


@pytest.mark.parametrize(
    ("server", "tool"),
    [
        (claude_mcp, "claude_codex_chat"),
        (grok_mcp, "grok_codex_chat"),
        (openai_mcp, "openai_codex_chat"),
    ],
)
def test_every_provider_redacts_a_failing_call_the_same_way(server, tool, monkeypatch):
    monkeypatch.setattr(
        server.chat,
        "run_chat",
        lambda _arguments: (_ for _ in ()).throw(RuntimeError("secret-token at /private/path")),
    )

    result = server.dispatch_tool(tool, {"prompt": "hello"})

    assert result["success"] is False
    assert result["error_type"] == "RuntimeError"
    serialized = json.dumps(result)
    assert "secret-token" not in serialized
    assert "/private/path" not in serialized
