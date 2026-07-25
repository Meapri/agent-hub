from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agent_hub import (
    capabilities,
    operations,
    orchestrator,
    provider_registry,
    provider_settings,
)
from agent_hub.core.inprocess import make_resolver
from agent_hub.core import media
from claude_codex import chat as claude_chat
from claude_codex import search as claude_search
from grok_codex import chat as grok_chat
from grok_codex import image as grok_image
from grok_codex import search as grok_search


def _spec(name: str):
    return next(item for item in operations.tool_definitions() if item["name"] == name)


def test_provider_registry_is_the_ordered_metadata_source():
    assert provider_registry.AVAILABLE_PROVIDERS == ("claude", "grok", "gemini", "gpt")
    assert provider_registry.DEFAULT_COMPARE_PROVIDERS == ("claude", "grok", "gemini")
    assert provider_registry.normalize("anthropic") == "claude"
    assert provider_registry.normalize("google-antigravity") == "gemini"
    assert provider_registry.chat_tools(planner_only=True) == (
        "claude_codex_chat",
        "grok_codex_chat",
        "google_antigravity_chat",
        "openai_codex_chat",
    )
    manifest = orchestrator.capability_manifest()
    assert manifest["chat"]["providers"] == ["claude", "grok", "gemini", "gpt"]
    assert manifest["search"]["providers"] == ["claude", "grok", "gemini"]


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
    assert _spec("agent_hub_chat")["inputSchema"]["properties"]["reasoning_effort"][
        "enum"
    ] == ["low", "medium", "high", "xhigh", "max", "ultra"]
    assert _spec("agent_hub_write")["inputSchema"]["properties"][
        "quality_rewrite_attempts"
    ]["default"] == 2


@pytest.mark.parametrize(
    ("provider", "model", "supported"),
    [
        ("claude", "claude-haiku-4-5-20251001", False),
        ("claude", "claude-sonnet-5", True),
        ("grok", "grok-4.3", False),
        ("grok", "grok-4.5", True),
        ("gemini", "claude-sonnet-4-6-thinking", False),
        ("gemini", "gemini-3.5-flash-high", True),
        ("gpt", "gpt-5.6-sol", True),
    ],
)
def test_reasoning_effort_support_uses_adapter_model_truth(provider, model, supported):
    assert capabilities.supports_reasoning_effort(provider, model) is supported


def test_chat_raw_omits_only_implicit_effort_for_saved_unsupported_model(monkeypatch):
    calls = []

    monkeypatch.setattr(
        operations.provider_settings,
        "get",
        lambda provider: {"model": "claude-haiku-4-5-20251001"}
        if provider == "claude"
        else {},
    )
    monkeypatch.setattr(
        operations.consistency_gate,
        "prepare_provider_call",
        lambda args: (args, {}),
    )
    monkeypatch.setattr(
        operations.claude_mcp,
        "dispatch_tool",
        lambda _name, arguments: calls.append(dict(arguments))
        or {"success": True, "text": "ok", "warnings": []},
    )

    automatic = operations._chat_raw(
        "claude",
        {
            "prompt": "automatic",
            "reasoning_effort": "high",
            "_reasoning_effort_implicit": True,
        },
    )
    operations._chat_raw(
        "claude",
        {"prompt": "explicit", "reasoning_effort": "high"},
    )

    assert "reasoning_effort" not in calls[0]
    assert calls[1]["reasoning_effort"] == "high"
    assert automatic["warnings"] == [
        "automatic_reasoning_effort_omitted:claude:claude-haiku-4-5-20251001"
    ]


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


def test_readme_write_rewrites_failed_korean_quality_once(tmp_path, monkeypatch):
    calls = []

    def fake_chat(provider, arguments):
        calls.append(arguments["prompt"])
        text = (
            "# 안내\n\n이전 이름은 지원하지 않습니다."
            if len(calls) == 1
            else "# 안내\n\n새 이름만 사용할 수 있습니다. 설치 방법은 아래에서 확인하세요."
        )
        return {"success": True, "text": text, "model": "claude-test", "warnings": []}

    monkeypatch.setattr(operations, "_chat_raw", fake_chat)
    result = operations.dispatch_tool(
        "agent_hub_write",
        {
            "provider": "claude",
            "task": "readme",
            "instruction": "간단한 안내를 작성해 주세요.",
            "project_root": str(tmp_path),
        },
    )

    assert result["success"] is True
    assert len(calls) == 2
    assert result["data"]["quality_gate"] == {
        "applied": True,
        "passed": True,
        "checker_version": "3",
        "rewrite_attempts": 1,
        "warnings": [],
        "policy_source": None,
    }
    assert "quality_rewrite_applied:1" in result["warnings"]
    assert "Draft to replace" in calls[1]


def test_readme_write_fails_closed_when_rewrite_still_fails(tmp_path, monkeypatch):
    calls = []

    def fake_chat(provider, arguments):
        calls.append(arguments["prompt"])
        return {
            "success": True,
            "text": "# 안내\n\n이전 이름은 지원하지 않습니다.",
            "model": "claude-test",
            "warnings": [],
        }

    monkeypatch.setattr(operations, "_chat_raw", fake_chat)
    result = operations.dispatch_tool(
        "agent_hub_write",
        {
            "provider": "claude",
            "task": "readme",
            "instruction": "안내를 작성해 주세요.",
            "project_root": str(tmp_path),
            "quality_rewrite_attempts": 1,
        },
    )

    assert len(calls) == 2
    assert result["success"] is False
    assert result["error"]["type"] == "document_quality_failed"
    assert result["data"]["quality_gate"]["passed"] is False


@pytest.mark.parametrize(
    ("draft", "warning_prefix"),
    [
        (
            "# Setup\n\n[설치 버전 확인 필요 — placeholder]\n",
            "placeholder_in_final_document:",
        ),
        (
            "# Setup\n\nTODO: add the real install command later.\n",
            "placeholder_in_final_document:",
        ),
        (
            "# Layout\n\nUse `src/missing_runtime.py` to start the service.\n",
            "repository_path_not_found:src/missing_runtime.py",
        ),
        (
            "# Layout\n\n```text\nrepo/\n├── src/\n│   └── ghost.py\n```\n",
            "repository_path_not_found:src/ghost.py",
        ),
    ],
)
def test_readme_quality_blocks_placeholders_and_missing_repository_paths(
    tmp_path, monkeypatch, draft, warning_prefix
):
    (tmp_path / "README.md").write_text("old\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real_runtime.py").write_text("VALUE = 1\n", encoding="utf-8")

    monkeypatch.setattr(
        operations,
        "_chat_raw",
        lambda *_args, **_kwargs: {
            "success": True,
            "text": draft,
            "model": "claude-test",
            "warnings": [],
        },
    )
    result = operations.dispatch_tool(
        "agent_hub_write",
        {
            "provider": "claude",
            "task": "readme",
            "instruction": "Write a final README.",
            "project_root": str(tmp_path),
            "quality_rewrite_attempts": 0,
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "document_quality_failed"
    assert any(
        warning.startswith(warning_prefix)
        for warning in result["data"]["quality_gate"]["warnings"]
    )


def test_readme_quality_accepts_existing_repository_path(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "settings.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        operations,
        "_chat_raw",
        lambda *_args, **_kwargs: {
            "success": True,
            "text": (
                "# Setup\n\nRun `src/runtime.py` after configuring "
                "`.gemini/settings.json`.\n"
            ),
            "model": "claude-test",
            "warnings": [],
        },
    )

    result = operations.dispatch_tool(
        "agent_hub_write",
        {
            "provider": "claude",
            "task": "readme",
            "instruction": "Write a final README.",
            "project_root": str(tmp_path),
            "quality_rewrite_attempts": 0,
        },
    )

    assert result["success"] is True


def test_verify_tool_exposes_failed_quality_as_operation_failure(tmp_path):
    result = operations.dispatch_tool(
        "agent_hub_verify",
        {
            "text": "# 안내\n\n이전 이름은 지원하지 않습니다.",
            "project_root": str(tmp_path),
            "doc_class": "durable",
            "user_facing": True,
        },
    )

    assert result["success"] is False
    assert result["data"]["ok"] is False
    assert result["data"]["checker_version"] == "3"


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


def test_explicit_all_compare_includes_opt_in_gpt(monkeypatch):
    called = []

    def fake_chat(provider, arguments):
        called.append(provider)
        return {"success": True, "text": provider, "model": f"{provider}-test", "warnings": []}

    monkeypatch.setattr(operations, "_chat_raw", fake_chat)
    result = operations.dispatch_tool(
        "agent_hub_compare_models",
        {"provider": "all", "prompt": "compare", "min_successes": 2},
    )
    assert called == ["claude", "grok", "gemini", "gpt"]
    assert [item["provider"] for item in result["data"]["results"]] == called


def test_compare_surfaces_participant_model_compatibility_warnings(monkeypatch):
    def fake_chat(provider, _arguments):
        return {
            "success": True,
            "text": provider,
            "model": f"{provider}-test",
            "warnings": (
                ["automatic_reasoning_effort_omitted:claude:claude-haiku-test"]
                if provider == "claude"
                else []
            ),
        }

    monkeypatch.setattr(operations, "_chat_raw", fake_chat)
    result = operations.dispatch_tool(
        "agent_hub_compare_models",
        {
            "providers": ["claude", "grok"],
            "prompt": "compare",
            "min_successes": 2,
        },
    )

    assert (
        "automatic_reasoning_effort_omitted:claude:claude-haiku-test"
        in result["warnings"]
    )


def test_gpt_aliases_and_model_routing_are_canonical():
    assert provider_registry.normalize("codex") == "gpt"
    assert provider_registry.normalize("chatgpt") == "gpt"
    assert provider_registry.normalize("openai-codex") == "gpt"
    assert operations._auto_chat_provider({"model": "gpt-5.6-sol"}) == "gpt"


def test_gpt_chat_uses_canonical_agent_hub_envelope(monkeypatch):
    captured = {}

    def fake_dispatch(name, arguments):
        captured.update({"name": name, "arguments": arguments})
        return {
            "success": True,
            "provider": "gpt",
            "model": "gpt-test",
            "text": "answer",
            "warnings": [],
        }

    monkeypatch.setattr(operations.openai_mcp, "dispatch_tool", fake_dispatch)
    result = operations.dispatch_tool(
        "agent_hub_chat",
        {"provider": "gpt", "prompt": "question", "reasoning_effort": "high"},
    )
    assert captured["name"] == "openai_codex_chat"
    assert captured["arguments"]["prompt"] == "question"
    assert result["success"] is True
    assert result["provider"] == "gpt"
    assert result["data"]["provider"] == "gpt"


def test_gpt_status_models_and_gui_auth_guidance_use_canonical_envelopes(monkeypatch):
    status_refresh_args = []

    def fake_status(*, refresh=False):
        status_refresh_args.append(refresh)
        return {
            "logged_in": True,
            "configured": True,
            "auth_mode": "chatgpt",
            "plan_type": "pro",
            "email": "must-not-leak@example.test",
        }

    monkeypatch.setattr(
        operations.openai_security,
        "consent_status",
        lambda: {"user_consent": True},
    )
    monkeypatch.setattr(
        operations.openai_security,
        "require_consent",
        lambda: None,
    )
    monkeypatch.setattr(
        operations.openai_auth,
        "status",
        fake_status,
    )
    status = operations.dispatch_tool("agent_hub_status", {"provider": "gpt"})
    assert status_refresh_args == [False]
    assert status["success"] is True
    assert list(status["data"]["providers"]) == ["gpt"]
    assert status["data"]["providers"]["gpt"]["ready"] is True
    assert "must-not-leak" not in str(status)

    monkeypatch.setattr(
        operations.openai_models,
        "list_models",
        lambda _args: {
            "success": True,
            "provider": "gpt",
            "models": [{"id": "gpt-test"}],
        },
    )
    models = operations.dispatch_tool(
        "agent_hub_list_models",
        {"provider": "gpt"},
    )
    assert models["data"]["models"]["gpt"]["provider"] == "gpt"

    auth = operations.dispatch_tool(
        "agent_hub_auth_start",
        {"provider": "gpt"},
    )
    assert auth["success"] is False
    assert auth["data"]["error"] == "provider_gui_required"
    assert auth["provider"] == "gpt"
    next_action = auth["data"]["next_action"]
    assert next_action["type"] == "local_gui"
    assert next_action["provider"] == "gpt"
    assert next_action["args"] == []
    assert Path(next_action["command"]).is_absolute()
    assert Path(next_action["command"]).name == "agent-hub-connect"

    logout = operations.dispatch_tool(
        "agent_hub_auth_logout",
        {"provider": "gpt"},
    )
    assert logout["success"] is False
    assert logout["provider"] == "gpt"
    assert logout["data"]["error"] == "provider_gui_required"
    assert "token" not in str(logout).lower()


def test_private_gpt_leaf_is_inprocess_only_not_a_public_hub_tool():
    assert make_resolver()("openai_codex_chat") is not None
    with pytest.raises(ValueError, match="unknown canonical tool"):
        operations.dispatch_tool("openai_codex_chat", {"prompt": "question"})


def test_public_auth_tools_are_read_only_gui_guidance():
    auth_tools = [
        item
        for item in operations.tool_definitions()
        if item["name"].startswith("agent_hub_auth_")
    ]

    assert len(auth_tools) == 4
    for item in auth_tools:
        assert item["inputSchema"]["required"] == ["provider"]
        assert item["annotations"]["readOnlyHint"] is True
        assert item["annotations"]["destructiveHint"] is False
        assert item["annotations"]["idempotentHint"] is True


@pytest.mark.parametrize(
    "tool",
    [
        "agent_hub_auth_start",
        "agent_hub_auth_complete",
        "agent_hub_auth_refresh",
        "agent_hub_auth_logout",
    ],
)
def test_public_auth_tools_never_report_gui_guidance_as_success(tool):
    result = operations.dispatch_tool(tool, {"provider": "gpt"})

    assert result["success"] is False
    assert result["error"]["type"] == "provider_gui_required"
    assert result["data"]["error"] == "provider_gui_required"
    next_action = result["data"]["next_action"]
    assert Path(next_action["command"]).is_absolute()
    assert Path(next_action["command"]).name == "agent-hub-connect"
    assert next_action["args"] == []


def test_fixed_workflow_provider_maps_to_private_gpt_binding(tmp_path, monkeypatch):
    planned = operations.dispatch_tool(
        "agent_hub_plan_workflow",
        {
            "workflow_id": "repo_document",
            "preset": "proposal",
            "provider": "gpt",
            "prompt": "question",
            "project_root": str(tmp_path),
        },
    )
    assert planned["success"] is True
    draft = next(
        step for step in planned["data"]["plan"]["steps"] if step["id"] == "draft"
    )
    assert draft["tool"] == "openai_codex_chat"

    captured = {}

    def fake_run_auto(recipe_id, **kwargs):
        captured.update({"recipe_id": recipe_id, **kwargs})
        return {"ok": True, "status": "completed", "artifact": "answer"}

    monkeypatch.setattr(operations.broker, "run_auto", fake_run_auto)
    result = operations.dispatch_tool(
        "agent_hub_run_workflow",
        {
            "workflow_id": "repo_document",
            "preset": "proposal",
            "provider": "gpt",
            "prompt": "question",
            "project_root": str(tmp_path),
        },
    )
    assert result["success"] is True
    assert captured["bindings"]["chat"] == "openai_codex_chat"
    assert captured["bindings"]["write_ag"] == "openai_codex_chat"


def test_fixed_provider_rejects_a_conflicting_private_binding(tmp_path):
    result = operations.dispatch_tool(
        "agent_hub_plan_workflow",
        {
            "workflow_id": "repo_document",
            "preset": "proposal",
            "provider": "gpt",
            "bindings": {"write_ag": "claude_codex_chat"},
            "prompt": "question",
            "project_root": str(tmp_path),
        },
    )
    assert result["success"] is False
    assert "provider conflicts with explicit fixed bindings: write_ag" in result["text"]


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
    operations.dispatch_tool(
        "agent_hub_reset_settings",
        {"provider": "grok", "reset": "model"},
    )
    assert provider_settings.get("grok") == {"api_mode": "responses"}
    operations.dispatch_tool("agent_hub_reset_settings", {"provider": "grok"})
    assert provider_settings.get("grok") == {}


def test_all_provider_model_reset_preserves_non_model_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_CONFIG_DIR", str(tmp_path / "agent-hub"))
    monkeypatch.setenv(
        "GOOGLE_ANTIGRAVITY_CONFIG_DIR",
        str(tmp_path / "google"),
    )
    provider_settings.update(
        "claude",
        {"model": "claude-sonnet-5", "temperature": 0.2},
    )
    provider_settings.update(
        "grok",
        {"model": "grok-4.5", "api_mode": "responses"},
    )
    provider_settings.update("gpt", {"model": "gpt-5.6-sol"})
    operations.google_model_prefs.set_model(
        model="gemini-3.1-pro-high",
        task="chat",
        validate=False,
    )

    reset = operations.dispatch_tool(
        "agent_hub_reset_settings",
        {"provider": "all", "reset": "model", "task": "chat"},
    )

    assert reset["success"] is True
    assert provider_settings.get("claude") == {"temperature": 0.2}
    assert provider_settings.get("grok") == {"api_mode": "responses"}
    assert provider_settings.get("gpt") == {}
    assert "chat" not in operations.google_model_prefs.load_prefs()["task_models"]


def test_gemini_model_reset_without_task_clears_web_and_global_defaults(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "GOOGLE_ANTIGRAVITY_CONFIG_DIR",
        str(tmp_path / "google"),
    )
    operations.google_model_prefs.set_model(
        model="gemini-3.1-pro-high",
        validate=False,
    )
    operations.google_model_prefs.set_model(
        model="gemini-3.5-flash-high",
        task="chat",
        validate=False,
    )
    operations.google_model_prefs.set_model(
        model="claude-opus-4-6-thinking",
        task="code",
        validate=False,
    )

    reset = operations.dispatch_tool(
        "agent_hub_reset_settings",
        {"provider": "gemini", "reset": "model"},
    )

    assert reset["success"] is True
    prefs = operations.google_model_prefs.load_prefs()
    assert prefs["default_model"] == ""
    assert prefs["task_models"] == {"code": "claude-opus-4-6-thinking"}


def test_settings_validation_is_exact_and_text_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_CONFIG_DIR", str(tmp_path))

    image = operations.dispatch_tool(
        "agent_hub_update_settings",
        {"provider": "grok", "model": "grok-imagine-image"},
    )
    unknown = operations.dispatch_tool(
        "agent_hub_update_settings",
        {"provider": "gpt", "model": "gpt-made-up"},
    )

    assert image["success"] is False
    assert unknown["success"] is False
    assert provider_settings.get("grok") == {}
    assert provider_settings.get("gpt") == {}

    unchecked = operations.dispatch_tool(
        "agent_hub_update_settings",
        {"provider": "gpt", "model": "gpt-provider-preview", "validate": False},
    )
    assert unchecked["success"] is True
    assert provider_settings.get("gpt") == {"model": "gpt-provider-preview"}


def test_gemini_status_and_fixed_default_share_profile_precedence(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "GOOGLE_ANTIGRAVITY_CONFIG_DIR",
        str(tmp_path / "google"),
    )
    operations.google_model_prefs.set_model(
        model="gemini-3.5-flash-high",
        task="chat",
        validate=False,
    )
    operations.google_profiles.use_profile_tool(
        {
            "name": "coding",
            "apply_model_pref": False,
            "apply_provider": False,
        }
    )

    saved = operations._gemini_model_state()
    assert saved["default_model"] == "gemini-3.5-flash-high"
    assert saved["model_source"] == "saved_chat"

    operations.google_model_prefs.clear_prefs(default_scopes=True)
    profile = operations._gemini_model_state()
    assert profile["default_model"] == "gemini-3.1-pro-high"
    assert profile["model_source"] == "profile"
    assert profile["model_override_scope"] == "profile:coding"


def test_non_gemini_transport_reset_is_rejected_without_deleting_settings(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AGENT_HUB_CONFIG_DIR", str(tmp_path))
    provider_settings.update(
        "grok",
        {"model": "grok-4.5", "api_mode": "responses"},
    )

    reset = operations.dispatch_tool(
        "agent_hub_reset_settings",
        {"provider": "grok", "reset": "transport"},
    )

    assert reset["success"] is False
    assert provider_settings.get("grok") == {
        "model": "grok-4.5",
        "api_mode": "responses",
    }


def test_saved_model_precedence_and_source_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODEX_MODEL", "claude-env")
    provider_settings.update("claude", {"model": "claude-saved"})

    saved = operations._provider_model_state(
        "claude",
        fallback="claude-default",
        environment_name="CLAUDE_CODEX_MODEL",
    )
    assert saved["default_model"] == "claude-saved"
    assert saved["model_source"] == "saved"
    assert saved["model_managed_by_environment"] is False

    provider_settings.remove("claude", {"model"})
    environment = operations._provider_model_state(
        "claude",
        fallback="claude-default",
        environment_name="CLAUDE_CODEX_MODEL",
    )
    assert environment["default_model"] == "claude-env"
    assert environment["model_source"] == "environment"
    assert environment["model_managed_by_environment"] is True


def test_saved_model_is_injected_into_delegate_and_fixed_workflow(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AGENT_HUB_CONFIG_DIR", str(tmp_path / "config"))
    provider_settings.update("claude", {"model": "claude-fable-5"})

    delegated = operations.dispatch_tool(
        "agent_hub_delegate",
        {
            "capability": "chat",
            "instruction": "hello",
            "leaf": "claude_codex_chat",
            "project_root": str(tmp_path),
        },
    )
    assert delegated["data"]["arguments"]["model"] == "claude-fable-5"

    captured = {}

    def fake_run_auto(_recipe_id, **kwargs):
        captured.update(kwargs)
        return {"success": True, "artifact": "done"}

    monkeypatch.setattr(operations.broker, "run_auto", fake_run_auto)
    result = operations.dispatch_tool(
        "agent_hub_run_workflow",
        {
            "workflow_id": "repo_document",
            "preset": "proposal",
            "provider": "claude",
            "prompt": "question",
            "project_root": str(tmp_path),
        },
    )

    assert result["success"] is True
    assert captured["args"]["_provider_models"]["claude"] == "claude-fable-5"


def test_saved_text_model_is_not_injected_into_image_delegate(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "GOOGLE_ANTIGRAVITY_CONFIG_DIR",
        str(tmp_path / "google"),
    )
    operations.google_model_prefs.set_model(
        model="gemini-3.5-flash-high",
        task="chat",
        validate=False,
    )

    delegated = operations.dispatch_tool(
        "agent_hub_delegate",
        {
            "capability": "image",
            "instruction": "make an image",
            "leaf": "google_antigravity_generate_image",
            "project_root": str(tmp_path),
        },
    )

    assert "model" not in delegated["data"]["arguments"]


def test_gpt_settings_aliases_use_the_canonical_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_CONFIG_DIR", str(tmp_path))
    updated = operations.dispatch_tool(
        "agent_hub_update_settings",
        {"provider": "chatgpt", "model": "gpt-test", "validate": False},
    )
    assert updated["provider"] == "gpt"
    assert provider_settings.get("gpt") == {"model": "gpt-test"}

    refused = operations.dispatch_tool(
        "agent_hub_update_settings",
        {
            "provider": "gpt",
            "model": "gpt-test",
            "temperature": 0.2,
            "validate": False,
        },
    )
    assert refused["success"] is False
    assert refused["text"] == "unsupported gpt settings: temperature"
    assert provider_settings.get("gpt") == {"model": "gpt-test"}

    reset = operations.dispatch_tool(
        "agent_hub_reset_settings",
        {"provider": "openai-codex"},
    )
    assert reset["provider"] == "gpt"
    assert provider_settings.get("gpt") == {}


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
            "output_text": f"answer\n{grok_search.SEARCH_COMPLETE_MARKER}",
            "citations": ["https://x.com/example/status/1"],
        }

    monkeypatch.setattr(grok_search.api, "responses_create", fake_response)
    result = grok_search.run_search({"query": "q", "source": "both", "model": "grok-test"})
    assert [tool["type"] for tool in captured["tools"]] == ["web_search", "x_search"]
    assert result["sources"][0]["url"].startswith("https://x.com/")
    assert result["text"] == "answer"
    assert result["success"] is True


def test_grok_search_retries_and_replaces_incomplete_answer(monkeypatch):
    calls = []
    monkeypatch.setattr(grok_search.security, "require_consent", lambda: None)
    monkeypatch.setattr(grok_search.auth, "resolve_auth", lambda: {"mode": "api_key"})

    def fake_response(body, **_kwargs):
        calls.append(body)
        if len(calls) == 1:
            return {
                "status": "completed",
                "model": "grok-test",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "partial"}],
                    }
                ],
                "citations": ["https://x.com/example/status/1"],
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            }
        return {
            "status": "completed",
            "model": "grok-test",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                "complete answer\n"
                                f"{grok_search.SEARCH_COMPLETE_MARKER}"
                            ),
                            "annotations": [{"url": "https://official.test"}],
                        }
                    ],
                }
            ],
            "usage": {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24},
        }

    monkeypatch.setattr(grok_search.api, "responses_create", fake_response)

    result = grok_search.run_search(
        {"query": "q", "source": "both", "model": "grok-test", "retry_count": 1}
    )

    assert len(calls) == 2
    assert "previous attempt" in calls[1]["input"][0]["content"]
    assert result["text"] == "complete answer"
    assert result["success"] is True
    assert result["finish_reason"] == "completed"
    assert result["warnings"] == ["search_completion_recovered:1"]
    assert result["usage"] == {
        "prompt_tokens": 30,
        "completion_tokens": 6,
        "total_tokens": 36,
    }
    assert [item["url"] for item in result["sources"]] == ["https://official.test"]
    assert result["diagnostics"]["completion_attempts"] == 2


def test_grok_search_fails_closed_when_completion_marker_never_arrives(monkeypatch):
    monkeypatch.setattr(grok_search.security, "require_consent", lambda: None)
    monkeypatch.setattr(grok_search.auth, "resolve_auth", lambda: {"mode": "api_key"})
    monkeypatch.setattr(
        grok_search.api,
        "responses_create",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "model": "grok-test",
            "output_text": "partial answer",
            "citations": ["https://x.com/example/status/1"],
        },
    )

    result = grok_search.run_search(
        {"query": "q", "model": "grok-test", "retry_count": 0}
    )

    assert result["success"] is False
    assert result["finish_reason"] == "incomplete"
    assert result["warnings"] == [
        "incomplete_search_answer:missing_completion_marker"
    ]


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
