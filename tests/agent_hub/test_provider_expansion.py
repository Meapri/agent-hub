from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agent_hub import capabilities, operations, provider_settings
from agent_hub.core import media
from claude_codex import chat as claude_chat
from claude_codex import search as claude_search
from grok_codex import chat as grok_chat
from grok_codex import image as grok_image
from grok_codex import search as grok_search


def _spec(name: str):
    return next(item for item in operations.tool_definitions() if item["name"] == name)


def test_public_schemas_expose_real_provider_capabilities():
    assert _spec("agent_hub_search")["inputSchema"]["properties"]["provider"]["enum"] == [
        "auto",
        "claude",
        "grok",
        "gemini",
    ]
    assert _spec("agent_hub_generate_image")["inputSchema"]["properties"]["provider"][
        "enum"
    ] == ["auto", "grok", "gemini"]
    assert "provider" not in _spec("agent_hub_release_snapshot")["inputSchema"]["properties"]
    assert capabilities.supports("claude", "vision")
    assert not capabilities.supports("claude", "image_generation")


def test_write_routes_common_prompt_to_claude(monkeypatch):
    seen = {}

    def fake_chat(provider, arguments):
        seen.update({"provider": provider, "arguments": arguments})
        return {"success": True, "text": "다듬은 글", "model": "claude-test", "warnings": []}

    monkeypatch.setattr(operations, "_chat_raw", fake_chat)
    result = operations.dispatch_tool(
        "agent_hub_write",
        {"provider": "claude", "task": "polish", "source_text": "초안"},
    )
    assert result["success"] is True
    assert result["provider"] == "claude"
    assert result["text"] == "다듬은 글"
    assert seen["provider"] == "claude"
    assert "Source text:\n초안" in seen["arguments"]["prompt"]


def test_compare_defaults_to_three_providers(monkeypatch):
    called = []

    def fake_chat(provider, arguments):
        called.append(provider)
        return {"success": True, "text": provider, "model": f"{provider}-test", "warnings": []}

    monkeypatch.setattr(operations, "_chat_raw", fake_chat)
    result = operations.dispatch_tool("agent_hub_compare_models", {"prompt": "compare"})
    assert set(called) == {"claude", "grok", "gemini"}
    assert [item["provider"] for item in result["data"]["results"]] == [
        "claude",
        "grok",
        "gemini",
    ]
    assert result["data"]["execution"] == "parallel"
    assert result["provider"] == "multiple"


def test_provider_settings_are_persistent_and_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_CONFIG_DIR", str(tmp_path))
    updated = operations.dispatch_tool(
        "agent_hub_update_settings",
        {"provider": "grok", "model": "grok-4.5", "api_mode": "responses"},
    )
    assert updated["success"] is True
    assert provider_settings.get("grok") == {
        "model": "grok-4.5",
        "api_mode": "responses",
    }
    loaded = operations.dispatch_tool("agent_hub_get_settings", {"provider": "grok"})
    assert loaded["data"]["providers"]["grok"]["overrides"]["api_mode"] == "responses"
    operations.dispatch_tool("agent_hub_reset_settings", {"provider": "grok"})
    assert provider_settings.get("grok") == {}


def test_local_image_normalization_is_bounded_by_workspace(tmp_path):
    image = tmp_path / "frame.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nframe")
    normalized = media.normalize_images([str(image)], workspace_root=str(tmp_path))
    assert normalized[0]["url"].startswith("data:image/png;base64,")
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nframe")
    try:
        with pytest.raises(ValueError, match="outside workspace_root"):
            media.normalize_images([str(outside)], workspace_root=str(tmp_path))
    finally:
        outside.unlink(missing_ok=True)


def test_claude_multimodal_conversion_merges_adjacent_user_messages():
    _, messages = claude_chat.to_anthropic_messages(
        [
            {"role": "user", "content": "context"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,aGVsbG8=",
                    },
                    {"type": "input_text", "text": "label this frame"},
                ],
            },
        ]
    )
    assert len(messages) == 1
    assert messages[0]["content"][1]["type"] == "image"
    assert messages[0]["content"][2]["text"] == "label this frame"


def test_claude_five_ignores_deprecated_temperature(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_chat.security, "require_consent", lambda: None)
    monkeypatch.setattr(
        claude_chat.auth,
        "resolve_auth",
        lambda: {"mode": "api_key", "source": "test"},
    )

    def fake_messages(body, **_kwargs):
        captured.update(body)
        return {
            "model": "claude-sonnet-5",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "ok"}],
        }

    monkeypatch.setattr(claude_chat.api, "messages_create", fake_messages)
    result = claude_chat.run_chat(
        {"prompt": "test", "model": "claude-sonnet-5", "temperature": 0.2}
    )
    assert "temperature" not in captured
    assert "temperature_ignored_by_model" in result["warnings"]


def test_claude_opus_48_ignores_deprecated_temperature(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_chat.security, "require_consent", lambda: None)
    monkeypatch.setattr(
        claude_chat.auth,
        "resolve_auth",
        lambda: {"mode": "api_key", "source": "test"},
    )

    def fake_messages(body, **_kwargs):
        captured.update(body)
        return {
            "model": "claude-opus-4-8",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "ok"}],
        }

    monkeypatch.setattr(claude_chat.api, "messages_create", fake_messages)
    result = claude_chat.run_chat(
        {"prompt": "test", "model": "claude-opus-4-8", "temperature": 0.2}
    )
    assert "temperature" not in captured
    assert "temperature_ignored_by_model" in result["warnings"]


def test_grok_images_force_responses_api(tmp_path, monkeypatch):
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\nframe")
    captured = {}
    monkeypatch.setattr(grok_chat.security, "require_consent", lambda: None)
    monkeypatch.setattr(
        grok_chat.auth,
        "resolve_auth",
        lambda: {"mode": "api_key", "source": "test"},
    )

    def fake_responses(body, **_kwargs):
        captured.update(body)
        return {"status": "completed", "model": "grok-test", "output_text": "mouse"}

    monkeypatch.setattr(grok_chat.api, "responses_create", fake_responses)
    result = grok_chat.run_chat(
        {
            "prompt": "label",
            "images": [str(frame)],
            "workspace_root": str(tmp_path),
            "model": "grok-test",
        }
    )
    assert result["text"] == "mouse"
    blocks = captured["input"][0]["content"]
    assert blocks[0]["type"] == "input_image"
    assert blocks[1] == {"type": "input_text", "text": "label"}


def test_claude_search_returns_structured_citations(monkeypatch):
    monkeypatch.setattr(claude_search.security, "require_consent", lambda: None)
    monkeypatch.setattr(
        claude_search.auth,
        "resolve_auth",
        lambda: {"mode": "api_key"},
    )
    monkeypatch.setattr(
        claude_search.api,
        "messages_create",
        lambda *_args, **_kwargs: {
            "model": "claude-test",
            "stop_reason": "end_turn",
            "content": [
                {
                    "type": "text",
                    "text": "answer",
                    "citations": [{"type": "web_search_result_location", "url": "https://a.test"}],
                }
            ],
        },
    )
    result = claude_search.run_search({"query": "q", "model": "claude-test"})
    assert result["sources"][0]["url"] == "https://a.test"
    assert result["warnings"] == []


def test_grok_search_uses_web_and_x_tools(monkeypatch):
    captured = {}
    monkeypatch.setattr(grok_search.security, "require_consent", lambda: None)
    monkeypatch.setattr(grok_search.auth, "resolve_auth", lambda: {"mode": "api_key"})

    def fake_response(body, **_kwargs):
        captured.update(body)
        return {
            "status": "completed",
            "model": "grok-test",
            "output_text": "answer",
            "citations": ["https://x.com/example/status/1"],
        }

    monkeypatch.setattr(grok_search.api, "responses_create", fake_response)
    result = grok_search.run_search({"query": "q", "source": "both", "model": "grok-test"})
    assert [tool["type"] for tool in captured["tools"]] == ["web_search", "x_search"]
    assert result["sources"][0]["url"].startswith("https://x.com/")


def test_grok_image_generation_caches_base64(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_CODEX_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(grok_image.security, "require_consent", lambda: None)
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode("ascii")
    monkeypatch.setattr(
        grok_image.api,
        "images_generate",
        lambda *_args, **_kwargs: {"data": [{"b64_json": encoded}]},
    )
    result = grok_image.generate_image(
        {"prompt": "mouse", "model": "grok-imagine-image", "response_format": "b64_json"}
    )
    assert Path(result["path"]).is_file()
    assert result["mime_type"] == "image/png"
