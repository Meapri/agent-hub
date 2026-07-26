from __future__ import annotations

import re
from pathlib import Path

from agent_hub.v2.tools import tool_definitions
from orchestrate_codex.document_quality import review_natural_korean


ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"

REQUIRED_COMMANDS = (
    "./.venv/bin/pip install -e '.[dev]'",
    "./.venv/bin/claude-codex-consent grant --i-understand-and-consent",
    "./.venv/bin/grok-codex-consent grant --i-understand-and-consent",
    "./.venv/bin/google-antigravity-consent grant --i-understand-and-consent",
    "./.venv/bin/openai-codex-consent grant --i-understand-and-consent",
    "./.venv/bin/agent-hub setup --repo-root . --json",
    "./.venv/bin/agent-hub-setup --apply",
    "claude auth login --claudeai",
    "codex login",
    "codex login --device-auth",
    "scripts/grok_codex_login.py interactive",
    "scripts/google_antigravity_login.py interactive",
    "codex plugin add agent-hub@agent-hub",
    "claude plugin install agent-hub@agent-hub --scope user",
    "./.venv/bin/ruff check src tests",
    "./.venv/bin/python -m build",
)


def test_readme_avoids_translation_like_copy() -> None:
    text = README.read_text(encoding="utf-8")
    assert review_natural_korean(text) == []

    placeholders = set(re.findall(r"<([A-Z][A-Z0-9_]*)>", text))
    assert placeholders <= {"REPO_ROOT"}


def test_readme_keeps_copyable_setup_commands() -> None:
    text = README.read_text(encoding="utf-8")

    for command in REQUIRED_COMMANDS:
        assert command in text
    assert "Claude, Grok, Gemini, GPT" in text
    assert "공개 MCP 도구 14개" in text
    assert "/Users/" not in text

    tools_section = text.split("## 공개 MCP 도구 14개", 1)[1].split("\n## ", 1)[0]
    for tool in tool_definitions():
        assert f"`{tool['name']}`" in tools_section


def test_document_quality_check_catches_process_narration() -> None:
    warnings = review_natural_korean("먼저 저장소 구조를 살펴보겠습니다.")
    assert any("process_narration" in warning for warning in warnings)
