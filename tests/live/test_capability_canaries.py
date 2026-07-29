"""One real call per provider per capability.

Each of these would have caught a breakage that shipped:

  vision            payload conversion was suspected and fine; the answer came
                    back marked failed
  image             the runtime handed a chat model to image generation
  chat truncation   three adapters called a truncated answer a failure and
                    discarded the text
  connection test   the GUI never sent project_root, so every provider failed
"""

from __future__ import annotations

import base64

import pytest

from agent_hub.v2 import provider_runtime
from agent_hub.v2.policy import DEFAULT_POLICY

pytestmark = pytest.mark.live

CHAT_PROVIDERS = ["claude", "grok", "gemini", "gpt"]
VISION_PROVIDERS = ["claude", "grok", "gemini", "gpt"]
SEARCH_PROVIDERS = ["claude", "grok", "gemini"]
IMAGE_PROVIDERS = ["grok", "gemini"]


def _usable_text(result) -> str:
    assert result.get("success") is True, result.get("error")
    text = str(result.get("text") or "")
    assert text.strip(), "a provider answered with nothing"
    return text


@pytest.mark.parametrize("provider", CHAT_PROVIDERS)
def test_chat_answers(provider, require_provider):
    require_provider(provider)

    result = provider_runtime.chat(
        provider,
        {"prompt": "Reply with the single word: ok", "max_tokens": 2048, "timeout_sec": 120},
    )

    _usable_text(result)


@pytest.mark.parametrize("provider", VISION_PROVIDERS)
def test_vision_reads_an_image(provider, require_provider, canary_png_bytes):
    """The capability the user reported as broken.

    Asserts only that a description came back. Which words a model picks for a
    checkerboard is not this repository's contract.
    """

    require_provider(provider)
    data_url = "data:image/png;base64," + base64.b64encode(canary_png_bytes).decode()

    result = provider_runtime.chat(
        provider,
        {
            "prompt": "Describe this image in one sentence.",
            "images": [data_url],
            "max_tokens": 2048,
            "timeout_sec": 180,
        },
    )

    _usable_text(result)


@pytest.mark.parametrize("provider", SEARCH_PROVIDERS)
def test_search_answers(provider, require_provider):
    """Sends the budget a real run sends, not a small convenient one.

    This canary used to pass max_tokens=2048 and so never exercised the value
    the runtime actually uses. With the project default of 131072, claude
    refused the whole request -- 131072 > 128000 for the model -- and every
    search step in every plan failed. A canary that picks its own comfortable
    numbers is testing a path production does not take.
    """

    require_provider(provider)

    result = provider_runtime.search(
        provider,
        {
            "query": "What is the capital of France?",
            "max_tokens": DEFAULT_POLICY["budgets"]["max_output_tokens"],
            "timeout_sec": 180,
        },
    )

    _usable_text(result)


@pytest.mark.parametrize("provider", IMAGE_PROVIDERS)
def test_image_generation_returns_a_file_the_worker_can_collect(provider, require_provider):
    """Pins the model-resolution bug.

    The runtime used to pass the provider's default *chat* model here, which
    gemini rejected as an unsupported image model. Leaving the model unset lets
    the adapter choose its own.
    """

    require_provider(provider)

    result = provider_runtime.generate_image(
        provider,
        {"prompt": "A plain solid red square on a white background.", "timeout_sec": 240},
    )

    assert result.get("success") is True, result.get("error")
    path = (result.get("data") or {}).get("path")
    assert path, "an image provider reported no output file"

    import pathlib

    written = pathlib.Path(str(path))
    assert written.is_file()
    assert written.stat().st_size > 0
    # The worker deletes this after collecting the bytes; here nothing did.
    written.unlink(missing_ok=True)


@pytest.mark.parametrize("provider", ["claude", "grok", "gemini"])
def test_a_truncated_answer_comes_back_rather_than_failing(provider, require_provider):
    """The defect that made vision look broken.

    A tiny output budget is the cheapest way to force finish_reason=max_tokens.
    Adapters used to report that as success=False with no error_type, which the
    runtime could only call provider_unclassified_failure -- discarding text the
    model had already produced.
    """

    require_provider(provider)

    result = provider_runtime.chat(
        provider,
        {"prompt": "Count slowly from one to one hundred.", "max_tokens": 16, "timeout_sec": 120},
    )

    assert result.get("success") is True, result.get("error")
    warnings = [str(item) for item in result.get("warnings") or []]
    assert any("incomplete_finish_reason" in item for item in warnings), warnings

    # An empty answer here is the provider's doing, not ours: claude spends its
    # whole budget on reasoning and emits no output token at all. What must not
    # happen is silence -- either there is text, or a warning says there is not.
    if str(result.get("text") or "").strip():
        return
    assert "empty_model_text" in warnings, warnings
