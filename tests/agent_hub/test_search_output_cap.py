"""Search asks for more output than the model allows.

Found by running a real plan after the per-call cap was taken off the public
schema. Every `capability="search"` step failed in about half a second with
`internal_error`; the provider had actually answered:

    HTTP 400: max_tokens: 131072 > 128000, which is the maximum allowed
    number of output tokens for claude-sonnet-5

Two separate faults, both of the shape this repo keeps producing. `chat` clamps
the request to the model's documented output cap and `search` did not, so the
same rule lived in one of the two places that needed it -- and while callers
were passing 4000 or 7000 the gap could not show. And the search leaf was the
one provider call not wrapped by `_call_leaf`, so the refusal arrived as a bare
RuntimeError and reached the step record as `internal_error`, naming neither
the provider nor the reason.
"""

from __future__ import annotations

import pytest

from agent_hub.v2 import provider_runtime
from claude_codex import chat as claude_chat
from claude_codex import search as claude_search


@pytest.fixture
def sent(monkeypatch):
    """Capture the request body instead of calling the provider."""

    captured: dict = {}

    def messages_create(body, timeout=None):
        captured.update(body)
        return {
            "model": body["model"],
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "answer"}],
        }

    monkeypatch.setattr(claude_search.api, "messages_create", messages_create)
    monkeypatch.setattr(claude_search.security, "require_consent", lambda: None)
    return captured


# --- the clamp search was missing -------------------------------------------


def test_search_never_asks_for_more_output_than_the_model_allows(sent):
    result = claude_search.run_search({"query": "q", "max_tokens": 131_072})

    limit = claude_chat.max_output_tokens_for_model(sent["model"])
    assert sent["max_tokens"] == limit
    assert sent["max_tokens"] < 131_072
    assert f"max_tokens_clamped_for_model:131072->{limit}" in result["warnings"]


def test_a_request_under_the_limit_is_left_alone(sent):
    result = claude_search.run_search({"query": "q", "max_tokens": 4_000})

    assert sent["max_tokens"] == 4_000
    assert not [item for item in result["warnings"] if "clamped" in item]


def test_the_policy_default_is_above_the_claude_limit():
    """This is why the bug surfaced when callers stopped setting their own cap:
    the project default flows straight through to the provider."""

    from agent_hub.v2.policy import DEFAULT_POLICY

    default = DEFAULT_POLICY["budgets"]["max_output_tokens"]
    assert default > claude_chat.max_output_tokens_for_model("claude-sonnet-5")


def test_search_and_chat_clamp_to_the_same_number(sent):
    """The rule is one rule. Two copies is how it went missing from one."""

    claude_search.run_search({"query": "q", "max_tokens": 999_999})
    searched = sent["max_tokens"]

    assert searched == claude_chat.max_output_tokens_for_model(sent["model"])


# --- and the wrapper that would have named the failure ----------------------


def test_a_failing_claude_search_arrives_named_not_as_an_internal_error(monkeypatch):
    def explode(_arguments):
        raise RuntimeError("HTTP 400: something the provider refused")

    monkeypatch.setattr(claude_search, "run_search", explode)
    monkeypatch.setattr(provider_runtime.capabilities, "require", lambda *_: None)

    result = provider_runtime.search("claude", {"query": "q"})

    assert result["success"] is False
    assert result["error"]["type"] == "RuntimeError"
    assert result["provider"] == "claude"
    # The provider's own words must not reach the caller verbatim.
    assert "HTTP 400" not in result["text"]


def test_a_failing_grok_search_is_wrapped_the_same_way(monkeypatch):
    from grok_codex import search as grok_search

    def explode(_arguments):
        raise RuntimeError("boom")

    monkeypatch.setattr(grok_search, "run_search", explode)
    monkeypatch.setattr(provider_runtime.capabilities, "require", lambda *_: None)

    result = provider_runtime.search("grok", {"query": "q"})

    assert result["success"] is False
    assert result["provider"] == "grok"


def test_every_search_provider_goes_through_the_wrapper():
    """gemini was already wrapped; claude and grok were the two that were not."""

    import ast
    import inspect

    tree = ast.parse(inspect.getsource(provider_runtime.search).lstrip())
    direct = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_search"
        and not isinstance(node.func.value, ast.Attribute)
    ]

    assert direct == [], [ast.unparse(node) for node in direct]
