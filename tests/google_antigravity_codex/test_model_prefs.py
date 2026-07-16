from __future__ import annotations

from google_antigravity_codex import chat, model_prefs, routing


def test_set_and_resolve_default_and_task(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_DEFAULT_MODEL", raising=False)

    model_prefs.set_model(model="flash", validate=False)
    assert model_prefs.resolve_model(fallback="x") == "gemini-3.5-flash-high"

    model_prefs.set_model(model="opus", task="code", validate=False)
    assert model_prefs.resolve_model(task="code", fallback="x") == "claude-opus-4-6-thinking"
    # chat still uses default
    assert model_prefs.resolve_model(task="chat", fallback="x") == "gemini-3.5-flash-high"
    # explicit wins
    assert model_prefs.resolve_model(explicit="sonnet", task="code") == "claude-sonnet-4-6-thinking"


def test_route_model_uses_saved_pref(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    model_prefs.set_model(model="claude-opus-4-6-thinking", task="code", validate=False)
    result = routing.route_model({"task": "code"})
    assert result["recommended_model"] == "claude-opus-4-6-thinking"
    assert result["selection_source"] == "user-pref"


def test_chat_uses_saved_default_when_model_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    model_prefs.set_model(model="gemini-3.1-pro-high", validate=False)
    seen = {}

    def fake_generate(**kwargs):
        seen["model"] = kwargs.get("model")
        return {"response": {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}}

    from unittest.mock import patch

    with patch.object(chat.provider, "generate_content", side_effect=fake_generate):
        result = chat.run_chat({"prompt": "hi"})
    assert seen["model"] == "gemini-3.1-pro-high"
    assert result["model"] == "gemini-3.1-pro-high"


def test_clear_prefs(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    model_prefs.set_model(model="flash", validate=False)
    model_prefs.set_model(model="opus", task="code", validate=False)
    model_prefs.clear_prefs(task="code")
    assert model_prefs.load_prefs()["task_models"] == {}
    assert model_prefs.load_prefs()["default_model"] == "gemini-3.5-flash-high"
    model_prefs.clear_prefs(all_prefs=True)
    assert model_prefs.load_prefs()["default_model"] == ""


def test_mcp_model_tools_registered():
    from google_antigravity_codex import mcp_server

    names = {t["name"] for t in mcp_server.tool_definitions()}
    assert "google_antigravity_set_model" in names
    assert "google_antigravity_get_model_prefs" in names
    assert "google_antigravity_clear_model_prefs" in names
