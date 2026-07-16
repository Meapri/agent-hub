from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from google_antigravity_codex import release, writing


def test_writing_routes_to_antigravity_chat():
    seen = {}

    def fake_run_chat(arguments):
        seen.update(arguments)
        return {"text": "Polished text", "usage": {"total_tokens": 3}}

    with patch.object(writing.chat, "run_chat", fake_run_chat):
        result = writing.run_writing(
            {
                "task": "polish",
                "profile": ["chanwoo-ko"],
                "instruction": "Make it natural.",
                "source_text": "rough text",
                "model": "gemini-3.1-pro-preview",
            }
        )

    assert result["text"] == "Polished text"
    assert result["task"] == "polish"
    assert result["profiles"] == ["chanwoo-ko"]
    assert seen["model"] == "gemini-3.1-pro-preview"
    assert "rough text" in seen["prompt"]


def test_durable_readme_forces_git_off_and_injects_fact_pack(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ALLOWED_ROOTS", str(tmp_path))
    (tmp_path / "pyproject.toml").write_text('version = "7.7.7"\n', encoding="utf-8")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "demo").mkdir()
    seen = {}

    def fake_run_chat(arguments):
        seen.update(arguments)
        return {"text": "# Product\nSetup and usage.", "usage": {"total_tokens": 2}}

    with patch.object(writing.chat, "run_chat", fake_run_chat):
        result = writing.run_writing(
            {
                "task": "readme",
                "instruction": "Write a short README",
                "project_root": str(tmp_path),
                "project_context": "git-summary",
                "model": "gemini-3.1-pro-preview",
            }
        )

    assert result["doc_class"] == "durable"
    assert result["fact_pack_used"] is True
    assert result["project_context_used"] is False
    assert "DURABLE FACT PACK" in seen["prompt"]
    assert "7.7.7" in seen["prompt"]
    assert "Recent commits" not in seen["prompt"]
    assert "durable" in seen["system"].lower()


def test_durable_review_flags_recency():
    warnings = writing.review_text("today we fixed HTTP 400 in this session", durable=True)
    assert "durable_output_contains_recency_language" in warnings


def test_release_snapshot_and_draft_from_git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ALLOWED_ROOTS", str(tmp_path))
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    subprocess.run(["git", "add", "pyproject.toml"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: initial release helper"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    snapshot_result = release.release_snapshot({"repo": str(tmp_path)})
    draft_result = release.release_draft({"repo": str(tmp_path), "version": "1.2.4", "tag": "v1.2.4"})

    snapshot = snapshot_result["snapshot"]
    assert snapshot["recommended_bump"] == "minor"
    assert "README.md" in snapshot["changed_files"]
    assert "Release Copilot Draft" in draft_result["text"]
    assert "v1.2.4" in draft_result["text"]


def test_release_repo_parameter_is_an_explicit_workspace_root(tmp_path, monkeypatch):
    repo = tmp_path / "outside-server-cwd"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ALLOWED_ROOTS", "")

    snapshot = release.release_snapshot({"repo": str(repo)})["snapshot"]

    assert snapshot["repo_root"] == str(repo.resolve())


def test_writing_workspace_root_authorizes_source_file(tmp_path, monkeypatch):
    workspace = tmp_path / "outside-server-cwd"
    workspace.mkdir()
    source = workspace / "note.md"
    source.write_text("hello", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ALLOWED_ROOTS", "")

    assert writing.read_source(
        {"source_file": str(source), "workspace_root": str(workspace)}
    ) == "hello"


def test_writing_blocks_sensitive_and_outside_source_files(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = allowed / ".env"
    secret.write_text("API_KEY=secret\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ALLOWED_ROOTS", str(allowed))

    with pytest.raises(ValueError, match="sensitive"):
        writing.read_source({"source_file": str(secret)})
    with pytest.raises(ValueError, match="allowed roots"):
        writing.read_source({"source_file": str(outside)})


def test_release_check_commands_are_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ALLOWED_ROOTS", str(tmp_path))
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)

    with pytest.raises(ValueError, match="disabled"):
        release.collect_snapshot({"repo": str(tmp_path), "check_commands": ["echo unsafe"]})


def test_remote_url_strips_embedded_credentials():
    assert (
        release.normalize_remote_url("https://user:secret@example.com/owner/repo.git?token=x")
        == "https://example.com/owner/repo"
    )
