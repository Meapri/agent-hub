from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"

TRANSLATION_LIKE_PHRASES = (
    "이전 이름은 지원하지 않습니다",
    "정상입니다",
    "끝난 것입니다",
    "별개로 보면 됩니다",
    "실패하는 것이 정상입니다",
    "호출 예산",
    "정본",
    "이를 통해",
    "활용할 수 있습니다",
    "dependency frontier",
)

REQUIRED_COMMANDS = (
    "./.venv/bin/pip install -e '.[dev]'",
    "./.venv/bin/claude-codex-consent grant --i-understand-and-consent",
    "./.venv/bin/grok-codex-consent grant --i-understand-and-consent",
    "./.venv/bin/google-antigravity-consent grant --i-understand-and-consent",
    "claude auth login --claudeai",
    "scripts/grok_codex_login.py interactive",
    "scripts/google_antigravity_login.py interactive",
    "codex plugin add agent-hub@agent-hub",
    "claude plugin install agent-hub@agent-hub --scope user",
    "./.venv/bin/ruff check src tests",
    "./.venv/bin/python -m build",
)


def test_readme_avoids_translation_like_copy() -> None:
    text = README.read_text(encoding="utf-8")

    for phrase in TRANSLATION_LIKE_PHRASES:
        assert phrase not in text

    placeholders = set(re.findall(r"<([A-Z][A-Z0-9_]*)>", text))
    assert placeholders <= {"REPO_ROOT"}


def test_readme_keeps_copyable_setup_commands() -> None:
    text = README.read_text(encoding="utf-8")

    for command in REQUIRED_COMMANDS:
        assert command in text
