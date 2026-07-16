from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


from google_antigravity_codex import antigravity_api, agy_auth


def test_stream_post_parses_sse_and_json_lines():
    lines = [
        b"data: {\"response\":{\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"hi\"}]}}]}}\n",
        b"\n",
        b"{\"response\":{\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"!\"}]}}]}}\n",
        b"data: [DONE]\n",
    ]

    class FakeResp:
        def __init__(self):
            self._i = 0

        def readline(self):
            if self._i >= len(lines):
                return b""
            line = lines[self._i]
            self._i += 1
            return line

        def close(self):
            return None

    opener = MagicMock()
    opener.open.return_value = FakeResp()
    with patch.object(antigravity_api.urllib.request, "build_opener", return_value=opener):
        chunks = list(
            antigravity_api._stream_post(
                "/v1internal:streamGenerateContent",
                {"model": "x"},
                "token",
                timeout=5.0,
            )
        )
    assert len(chunks) == 2
    assert "hi" in json.dumps(chunks[0])


def test_generate_content_stream_yields_chunks_and_diagnostics():
    credentials = agy_auth.AgyCredentials(
        access_token="access",
        refresh_token="refresh",
        expires_at_ms=4102444800000,
        project_id="proj",
    )

    def fake_stream(path, body, access_token, *, timeout):
        assert "streamGenerateContent" in path
        yield {"response": {"candidates": [{"content": {"parts": [{"text": "S"}]}}]}}

    with patch.object(antigravity_api.agy_auth, "valid_credentials", return_value=credentials), patch.object(
        antigravity_api, "_stream_post", side_effect=fake_stream
    ):
        events = list(
            antigravity_api.generate_content_stream(
                model="gemini-3.5-flash",
                request={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            )
        )
    assert events[0]["response"]["candidates"][0]["content"]["parts"][0]["text"] == "S"
    assert events[-1]["_antigravity_diagnostics"]["streamed"] is True
