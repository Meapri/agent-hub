"""What a chat response means, decided once instead of four times.

Each adapter used to answer this for itself and they disagreed. Three
independently marked a truncated answer as failed, which discarded the text
and -- carrying no error_type -- reached callers as
provider_unclassified_failure. Only one warned about an empty answer, so a model
that spent its whole budget reasoning and emitted nothing read as an ordinary
success. Both bugs were found in production, twice, because fixing one adapter
left the same dead end reachable through the others.
"""

from __future__ import annotations

import inspect

import pytest

from agent_hub.core.response import chat_outcome


def test_a_complete_answer_is_quiet():
    outcome = chat_outcome(text="the answer", finish_reason="stop")

    assert outcome["success"] is True
    assert outcome["warnings"] == []


@pytest.mark.parametrize("reason", ["max_tokens", "MAX_TOKENS", "length", "incomplete"])
def test_a_truncated_answer_is_kept_and_labelled(reason):
    outcome = chat_outcome(text="partial answer", finish_reason=reason)

    assert outcome["success"] is True
    assert f"incomplete_finish_reason:{reason.lower()}" in outcome["warnings"]


def test_an_empty_answer_says_so_rather_than_passing():
    outcome = chat_outcome(text="   ", finish_reason="stop")

    assert outcome["success"] is True
    assert "empty_model_text" in outcome["warnings"]


def test_a_stop_the_runtime_cannot_use_is_a_failure():
    # claude stops with tool_use to ask for a tool nothing here offers. That is
    # not truncation; there is no answer to keep.
    outcome = chat_outcome(text="", finish_reason="tool_use", unusable_finish_reasons={"tool_use"})

    assert outcome["success"] is False
    assert "incomplete_finish_reason:tool_use" in outcome["warnings"]


def test_an_unusable_reason_for_one_provider_is_ordinary_for_another():
    assert chat_outcome(text="hi", finish_reason="tool_use")["success"] is True


def test_caller_warnings_survive_and_do_not_duplicate():
    outcome = chat_outcome(
        text="",
        finish_reason="max_tokens",
        warnings=["capacity_fallback:a->b", "empty_model_text"],
    )

    assert outcome["warnings"].count("empty_model_text") == 1
    assert "capacity_fallback:a->b" in outcome["warnings"]


def test_a_missing_finish_reason_is_a_normal_stop():
    for value in (None, "", "   "):
        assert chat_outcome(text="hi", finish_reason=value)["finish_reason"] == "stop"


def test_no_adapter_decides_this_for_itself_any_more():
    """The property that keeps the bug from coming back in one provider only.

    Both defects were identical across adapters, so a per-adapter test would
    have passed while the others stayed broken.
    """

    from claude_codex import chat as claude_chat
    from google_antigravity_codex import chat as google_chat
    from grok_codex import chat as grok_chat
    from openai_codex import chat as openai_chat

    for module in (claude_chat, grok_chat, google_chat, openai_chat):
        source = inspect.getsource(module)
        assert "empty_model_text" not in source, module.__name__
        assert "incomplete_finish_reason" not in source, module.__name__
        assert "chat_outcome" in source, module.__name__
