from __future__ import annotations

import base64
import os
import socket
from unittest.mock import patch

import pytest

from google_antigravity_codex import chat, grounding, image, provider


PNG_HEX = (
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6300010000000500010d0a2db40000000049454e44"
    "ae426082"
)


def b64_png() -> str:
    return base64.b64encode(bytes.fromhex(PNG_HEX)).decode()


def test_chat_request_adds_google_search_grounding():
    request = chat.build_request(
        messages=[{"role": "user", "content": "latest news with sources"}],
        grounding="always",
    )

    assert {"google_search": {}} in request["tools"]
    assert "toolConfig" not in request
    assert "Google Search grounding" in request["systemInstruction"]["parts"][0]["text"]


def test_chat_floors_small_generation_limits_for_reasoning_models():
    request = chat.build_request(
        messages=[{"role": "user", "content": "short answer"}],
        model="gemini-3.5-flash",
        max_tokens=16,
    )

    assert request["generationConfig"]["maxOutputTokens"] == 256


def test_chat_floors_small_generation_limits_for_gemini_pro_agent():
    request = chat.build_request(
        messages=[{"role": "user", "content": "short answer"}],
        model="gemini-pro-agent",
        max_tokens=16,
    )

    assert request["generationConfig"]["maxOutputTokens"] == 256


def test_chat_exposes_official_thinking_level():
    request = chat.build_request(
        messages=[{"role": "user", "content": "reason carefully"}],
        model="gemini-3.5-flash",
        thinking_level="high",
    )

    assert request["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}


def test_chat_uses_grounding_env_default():
    seen = {}

    def fake_generate_content(**kwargs):
        seen.update(kwargs)
        return {"response": {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}}

    with patch.dict(os.environ, {"GOOGLE_ANTIGRAVITY_GROUNDING": "always"}), patch.object(
        chat.provider, "generate_content", fake_generate_content
    ):
        result = chat.run_chat({"prompt": "latest"})

    assert result["text"] == "ok"
    assert result["success"] is True
    assert result["backend"] == "agy-session"
    assert {"google_search": {}} in seen["request"]["tools"]


def test_chat_extracts_text_and_usage():
    payload = {
        "response": {
            "candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 3},
        }
    }

    result = chat.extract_response_text(payload)

    assert result["text"] == "hello"
    assert result["usage"]["total_tokens"] == 3


def test_grounding_extracts_sources_and_claims():
    text = "A device has 16 GB memory. Sources: **https://blog.google/example**"

    urls = grounding.extract_urls(text)
    claims = grounding.extract_numeric_claims(text)
    source_type = grounding.classify_source(urls[0])

    assert urls == ["https://blog.google/example"]
    assert claims == ["16 GB"]
    assert source_type == "official"


def test_grounding_official_domains_can_be_extended():
    with patch.dict(os.environ, {"GOOGLE_ANTIGRAVITY_OFFICIAL_DOMAINS": "example.com"}):
        assert grounding.classify_source("https://docs.example.com/page") == "official"


def test_grounded_search_rejects_agy_cli_provider(monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_PROVIDER", "agy-cli")
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ENABLE_AGY_SESSION", "1")

    with pytest.raises(provider.ProviderError) as raised:
        grounding.run_grounded_search({"query": "latest news"})

    assert raised.value.code == "agy_cli_removed"


def test_grounded_search_exposes_text_alias():
    with patch.object(
        grounding.chat,
        "run_chat",
        lambda args: {"text": "The official site is https://openai.com.", "usage": {"total_tokens": 3}},
    ), patch.object(grounding.provider, "require_capability"):
        result = grounding.run_grounded_search({"query": "official site", "resolve_sources": False})

    assert result["answer"] == "The official site is https://openai.com."
    assert result["text"] == result["answer"]
    assert result["sources"][0]["resolved_url"] == "https://openai.com"
    assert result["source_summary"]
    assert result["evidence"][0]["source_urls"]
    assert result["success"] is True


def test_grounded_search_recovers_direct_sources_from_failed_redirect():
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/bad"
    calls = []

    def fake_run_chat(args):
        calls.append(args)
        if len(calls) == 1:
            return {"text": f"Answer with source {redirect}", "usage": {"total_tokens": 3}}
        return {"text": "https://nvidianews.nvidia.com/news/nvidia-microsoft-windows-pcs-agents-rtx-spark"}

    with patch.object(grounding.chat, "run_chat", fake_run_chat), patch.object(
        grounding, "resolve_url_with_curl", lambda url, timeout_sec=8: ""
    ), patch.object(grounding.urllib.request, "urlopen", side_effect=RuntimeError("404")):
        with patch.object(grounding.provider, "require_capability"):
            result = grounding.run_grounded_search({"query": "rtx spark", "max_sources": 3})

    assert len(calls) == 2
    assert result["quality_signals"]["unresolved_redirect_count"] == 0
    assert result["quality_signals"]["needs_manual_source_check"] is False
    assert result["sources"][0]["resolved_url"] == "https://nvidianews.nvidia.com/news/nvidia-microsoft-windows-pcs-agents-rtx-spark"
    assert result["sources"][0]["recovered_by"] == "direct_source_retry"


def test_image_result_extraction():
    payload = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": b64_png()}}]}}]}

    data, kind, extension = image.extract_image_result(payload)

    assert data == b64_png()
    assert kind == "b64"
    assert extension == "png"


def test_image_model_normalization():
    assert image.normalize_model("google/gemini-3-1-flash-image") == "gemini-3.1-flash-image"
    assert image.normalize_model("nano-banana") == "gemini-3.1-flash-image"
    assert image.resolve_image_size("2048") == "2K"


def test_generate_image_exposes_path_alias_and_metadata(tmp_path):
    payload = {
        "generatedImages": [
            {"image": {"mimeType": "image/png", "imageBytes": b64_png()}}
        ]
    }

    with patch.object(
        image, "available_model_catalog", lambda: {"gemini-3.1-flash-image": {}}
    ), patch.object(image.provider, "generate_image", lambda **kwargs: payload), patch.object(
        image.paths, "images_dir", lambda: tmp_path
    ), patch.object(image.provider, "require_capability"):
        result = image.generate_image({"prompt": "draw", "aspect_ratio": "square", "image_size": "512"})

    assert result["success"] is True
    assert result["image"] == result["path"]
    assert result["text"].startswith("Generated image: ")
    assert result["mime_type"] == "image/png"
    assert result["size_bytes"] > 0
    assert result["backend"] == "agy-oauth-code-assist"


def test_save_b64_image_rejects_oversized_payload(monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_MAX_IMAGE_BYTES", "8")
    with pytest.raises(ValueError, match="size limit"):
        image.save_b64_image(b64_png(), prefix="test", extension="png")


def test_save_url_image_blocks_private_network(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ValueError, match="private"):
        image.save_url_image("https://example.test/image.png", prefix="test")
