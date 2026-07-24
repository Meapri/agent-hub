from __future__ import annotations

import threading

import pytest

from google_antigravity_codex import chat, model_prefs, profiles, routing


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


def test_saved_chat_model_precedes_active_profile_model(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    model_prefs.set_model(
        model="gemini-3.5-flash-high",
        task="chat",
        validate=False,
    )
    profiles.use_profile_tool(
        {
            "name": "coding",
            "apply_model_pref": False,
            "apply_provider": False,
        }
    )
    seen = {}

    def fake_generate(**kwargs):
        seen["model"] = kwargs.get("model")
        return {"response": {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}}

    from unittest.mock import patch

    with patch.object(chat.provider, "generate_content", side_effect=fake_generate):
        result = chat.run_chat({"prompt": "hi"})

    assert seen["model"] == "gemini-3.5-flash-high"
    assert result["model"] == "gemini-3.5-flash-high"


def test_pair_profile_does_not_send_gemini_thinking_level_to_claude(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    profiles.use_profile_tool(
        {
            "name": "pair",
            "apply_model_pref": False,
            "apply_provider": False,
        }
    )
    seen = {}

    def fake_generate(**kwargs):
        seen["model"] = kwargs.get("model")
        return {"response": {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}}

    from unittest.mock import patch

    with patch.object(chat.provider, "generate_content", side_effect=fake_generate):
        result = chat.run_chat({"prompt": "hi"})

    assert seen["model"] == "claude-opus-4-6-thinking"
    assert result["model"] == "claude-opus-4-6-thinking"


def test_clear_prefs(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    model_prefs.set_model(model="flash", validate=False)
    model_prefs.set_model(model="opus", task="code", validate=False)
    model_prefs.clear_prefs(task="code")
    assert model_prefs.load_prefs()["task_models"] == {}
    assert model_prefs.load_prefs()["default_model"] == "gemini-3.5-flash-high"
    model_prefs.clear_prefs(all_prefs=True)
    assert model_prefs.load_prefs()["default_model"] == ""


def test_clear_default_scopes_removes_chat_and_global_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    model_prefs.set_model(model="gemini-3.1-pro-high", validate=False)
    model_prefs.set_model(model="gemini-3.5-flash-high", task="chat", validate=False)
    model_prefs.set_model(model="claude-opus-4-6-thinking", task="code", validate=False)

    model_prefs.clear_prefs(default_scopes=True)

    prefs = model_prefs.load_prefs()
    assert prefs["default_model"] == ""
    assert prefs["task_models"] == {"code": "claude-opus-4-6-thinking"}


def test_validation_rejects_unknown_and_image_models_for_text_defaults(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))

    with pytest.raises(model_prefs.ModelPrefsError) as unknown:
        model_prefs.set_model(model="made-up-model")
    with pytest.raises(model_prefs.ModelPrefsError) as image:
        model_prefs.set_model(model="nano-banana")

    assert unknown.value.code == "model_not_available"
    assert image.value.code == "model_not_available"
    assert model_prefs.load_prefs()["default_model"] == ""


def test_mutation_does_not_overwrite_malformed_preferences(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    path = model_prefs.prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(model_prefs.ModelPrefsError) as error:
        model_prefs.set_model(model="flash", validate=False)

    assert error.value.code == "model_prefs_invalid"
    assert path.read_text(encoding="utf-8") == "{broken"
    assert model_prefs.inspect_prefs()[1] == "model_prefs_invalid"


def test_mutation_does_not_overwrite_invalid_nested_preferences(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    path = model_prefs.prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = '{"version":1,"task_models":{"chat":["not-a-model-id"]}}'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(model_prefs.ModelPrefsError) as error:
        model_prefs.set_model(model="flash", validate=False)

    assert error.value.code == "model_prefs_invalid"
    assert path.read_text(encoding="utf-8") == original
    assert model_prefs.inspect_prefs()[1] == "model_prefs_invalid"


def test_concurrent_task_updates_preserve_both_preferences(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    start = threading.Barrier(3)
    errors = []

    def update(task, model):
        try:
            start.wait(timeout=5)
            model_prefs.set_model(
                model=model,
                task=task,
                validate=False,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the assertion
            errors.append(exc)

    first = threading.Thread(target=update, args=("chat", "flash"))
    second = threading.Thread(target=update, args=("code", "opus"))
    first.start()
    second.start()
    start.wait(timeout=5)
    first.join(timeout=5)
    second.join(timeout=5)

    assert errors == []
    assert model_prefs.load_prefs()["task_models"] == {
        "chat": "gemini-3.5-flash-high",
        "code": "claude-opus-4-6-thinking",
    }


def test_mcp_model_tools_registered():
    from google_antigravity_codex import mcp_server

    names = {t["name"] for t in mcp_server.tool_definitions()}
    assert "google_antigravity_set_model" in names
    assert "google_antigravity_get_model_prefs" in names
    assert "google_antigravity_clear_model_prefs" in names
