from __future__ import annotations

import json

import pytest

from openai_codex import auth, client, mcp_server, models
from openai_codex.errors import CodexSideEffectRefused


def _jsonl(*events):
    return "".join(json.dumps(event) + "\n" for event in events)


def test_exec_argv_is_prompt_free_and_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "codex_binary", lambda: "/test/codex")
    argv = client.exec_argv(
        cwd=str(tmp_path),
        model="gpt-test",
        reasoning_effort="high",
        image_paths=[str(tmp_path / "frame.png")],
    )
    assert argv[0:2] == ["/test/codex", "exec"]
    assert "--ephemeral" in argv
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "shell_tool" in argv
    assert 'web_search="disabled"' in argv
    assert argv[-1] == "-"
    assert "private prompt" not in argv


def test_safe_env_removes_api_key_billing_overrides(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("CODEX_API_KEY", "codex-test-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.invalid")
    safe = client._safe_env()
    assert "OPENAI_API_KEY" not in safe
    assert "CODEX_API_KEY" not in safe
    assert "OPENAI_BASE_URL" not in safe


def test_exec_chat_extracts_final_text_and_usage(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "codex_binary", lambda: "/test/codex")
    monkeypatch.setattr(
        client,
        "run_bounded",
        lambda *_args, **_kwargs: (
            _jsonl(
                {"type": "thread.started", "thread_id": "t"},
                {
                    "type": "item.completed",
                    "item": {"id": "m", "type": "agent_message", "text": "answer"},
                },
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 2,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 1,
                    },
                },
            ),
            "",
            0,
        ),
    )
    result = client.run_exec_chat("private prompt", cwd=str(tmp_path), model="gpt-test")
    assert result["text"] == "answer"
    assert result["usage"]["total_tokens"] == 15


@pytest.mark.parametrize(
    "item_type",
    ["command_execution", "file_change", "mcp_tool_call", "collab_tool_call", "web_search"],
)
def test_exec_chat_fails_closed_on_side_effect_items(monkeypatch, tmp_path, item_type):
    monkeypatch.setattr(client, "codex_binary", lambda: "/test/codex")
    monkeypatch.setattr(
        client,
        "run_bounded",
        lambda *_args, **_kwargs: (
            _jsonl(
                {
                    "type": "item.started",
                    "item": {"id": "side-effect", "type": item_type},
                },
                {"type": "turn.completed", "usage": {}},
            ),
            "",
            0,
        ),
    )
    with pytest.raises(CodexSideEffectRefused):
        client.run_exec_chat("do not act", cwd=str(tmp_path))


def test_app_server_batch_uses_honest_client_identity(monkeypatch):
    captured = {}

    def fake_messages(payload, *, expected_ids, timeout):
        captured["messages"] = [json.loads(line) for line in payload.splitlines()]
        captured["expected_ids"] = expected_ids
        captured["timeout"] = timeout
        return [
            {"id": 1, "result": {"userAgent": "codex"}},
            {"id": 2, "result": {"account": {"type": "chatgpt", "email": "secret"}}},
        ]

    monkeypatch.setattr(client, "_app_server_messages", fake_messages)
    result = client.app_server_request("account/read", {"refreshToken": False})
    assert result["account"]["type"] == "chatgpt"
    assert captured["expected_ids"] == {1, 2}
    assert captured["timeout"] == 20.0
    assert captured["messages"][0]["params"]["clientInfo"]["name"] == "agent_hub"
    assert captured["messages"][1] == {"method": "initialized", "params": {}}


def test_auth_status_redacts_account_identity(monkeypatch):
    monkeypatch.setattr(
        client,
        "app_server_request",
        lambda *_args, **_kwargs: {
            "account": {"type": "chatgpt", "email": "private@example.com", "planType": "plus"},
            "requiresOpenaiAuth": True,
        },
    )
    result = auth.status()
    assert result["configured"] is True
    assert result["auth_mode"] == "chatgpt"
    assert result["plan_type"] == "plus"
    assert "email" not in result
    assert "private@example.com" not in json.dumps(result)


def test_api_key_login_is_not_treated_as_subscription(monkeypatch):
    monkeypatch.setattr(
        client,
        "app_server_request",
        lambda *_args, **_kwargs: {
            "account": {"type": "apiKey"},
            "requiresOpenaiAuth": True,
        },
    )
    result = auth.status()
    assert result["logged_in"] is True
    assert result["configured"] is False
    assert result["warning"] == "codex_api_key_mode_not_subscription"


def test_auth_status_falls_back_to_official_cli(monkeypatch):
    def fail_app_server(*_args, **_kwargs):
        raise RuntimeError("state database unavailable")

    monkeypatch.setattr(client, "app_server_request", fail_app_server)
    monkeypatch.setattr(client, "codex_binary", lambda: "/test/codex")
    monkeypatch.setattr(
        client,
        "run_bounded",
        lambda argv, **_kwargs: (
            "Logged in using ChatGPT\n",
            "",
            0,
        ),
    )

    result = auth.status()

    assert result["configured"] is True
    assert result["logged_in"] is True
    assert result["auth_mode"] == "chatgpt"
    assert result["status_source"] == "codex_cli"
    assert result["status_warning"] == "codex_app_server_unavailable"


def test_auth_status_does_not_treat_not_logged_in_cli_output_as_login(monkeypatch):
    monkeypatch.setattr(
        client,
        "app_server_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("state database unavailable")
        ),
    )
    monkeypatch.setattr(client, "codex_binary", lambda: "/test/codex")
    monkeypatch.setattr(
        client,
        "run_bounded",
        lambda *_args, **_kwargs: ("Not logged in\n", "", 0),
    )

    result = auth.status()

    assert result["configured"] is False
    assert result["logged_in"] is False
    assert result["auth_mode"] is None


def test_model_list_uses_official_catalog_and_no_curated_synthetic(monkeypatch):
    monkeypatch.setattr(models.security, "require_consent", lambda: None)
    monkeypatch.setattr(models.auth, "require_subscription", lambda **_kwargs: {})
    monkeypatch.setattr(
        models.client,
        "app_server_request",
        lambda *_args, **_kwargs: {
            "data": [
                {
                    "id": "internal-id",
                    "model": "gpt-current",
                    "displayName": "GPT Current",
                    "description": "Current model",
                    "hidden": False,
                    "isDefault": True,
                    "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                    "inputModalities": ["text", "image"],
                }
            ],
            "nextCursor": None,
        },
    )
    result = models.list_models()
    assert result["default_model"] == "gpt-current"
    assert [item["id"] for item in result["models"]] == ["gpt-current"]
    assert result["models"][0]["source"] == "codex-app-server"


def test_model_list_follows_bounded_catalog_pagination(monkeypatch):
    monkeypatch.setattr(models.security, "require_consent", lambda: None)
    monkeypatch.setattr(models.auth, "require_subscription", lambda **_kwargs: {})
    calls = []

    def list_page(_method, params, **_kwargs):
        calls.append(dict(params))
        if not params.get("cursor"):
            return {
                "data": [{"model": "gpt-page-1"}],
                "nextCursor": "page-2",
            }
        return {
            "data": [{"model": "gpt-page-2"}],
            "nextCursor": None,
        }

    monkeypatch.setattr(models.client, "app_server_request", list_page)

    result = models.list_models()

    assert [item["id"] for item in result["models"]] == [
        "gpt-page-1",
        "gpt-page-2",
    ]
    assert calls == [
        {"includeHidden": False, "limit": 100},
        {"includeHidden": False, "limit": 100, "cursor": "page-2"},
    ]
    assert "model_catalog_truncated" not in result["warnings"]


def test_model_list_can_include_hidden_catalog_entries(monkeypatch):
    monkeypatch.setattr(models.security, "require_consent", lambda: None)
    monkeypatch.setattr(models.auth, "require_subscription", lambda **_kwargs: {})
    monkeypatch.setattr(
        models.client,
        "app_server_request",
        lambda *_args, **_kwargs: {
            "data": [{"model": "gpt-hidden", "hidden": True}],
            "nextCursor": None,
        },
    )
    assert models.list_models()["models"] == []
    result = models.list_models({"include_hidden": True})
    assert [item["id"] for item in result["models"]] == ["gpt-hidden"]


def test_leaf_chat_consent_cannot_be_bypassed(monkeypatch):
    monkeypatch.setattr(mcp_server.security, "require_consent", lambda: (_ for _ in ()).throw(
        RuntimeError("explicit consent required")
    ))
    result = mcp_server.dispatch_tool("openai_codex_chat", {"prompt": "hello"})
    assert result["success"] is False
    assert "consent" in result["text"].lower()


def test_shared_logout_is_refused():
    result = mcp_server.dispatch_tool("openai_codex_logout", {})
    assert result["success"] is False
    assert result["error"] == "shared_codex_logout_refused"
    assert result["next_action"]["command"] == "codex logout"
