from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
from unittest.mock import patch

import pytest

from google_antigravity_codex import cli


def completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(["agy"], returncode, stdout=stdout, stderr=stderr)


def test_run_detaches_child_stdin_from_mcp_transport():
    with patch.object(subprocess, "run", return_value=completed()) as run:
        cli._run(["agy", "models"], timeout=1)

    assert run.call_args.kwargs["stdin"] is subprocess.DEVNULL


def test_inspect_cli_reports_tested_1_1_2():
    with patch.object(cli, "find_executable", return_value="/tmp/agy"), patch.object(
        cli, "_run", return_value=completed(stdout="1.1.2\n")
    ):
        info = cli.inspect_cli()

    assert info.installed is True
    assert info.compatible is True
    assert info.tested is True
    assert info.version == "1.1.2"


def test_require_cli_rejects_release_without_agent_flag_support():
    with patch.object(
        cli,
        "inspect_cli",
        return_value=cli.CliInfo(executable="agy", version="1.1.0", installed=True),
    ):
        with pytest.raises(cli.CliError) as error:
            cli.require_cli()

    assert error.value.code == "agy_cli_unsupported"


def test_list_models_maps_current_display_names(monkeypatch):
    output = "Gemini 3.5 Flash (High)\nClaude Sonnet 4.6 (Thinking)\n"
    monkeypatch.setenv("CODEX_SHELL", "1")
    monkeypatch.setenv("GH_TOKEN", "secret")
    with patch.object(
        cli,
        "require_cli",
        return_value=cli.CliInfo(executable="agy", version="1.1.2", installed=True, compatible=True),
    ), patch.object(cli, "_run", return_value=completed(stdout=output)) as run:
        models = cli.list_models()

    assert models == [
        {
            "id": "gemini-3.5-flash-high",
            "display_name": "Gemini 3.5 Flash (High)",
            "source": "agy models",
        },
        {
            "id": "claude-sonnet-4-6-thinking",
            "display_name": "Claude Sonnet 4.6 (Thinking)",
            "source": "agy models",
        },
    ]
    child_env = run.call_args.kwargs["env"]
    assert "CODEX_SHELL" not in child_env
    assert "GH_TOKEN" not in child_env


def test_run_prompt_plan_uses_disposable_cwd_and_keeps_stderr_private(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ENABLE_CLI_BRIDGE", "1")
    seen = {}

    def fake_run(command, *, timeout, cwd=None, env=None):
        seen.update({"command": command, "timeout": timeout, "cwd": cwd, "env": env})
        seen["cwd_mode"] = stat.S_IMODE(cwd.stat().st_mode)
        (cwd / "unexpected.txt").write_text("confined", encoding="utf-8")
        return completed(stdout="AGY_OK\n")

    with patch.object(
        cli,
        "require_cli",
        return_value=cli.CliInfo(executable="/tmp/agy", version="1.1.2", installed=True, compatible=True),
    ), patch.object(cli, "_run", side_effect=fake_run):
        result = cli.run_prompt(
            {
                "prompt": "Reply AGY_OK",
                "model": "Gemini 3.5 Flash (Low)",
                "mode": "plan",
                "sandbox": True,
                "timeout_sec": 60,
            }
        )

    assert result["text"] == "AGY_OK"
    assert result["backend"] == "agy-cli"
    assert seen["command"] == [
        "/tmp/agy",
        "--print",
        "Reply AGY_OK",
        "--model",
        "Gemini 3.5 Flash (Low)",
        "--mode",
        "plan",
        "--sandbox",
        "--print-timeout",
        "60s",
    ]
    assert seen["cwd"] != Path(tmp_path).resolve()
    if os.name == "posix":
        assert seen["cwd_mode"] == 0o700
    assert not seen["cwd"].exists()
    assert not (tmp_path / "unexpected.txt").exists()
    assert seen["env"]["GOOGLE_ANTIGRAVITY_CLI_BRIDGE_DEPTH"] == "1"


def test_run_prompt_plan_rejects_workspace_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ENABLE_CLI_BRIDGE", "1")
    with pytest.raises(cli.CliError) as error:
        cli.run_prompt({"prompt": "inspect", "mode": "plan", "cwd": str(tmp_path)})
    assert error.value.code == "agy_cli_plan_cwd_blocked"


def test_run_prompt_accept_edits_uses_explicit_workspace_after_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ENABLE_CLI_BRIDGE", "1")
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ALLOW_MUTATING_CLI", "1")
    seen = {}

    def fake_run(command, *, timeout, cwd=None, env=None):
        seen["cwd"] = cwd
        return completed(stdout="edited\n")

    with patch.object(
        cli,
        "require_cli",
        return_value=cli.CliInfo(executable="agy", version="1.1.2", installed=True, compatible=True),
    ), patch.object(cli, "_run", side_effect=fake_run):
        cli.run_prompt({"prompt": "edit", "mode": "accept-edits", "cwd": str(tmp_path)})

    assert seen["cwd"] == tmp_path.resolve()


def test_run_prompt_strips_unrelated_secrets_from_child_environment(monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ENABLE_CLI_BRIDGE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("CODEX_SHELL", "1")
    monkeypatch.setenv("MCP_SESSION_ID", "host-session")
    monkeypatch.setenv("SAFE_BUILD_LABEL", "keep-me")
    seen = {}

    def fake_run(command, *, timeout, cwd=None, env=None):
        seen["env"] = env
        return completed(stdout="safe\n")

    with patch.object(
        cli,
        "require_cli",
        return_value=cli.CliInfo(executable="agy", version="1.1.2", installed=True, compatible=True),
    ), patch.object(cli, "_run", side_effect=fake_run):
        cli.run_prompt({"prompt": "hi"})

    assert "OPENAI_API_KEY" not in seen["env"]
    assert "GH_TOKEN" not in seen["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in seen["env"]
    assert "SSH_AUTH_SOCK" not in seen["env"]
    assert "CODEX_SHELL" not in seen["env"]
    assert "MCP_SESSION_ID" not in seen["env"]
    assert seen["env"]["SAFE_BUILD_LABEL"] == "keep-me"
    assert seen["env"]["GOOGLE_ANTIGRAVITY_CLI_BRIDGE_DEPTH"] == "1"


def test_run_prompt_defaults_to_sandbox_and_blocks_recursion(monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ENABLE_CLI_BRIDGE", "1")
    seen = {}

    def fake_run(command, *, timeout, cwd=None, env=None):
        seen["command"] = command
        return completed(stdout="safe\n")

    with patch.object(
        cli,
        "require_cli",
        return_value=cli.CliInfo(executable="agy", version="1.1.2", installed=True, compatible=True),
    ), patch.object(cli, "_run", side_effect=fake_run):
        cli.run_prompt({"prompt": "hi"})

    assert "--sandbox" in seen["command"]

    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CLI_BRIDGE_DEPTH", "1")
    with pytest.raises(cli.CliError, match="recursive") as error:
        cli.run_prompt({"prompt": "hi"})
    assert error.value.code == "agy_cli_recursion_blocked"


def test_run_prompt_rejects_mutating_mode_without_opt_in(monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ENABLE_CLI_BRIDGE", "1")
    with pytest.raises(cli.CliError) as error:
        cli.run_prompt({"prompt": "edit", "mode": "accept-edits"})
    assert error.value.code == "agy_cli_mutation_blocked"


def test_run_prompt_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_ENABLE_CLI_BRIDGE", raising=False)
    with pytest.raises(cli.CliError) as error:
        cli.run_prompt({"prompt": "hi"})
    assert error.value.code == "agy_cli_bridge_disabled"


def test_master_user_consent_enables_cli_bridge(monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_USER_CONSENT", "1")
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_ENABLE_CLI_BRIDGE", raising=False)
    with patch.object(
        cli,
        "require_cli",
        return_value=cli.CliInfo(executable="agy", version="1.1.2", installed=True, compatible=True),
    ), patch.object(cli, "_run", return_value=completed(stdout="consented\n")):
        result = cli.run_prompt({"prompt": "hi"})
    assert result["text"] == "consented"


def test_running_under_agy_blocks_nested_cli_even_with_consent(monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_USER_CONSENT", "1")
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_RUNNING_UNDER_AGY", "1")
    with pytest.raises(cli.CliError) as error:
        cli.run_prompt({"prompt": "hi"})
    assert error.value.code == "agy_cli_recursion_blocked"


def test_run_prompt_rejects_unsandboxed_execution(monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ENABLE_CLI_BRIDGE", "1")
    with patch.object(cli, "require_cli") as require_cli:
        with pytest.raises(cli.CliError) as error:
            cli.run_prompt({"prompt": "hi", "sandbox": False})
        assert error.value.code == "agy_cli_unsandboxed_blocked"

        monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ALLOW_UNSANDBOXED_CLI", "1")
        with pytest.raises(cli.CliError) as error:
            cli.run_prompt({"prompt": "hi", "mode": "plan", "sandbox": False})
        assert error.value.code == "agy_cli_unsandboxed_blocked"

    require_cli.assert_not_called()


def test_status_never_claims_keyring_access():
    info = cli.CliInfo(
        executable="agy", version="1.1.2", installed=True, compatible=True, tested=True
    )
    with patch.object(cli, "inspect_cli", return_value=info), patch.object(
        cli, "list_models", return_value=[{"id": "model", "display_name": "Model", "source": "agy models"}]
    ), patch.object(
        cli,
        "validate_plugin",
        return_value={"valid": True, "returncode": 0, "output": "ok", "plugin_root": "/tmp/plugin"},
    ):
        status = cli.status()

    assert status["success"] is True
    assert status["model_listing_ready"] is True
    assert status["authentication_state"] == "not_directly_inspectable"
    assert status["keyring_read_by_plugin"] is False
    assert "token" not in str(status).lower()


def test_status_skips_nested_model_probe_inside_codex_by_default(monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_RUNNING_UNDER_CODEX_MCP", "1")
    info = cli.CliInfo(
        executable="agy", version="1.1.2", installed=True, compatible=True, tested=True
    )
    with patch.object(cli, "inspect_cli", return_value=info), patch.object(
        cli, "list_models"
    ) as list_models, patch.object(
        cli,
        "validate_plugin",
        return_value={"valid": True, "returncode": 0, "output": "ok", "plugin_root": "/tmp/plugin"},
    ):
        status = cli.status()

    list_models.assert_not_called()
    assert status["success"] is True
    assert status["model_listing_ready"] is False
    assert status["model_probe_state"] == "skipped_under_codex"
    assert status["warnings"] == ["agy_model_probe_skipped_under_codex"]


def test_cli_error_output_is_redacted():
    raw = (
        "Authorization: Bearer top-secret code=oauth-code "
        'refresh_token="refresh-secret" client_secret=client-secret'
    )

    sanitized = cli._sanitize_output(raw)

    assert "top-secret" not in sanitized
    assert "oauth-code" not in sanitized
    assert "refresh-secret" not in sanitized
    assert "client-secret" not in sanitized
