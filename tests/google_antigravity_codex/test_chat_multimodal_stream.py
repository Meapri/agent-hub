from __future__ import annotations

from unittest.mock import patch

from google_antigravity_codex import chat


def test_build_contents_keeps_inline_image_and_function_parts():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is in this image?"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
                    },
                },
            ],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "lookup",
            "content": "{\"result\":\"ok\"}",
        },
    ]

    contents, system = chat._build_contents(messages)

    assert system is None
    assert contents[0]["role"] == "user"
    assert any("inlineData" in p for p in contents[0]["parts"])
    assert any(p.get("text") == "what is in this image?" for p in contents[0]["parts"])
    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][0]["functionCall"]["name"] == "lookup"
    assert contents[1]["parts"][0]["functionCall"]["args"] == {"q": "x"}
    assert contents[2]["role"] == "user"
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "lookup"


def test_remote_image_url_is_not_fetched():
    contents, _ = chat._build_contents(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/secret.png"},
                    }
                ],
            }
        ]
    )
    text = contents[0]["parts"][0]["text"]
    assert "omitted" in text
    assert "https://example.com" in text


def test_openai_function_tools_map_to_function_declarations():
    request = chat.build_request(
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "search stuff",
                    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
                },
            }
        ],
    )
    assert "functionDeclarations" in request["tools"][0]
    assert request["tools"][0]["functionDeclarations"][0]["name"] == "search"


def test_merge_stream_chunks_assembles_text():
    chunks = [
        {"response": {"candidates": [{"content": {"parts": [{"text": "Hel"}]}}]}},
        {"response": {"candidates": [{"content": {"parts": [{"text": "lo"}]}, "finishReason": "STOP"}]}},
    ]
    merged = chat.merge_stream_chunks(chunks)
    extracted = chat.extract_response_text(merged)
    assert extracted["text"] == "Hello"


def test_run_chat_stream_emits_progress_and_falls_back():
    progress_events = []

    def progress(method, params):
        progress_events.append((method, params))

    def fake_stream(**kwargs):
        yield {"response": {"candidates": [{"content": {"parts": [{"text": "A"}]}}]}}
        yield {"response": {"candidates": [{"content": {"parts": [{"text": "B"}]}}]}}
        yield {
            "_antigravity_diagnostics": {
                "backend": "agy-oauth-code-assist",
                "streamed": True,
            }
        }

    with patch.object(chat.provider, "generate_content_stream", side_effect=fake_stream):
        result = chat.run_chat({"prompt": "hi", "stream": True}, progress=progress)

    assert result["text"] == "AB"
    assert result["streamed"] is True
    assert progress_events
    assert progress_events[0][0] == "notifications/message"
    assert progress_events[0][1]["data"]["delta"] == "A"


def test_run_chat_stream_falls_back_to_generate_content():
    def boom(**kwargs):
        raise RuntimeError("stream down")
        yield  # pragma: no cover

    def fake_generate(**kwargs):
        return {
            "response": {"candidates": [{"content": {"parts": [{"text": "fallback"}]}}]},
            "_antigravity_diagnostics": {"backend": "agy-oauth-code-assist"},
        }

    with patch.object(chat.provider, "generate_content_stream", side_effect=boom), patch.object(
        chat.provider, "generate_content", side_effect=fake_generate
    ):
        result = chat.run_chat({"prompt": "hi", "stream": True})

    assert result["text"] == "fallback"
    assert result["diagnostics"].get("stream_fallback") is True
