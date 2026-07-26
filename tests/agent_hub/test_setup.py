from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from agent_hub import local_setup

ROOT = Path(__file__).resolve().parents[2]


def _roots(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    return source, target


def test_setup_defaults_to_a_read_only_plan_then_applies_idempotently(
    tmp_path,
):
    source, target = _roots(tmp_path)

    plan = local_setup.plan_setup(source, target_root=target)

    assert len(plan.changed) == len(local_setup.CONFIG_PATHS)
    assert all(item.status == "create" for item in plan.changed)
    assert not any((target / item).exists() for item in local_setup.CONFIG_PATHS)
    public = plan.public()
    assert public["apply_required"] is True
    assert "dependency installation" in public["actions_excluded"]

    result = local_setup.apply_plan(plan)
    assert result["success"] is True
    assert result["applied"] == len(local_setup.CONFIG_PATHS)

    second = local_setup.plan_setup(source, target_root=target)
    assert second.changed == ()
    assert local_setup.apply_plan(second)["applied"] == 0

    expected_command = str(source / ".venv" / "bin" / "agent-hub-mcp")
    root_config = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
    codex_hub = json.loads((target / "hubs" / "codex" / ".mcp.json").read_text(encoding="utf-8"))
    assert root_config["mcpServers"]["agent-hub"]["command"] == expected_command
    assert codex_hub["agent-hub"]["command"] == expected_command
    assert set(root_config["mcpServers"]) == {"agent-hub"}
    assert set(codex_hub) == {"agent-hub"}


def test_setup_preserves_unrelated_json_and_toml_settings(tmp_path):
    source, target = _roots(tmp_path)
    gemini_path = target / ".gemini" / "settings.json"
    gemini_path.parent.mkdir(parents=True)
    gemini_path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "mcpServers": {
                    "custom": {
                        "command": "custom-mcp",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    codex_path = target / ".codex" / "config.toml"
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        "\n".join(
            (
                "[features]",
                "experimental = true",
                "",
                "[mcp_servers.memory]",
                'command = "old-memory"',
                "",
                "[mcp_servers.memory.env]",
                'BASIC_MEMORY_HOME = "/old/path"',
                "",
                "[mcp_servers.agent-hub]",
                'command = "/old/hub"',
                "",
            )
        ),
        encoding="utf-8",
    )

    local_setup.apply_plan(local_setup.plan_setup(source, target_root=target))

    gemini = json.loads(gemini_path.read_text(encoding="utf-8"))
    assert gemini["theme"] == "dark"
    assert gemini["contextFileName"] == "AGENTS.md"
    assert gemini["mcpServers"]["custom"]["command"] == "custom-mcp"
    assert gemini["mcpServers"]["agent-hub"]["command"].startswith(str(source))

    codex = codex_path.read_text(encoding="utf-8")
    assert "[features]" in codex
    assert "experimental = true" in codex
    assert codex.count(local_setup.MANAGED_TOML_BEGIN) == 1
    assert 'command = "old-memory"' not in codex
    assert 'command = "/old/hub"' not in codex


def test_setup_migrates_user_settings_out_of_legacy_managed_block(tmp_path):
    source, target = _roots(tmp_path)
    codex_path = target / ".codex" / "config.toml"
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        "\n".join(
            (
                local_setup.MANAGED_TOML_BEGIN,
                'approval_policy = "never"',
                'sandbox_mode = "danger-full-access"',
                "[mcp_servers.memory]",
                'command = "old-memory"',
                "[mcp_servers.agent-hub]",
                'command = "/old/hub"',
                local_setup.MANAGED_TOML_END,
                "",
            )
        ),
        encoding="utf-8",
    )

    local_setup.apply_plan(local_setup.plan_setup(source, target_root=target))

    codex = codex_path.read_text(encoding="utf-8")
    prefix, managed = codex.split(local_setup.MANAGED_TOML_BEGIN, 1)
    assert 'approval_policy = "never"' in prefix
    assert 'sandbox_mode = "danger-full-access"' in prefix
    assert 'command = "old-memory"' not in codex
    assert 'command = "/old/hub"' not in codex
    assert managed.count("[mcp_servers.agent-hub]") == 1


def test_setup_uses_all_file_cas_before_writing(tmp_path):
    source, target = _roots(tmp_path)
    plan = local_setup.plan_setup(source, target_root=target)
    conflicting = target / ".mcp.json"
    conflicting.write_text('{"user": "changed"}\n', encoding="utf-8")

    with pytest.raises(local_setup.SetupError, match="changed after planning"):
        local_setup.apply_plan(plan)

    assert not (target / ".codex" / "config.toml").exists()
    assert conflicting.read_text(encoding="utf-8") == '{"user": "changed"}\n'


def test_setup_refuses_malformed_or_symlinked_targets(tmp_path):
    source, target = _roots(tmp_path)
    malformed = target / ".cursor" / "mcp.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("{not-json", encoding="utf-8")

    with pytest.raises(local_setup.SetupError, match="valid UTF-8 JSON"):
        local_setup.plan_setup(source, target_root=target)

    malformed.unlink()
    actual = target / "actual.json"
    actual.write_text("{}\n", encoding="utf-8")
    malformed.symlink_to(actual)
    with pytest.raises(local_setup.SetupError, match="symlinked"):
        local_setup.plan_setup(source, target_root=target)


def test_setup_refuses_symlinked_parents_and_hardlinks(tmp_path):
    source, target = _roots(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / ".codex").symlink_to(outside, target_is_directory=True)
    with pytest.raises(local_setup.SetupError, match="unsafe config parent"):
        local_setup.plan_setup(source, target_root=target)

    (target / ".codex").unlink()
    cursor = target / ".cursor"
    cursor.mkdir()
    original = tmp_path / "shared.json"
    original.write_text("{}\n", encoding="utf-8")
    (cursor / "mcp.json").hardlink_to(original)
    with pytest.raises(local_setup.SetupError, match="hard-linked"):
        local_setup.plan_setup(source, target_root=target)


def test_setup_cli_dry_run_and_check_do_not_write(tmp_path, capsys):
    source, target = _roots(tmp_path)

    assert (
        local_setup.main(
            [
                "--repo-root",
                str(source),
                "--target-root",
                str(target),
            ]
        )
        == 0
    )
    assert "dry-run" in capsys.readouterr().out
    assert not (target / ".mcp.json").exists()

    assert (
        local_setup.main(
            [
                "--repo-root",
                str(source),
                "--target-root",
                str(target),
                "--check",
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["applied"] == 0
    assert not (target / ".mcp.json").exists()


def test_machine_local_runtime_configs_are_ignored_not_tracked():
    listed = subprocess.run(
        ["git", "ls-files", "--", *local_setup.CONFIG_PATHS],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert listed.stdout.strip() == ""
    ignored = subprocess.run(
        ["git", "check-ignore", "--", *local_setup.CONFIG_PATHS],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert set(ignored.stdout.splitlines()) == set(local_setup.CONFIG_PATHS)
    canonical = "\n".join(
        (
            (ROOT / ".gitignore").read_text(encoding="utf-8"),
            (ROOT / "instructions/.ruler/ruler.toml").read_text(encoding="utf-8"),
            (ROOT / "src/agent_hub/local_setup.py").read_text(encoding="utf-8"),
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "hubs/codex/README.md").read_text(encoding="utf-8"),
            (ROOT / "hubs/claude-code/README.md").read_text(encoding="utf-8"),
        )
    )
    assert "/Users/naen/" not in canonical
