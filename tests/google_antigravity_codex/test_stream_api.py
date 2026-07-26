from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


from google_antigravity_codex import antigravity_api, agy_auth


def test_stream_post_parses_sse_and_json_lines():
    lines = [
        b'data: {"response":{"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}}\n',
        b"\n",
        b'{"response":{"candidates":[{"content":{"parts":[{"text":"!"}]}}]}}\n',
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

    with (
        patch.object(antigravity_api.agy_auth, "valid_credentials", return_value=credentials),
        patch.object(antigravity_api, "_stream_post", side_effect=fake_stream),
    ):
        events = list(
            antigravity_api.generate_content_stream(
                model="gemini-3.5-flash",
                request={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            )
        )
    assert events[0]["response"]["candidates"][0]["content"]["parts"][0]["text"] == "S"
    assert events[-1]["_antigravity_diagnostics"]["streamed"] is True


def test_stream_refresh_keeps_public_model_after_project_change():
    stale = agy_auth.AgyCredentials(
        access_token="stale-token",
        refresh_token="refresh-secret",
        expires_at_ms=4102444800000,
        project_id="project-one",
    )
    fresh = agy_auth.AgyCredentials(
        access_token="fresh-token",
        refresh_token="refresh-secret",
        expires_at_ms=4102444800000,
        project_id="project-two",
    )
    current = {"credentials": stale}
    stream_calls: list[tuple[str, str, str]] = []

    def force_refresh():
        current["credentials"] = fresh
        return fresh

    def fake_catalog(path, body, access_token, *, timeout):
        del access_token, timeout
        assert path == "/v1internal:fetchAvailableModels"
        internal = "MODEL_PROJECT_ONE" if body["project"] == "project-one" else "MODEL_PROJECT_TWO"
        return {
            "models": {
                "gemini-3.6-flash-high": {
                    "model": internal,
                    "displayName": "Gemini 3.6 Flash (High)",
                }
            }
        }

    def fake_stream(path, body, access_token, *, timeout):
        del path, timeout
        stream_calls.append((access_token, body["project"], body["model"]))
        if access_token == "stale-token":
            raise antigravity_api.AntigravityApiError(
                "unauthorized",
                code="antigravity_unauthorized",
                status_code=401,
            )
        yield {"response": {"candidates": []}}

    with (
        patch.object(
            antigravity_api.agy_auth,
            "valid_credentials",
            side_effect=lambda **_kwargs: current["credentials"],
        ),
        patch.object(
            antigravity_api.agy_auth,
            "force_refresh_credentials",
            side_effect=force_refresh,
        ),
        patch.object(antigravity_api, "_post", side_effect=fake_catalog),
        patch.object(antigravity_api, "_stream_post", side_effect=fake_stream),
    ):
        events = list(
            antigravity_api.generate_content_stream(
                model="gemini-3.6-flash-high",
                request={"contents": [{"parts": [{"text": "hello"}]}]},
            )
        )

    assert stream_calls == [
        ("stale-token", "project-one", "gemini-3.6-flash-high"),
        ("fresh-token", "project-two", "gemini-3.6-flash-high"),
    ]
    diagnostics = events[-1]["_antigravity_diagnostics"]
    assert diagnostics["used_model"] == "gemini-3.6-flash-high"
    assert diagnostics["auth_refreshed"] is True
