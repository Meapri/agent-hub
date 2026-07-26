from __future__ import annotations

from orchestrate_codex import gather, verify


def test_gather_detects_compact_v2_tools_and_shared_skills(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "2.0.1"\n\n'
        '[project.scripts]\nagent-hub-mcp = "agent_hub.v2.bridge:serve"\n',
        encoding="utf-8",
    )
    tools = tmp_path / "src" / "agent_hub" / "v2" / "tools.py"
    tools.parent.mkdir(parents=True)
    tools.write_text(
        'TOOL_NAMES = ("agent_hub_status", "agent_hub_execute")\n',
        encoding="utf-8",
    )
    skill = tmp_path / "hubs" / "shared" / "skills" / "adaptive-orchestrate"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Adaptive\n", encoding="utf-8")

    facts = gather.gather_durable_facts(tmp_path)

    assert facts["version"] == "2.0.1"
    assert facts["mcp_tools_detected"] == [
        "agent_hub_execute",
        "agent_hub_status",
    ]
    assert facts["skills"] == ["adaptive-orchestrate"]
    assert facts["console_scripts"] == ["agent-hub-mcp"]


def test_verify_rejects_placeholders_and_unknown_repository_paths(tmp_path):
    source = tmp_path / "README.md"
    source.write_text("# Fixture\n", encoding="utf-8")
    facts = gather.gather_durable_facts(tmp_path)

    result = verify.verify_text(
        "See `missing/module.py`.\n\nTODO: finish this.",
        doc_class="durable",
        fact_pack=facts,
        user_facing=True,
    )

    assert result["ok"] is False
    assert any(
        warning.startswith("placeholder_in_final_document:") for warning in result["warnings"]
    )
    assert "repository_path_not_found:missing/module.py" in result["warnings"]


def test_verify_accepts_private_provider_tool_names():
    result = verify.verify_text(
        "The worker invokes `openai_codex_chat` behind the V2 boundary.",
        doc_class="durable",
        fact_pack={
            "repository_manifest_complete": True,
            "repository_files": ["README.md"],
            "mcp_tools_detected": [],
            "cli_commands": [],
            "packages": [],
        },
    )

    assert result["ok"] is True


def test_verify_module_cli_checks_user_facing_documents(tmp_path, capsys):
    document = tmp_path / "README.md"
    document.write_text("# Fixture\n\n설명입니다.\n", encoding="utf-8")

    assert verify.main(["--user-facing", str(document)]) == 0
    assert "verify ok" in capsys.readouterr().out

    document.write_text("# Fixture\n\n[TODO: 내용을 추가하세요]\n", encoding="utf-8")
    assert verify.main(["--user-facing", str(document)]) == 1
    assert "placeholder_in_final_document" in capsys.readouterr().out
