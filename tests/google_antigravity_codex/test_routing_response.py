from __future__ import annotations

from google_antigravity_codex import response, routing


def test_route_model_prefers_grounded_search_when_required():
    result = routing.route_model({"task": "chat", "grounding": "required"})

    assert result["task"] == "grounded-search"
    assert result["tool"] == "google_grounded_search"
    assert result["recommended_model"] in {
        "gemini-3.5-flash-high",
        "gemini-3.5-flash",
    }
    assert result["required_provider"] == "agy-oauth"
    assert result["success"] is True


def test_standard_fields_deduplicates_warnings():
    result = response.standard_fields(warnings=["a", "a", "", "b"], model="m")

    assert result["success"] is True
    assert result["provider"] == "google-antigravity"
    assert result["backend"] == "local"
    assert result["model"] == "m"
    assert result["warnings"] == ["a", "b"]
