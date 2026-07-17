from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HUBS = ROOT / "hubs"


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_codex_and_claude_plugins_share_one_adaptive_skill_contract():
    canonical = (HUBS / "shared/skills/adaptive-orchestrate/SKILL.md").read_text(
        encoding="utf-8"
    )
    for hub in ("codex", "claude-code"):
        installed = (HUBS / hub / "skills/adaptive-orchestrate/SKILL.md").read_text(
            encoding="utf-8"
        )
        assert installed == canonical
        assert 'workflow_id="adaptive"' in installed
        assert "agent_hub_plan_workflow" in installed
        assert "agent_hub_run_workflow" in installed
        assert "순서를\n직접 하드코딩하지 않는다" in installed


def test_hub_plugins_register_only_unified_agent_hub_and_memory():
    codex_mcp = _json(HUBS / "codex/.mcp.json")
    claude_mcp = _json(HUBS / "claude-code/.mcp.json")["mcpServers"]
    assert set(codex_mcp) == {"agent-hub", "memory"}
    assert set(claude_mcp) == {"agent-hub", "memory"}
    assert codex_mcp["agent-hub"]["command"].endswith("/.venv/bin/agent-hub-mcp")
    assert claude_mcp["agent-hub"]["command"].endswith("/.venv/bin/agent-hub-mcp")


def test_plugin_manifests_and_claude_commands_describe_adaptive_engine():
    codex = _json(HUBS / "codex/.codex-plugin/plugin.json")
    claude = _json(HUBS / "claude-code/.claude-plugin/plugin.json")
    assert codex["version"] == "1.2.0"
    assert claude["version"] == "1.2.0"
    assert codex["mcpServers"] == "./.mcp.json"
    assert "adaptive" in codex["description"].lower()
    assert "adaptive" in claude["description"].lower()
    for name in ("agent-hub-plan.md", "agent-hub-run.md"):
        command = (HUBS / "claude-code/commands" / name).read_text(encoding="utf-8")
        assert "adaptive" in command
        assert "project_root" in command


def test_local_marketplaces_install_the_matching_app_plugin():
    codex = _json(ROOT / ".agents/plugins/marketplace.json")
    claude = _json(ROOT / ".claude-plugin/marketplace.json")
    assert codex["name"] == "agent-hub"
    assert codex["plugins"][0]["source"]["path"] == "./hubs/codex"
    assert claude["name"] == "agent-hub"
    assert claude["plugins"][0]["source"] == "./hubs/claude-code"
    assert codex["plugins"][0]["name"] == claude["plugins"][0]["name"] == "agent-hub"
    assert claude["plugins"][0]["version"] == "1.2.0"
