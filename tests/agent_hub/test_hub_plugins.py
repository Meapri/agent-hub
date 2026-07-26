from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tomllib

from agent_hub import local_setup


ROOT = Path(__file__).resolve().parents[2]
HUBS = ROOT / "hubs"


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_codex_and_claude_plugins_share_all_skill_contracts():
    shared = sorted((HUBS / "shared/skills").glob("*/SKILL.md"))
    assert {path.parent.name for path in shared} == {
        "adaptive-orchestrate",
        "document-write",
        "gpt-provider",
        "handoff",
        "provider-connect",
        "route-to",
        "takeover",
    }
    for canonical_path in shared:
        canonical = canonical_path.read_text(encoding="utf-8")
        for hub in ("codex", "claude-code"):
            installed = (HUBS / hub / "skills" / canonical_path.parent.name / "SKILL.md").read_text(
                encoding="utf-8"
            )
            assert installed == canonical

    adaptive = (HUBS / "shared/skills/adaptive-orchestrate/SKILL.md").read_text(encoding="utf-8")
    assert 'agent_hub_plan`을\n   `mode="prepare"' in adaptive
    assert "agent_hub_start" in adaptive
    assert "agent_hub_continue" in adaptive
    assert "provider 순서나\n실행 스케줄을 복제하지 않고" in adaptive

    document = (HUBS / "shared/skills/document-write/SKILL.md").read_text(encoding="utf-8")
    assert 'agent_hub_execute(capability="write")' in document
    assert "orchestrate_codex.document_quality" in document
    assert "독백체" in document

    handoff = (HUBS / "shared/skills/handoff/SKILL.md").read_text(encoding="utf-8")
    takeover = (HUBS / "shared/skills/takeover/SKILL.md").read_text(encoding="utf-8")
    gpt = (HUBS / "shared/skills/gpt-provider/SKILL.md").read_text(encoding="utf-8")
    connect = (HUBS / "shared/skills/provider-connect/SKILL.md").read_text(encoding="utf-8")
    assert 'agent_hub_handoff(action="prepare_update")' in handoff
    assert "expected_sha256" in handoff
    assert "git add -A" not in handoff
    assert "handoff_drift" in takeover
    assert 'provider="gpt"' in gpt
    assert "`openai_codex_*`" in gpt
    assert "별도 MCP로 등록" in gpt
    assert "agent_hub_catalog" in connect
    assert "./.venv/bin/agent-hub-connect" in connect
    assert "consent 체크박스" in connect
    assert "공유 로그인을 Hub가 삭제하지 않는다" in connect

    route = (HUBS / "shared/skills/route-to/SKILL.md").read_text(encoding="utf-8")
    assert "agent_hub_execute" in route
    assert "agent_hub_plan" in route
    assert "placeholder" in route


def test_hub_plugins_register_only_unified_agent_hub(tmp_path):
    local_setup.apply_plan(local_setup.plan_setup(ROOT, target_root=tmp_path))
    codex_mcp = _json(tmp_path / "hubs/codex/.mcp.json")
    claude_mcp = _json(tmp_path / "hubs/claude-code/.mcp.json")["mcpServers"]
    assert set(codex_mcp) == {"agent-hub"}
    assert set(claude_mcp) == {"agent-hub"}
    assert codex_mcp["agent-hub"]["command"].endswith("/.venv/bin/agent-hub-mcp")
    assert claude_mcp["agent-hub"]["command"].endswith("/.venv/bin/agent-hub-mcp")


def test_public_hub_tools_do_not_expose_private_provider_leaf_names():
    from agent_hub.v2.tools import TOOL_NAMES

    names = set(TOOL_NAMES)
    assert len(names) == 14
    assert "agent_hub_execute" in names
    assert not any(name.startswith("openai_codex_") for name in names)

    tracked_configs = [
        ROOT / "hubs/codex/.codex-plugin/plugin.json",
        ROOT / "hubs/claude-code/.claude-plugin/plugin.json",
        ROOT / ".agents/plugins/marketplace.json",
        ROOT / ".claude-plugin/marketplace.json",
    ]
    assert all("openai_codex.mcp_server" not in path.read_text() for path in tracked_configs)


def test_plugin_manifests_and_claude_commands_describe_adaptive_engine():
    codex = _json(HUBS / "codex/.codex-plugin/plugin.json")
    claude = _json(HUBS / "claude-code/.claude-plugin/plugin.json")
    assert codex["version"] == "2.1.4"
    assert claude["version"] == "2.1.4"
    assert codex["mcpServers"] == "./.mcp.json"
    assert "durable" in codex["description"].lower()
    assert "durable" in claude["description"].lower()
    assert "GPT" in codex["interface"]["longDescription"]
    assert "egress manifest" in " ".join(codex["interface"]["defaultPrompt"])
    for name in ("agent-hub-plan.md", "agent-hub-run.md"):
        command = (HUBS / "claude-code/commands" / name).read_text(encoding="utf-8")
        assert "agent_hub_plan" in command
        assert "project_root" in command


def test_local_marketplaces_install_the_matching_app_plugin():
    codex = _json(ROOT / ".agents/plugins/marketplace.json")
    claude = _json(ROOT / ".claude-plugin/marketplace.json")
    assert codex["name"] == "agent-hub"
    assert codex["plugins"][0]["source"]["path"] == "./hubs/codex"
    assert claude["name"] == "agent-hub"
    assert claude["plugins"][0]["source"] == "./hubs/claude-code"
    assert codex["plugins"][0]["name"] == claude["plugins"][0]["name"] == "agent-hub"
    assert claude["plugins"][0]["version"] == "2.1.4"


def test_release_version_check_uses_unified_agent_hub_fields():
    module = runpy.run_path(str(ROOT / "scripts/check_release_version.py"))
    found = module["versions"]()
    assert set(found) == {
        "pyproject",
        "package",
        "codex_plugin",
        "claude_plugin",
        "claude_marketplace",
    }
    assert set(found.values()) == {"2.1.4"}


def test_package_exposes_only_canonical_agent_hub_entrypoints():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]

    assert scripts["agent-hub-mcp"] == "agent_hub.v2.bridge:serve"
    assert scripts["agent-hub"] == "agent_hub.v2.cli:main"
    assert scripts["agent-hubd"] == "agent_hub.v2.daemon:main"
    assert {
        "agent-hub-v1-mcp",
        "agent-hub-v2-mcp",
        "agent-hub-setup",
        "agent-hub-doctor",
    }.isdisjoint(scripts)
    assert importlib.util.find_spec("agent_hub.server") is None
    assert importlib.util.find_spec("agent_hub.v2.migrate") is None


def test_retired_standalone_bundle_fails_with_current_install_guidance(tmp_path):
    script = ROOT / "scripts" / "build_plugin_bundle.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--output",
            str(tmp_path / "bundle"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "standalone Antigravity plugin bundle was retired" in result.stderr
    assert "agent-hub setup" in result.stderr
    assert not (tmp_path / "bundle").exists()


def test_hub_readmes_describe_the_same_four_provider_surface():
    for hub in ("codex", "claude-code"):
        text = (HUBS / hub / "README.md").read_text(encoding="utf-8")
        assert "Claude, Grok, Gemini, GPT" in text
        assert "`agent_hub_*` 14개" in text
        assert "`openai_codex_*`" in text
        assert "gpt-provider/SKILL.md" in text
        assert "/Users/" not in text
        route = (HUBS / hub / "skills/route-to/SKILL.md").read_text(encoding="utf-8")
        assert "Claude/Grok/Gemini/GPT" in route

    provenance = _json(ROOT / "model-access/leaves.manifest.json")
    gpt = next(item for item in provenance["packages"] if item["name"] == "openai-codex")
    assert gpt["role"].startswith("provider adapter")
    assert "private leaf surface" in gpt["role"]
