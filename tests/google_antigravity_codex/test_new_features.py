from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from google_antigravity_codex import (
    account,
    compare,
    diff_review,
    mcp_server,
    profiles,
    session_prefs,
)


def test_session_provider_pref(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_PROVIDER", raising=False)
    session_prefs.set_provider("agy-oauth")
    assert session_prefs.preferred_provider() == "agy-oauth"
    session_prefs.clear_provider()
    assert session_prefs.preferred_provider() is None


def test_use_profile_coding(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    result = profiles.use_profile_tool({"name": "coding", "apply_provider": False})
    assert result["success"] is True
    assert result["profile"]["name"] == "coding"
    assert session_prefs.load()["active_profile"] == "coding"
    listed = profiles.list_profiles_tool({})
    assert any(p["name"] == "coding" and p["active"] for p in listed["profiles"])


def test_logout_removes_token_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    token = tmp_path / "oauth-token.json"
    token.write_text(json.dumps({"access": "secret", "refresh": "r", "expires": 9e12}), encoding="utf-8")
    token.chmod(0o600)
    with patch.object(account.agy_auth, "token_file_path", return_value=token), patch.object(
        account.oauth_login, "token_file_path", return_value=token
    ), patch.object(account.oauth_login, "pending_file_path", return_value=tmp_path / "pending.json"):
        result = account.logout({})
    assert result["success"] is True
    assert not token.exists()
    assert "secret" not in json.dumps(result)


def test_whoami_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_USER_CONSENT", "1")
    missing = tmp_path / "missing-token.json"
    with patch.object(account.agy_auth, "token_file_path", return_value=missing):
        result = account.whoami({})
    assert result["token_file_present"] is False
    assert result["success"] is False


def test_compare_models_two_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))

    def fake_chat(args, **kwargs):
        return {
            "text": f"answer-from-{args['model']}",
            "success": True,
            "usage": {"total_tokens": 1},
            "backend": "test",
            "diagnostics": {},
            "warnings": [],
        }

    with patch.object(compare.chat, "run_chat", side_effect=fake_chat):
        result = compare.compare_models(
            {"prompt": "hi", "models": ["gemini-3.5-flash-high", "gemini-3.1-pro-high"]}
        )
    assert result["success"] is True
    assert len(result["results"]) == 2
    assert result["results"][0]["text"].startswith("answer-from-")


def test_review_diff_on_temp_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ALLOWED_ROOTS", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    f = repo / "a.py"
    f.write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    f.write_text("print(2)\n", encoding="utf-8")

    def fake_chat(args, **kwargs):
        assert "print(2)" in args["prompt"] or "diff" in args["prompt"].lower()
        return {
            "text": "No critical issues.",
            "success": True,
            "usage": {},
            "backend": "test",
            "diagnostics": {"capacity_fallback": False},
            "warnings": [],
        }

    with patch.object(diff_review.chat, "run_chat", side_effect=fake_chat), patch.object(
        diff_review.security, "explicit_workspace_root", return_value=repo
    ):
        result = diff_review.review_diff({"cwd": str(repo)})
    assert result["success"] is True
    assert "No critical issues" in result["text"]
    assert result["diff_chars"] > 0


def test_new_mcp_tools_registered():
    names = {t["name"] for t in mcp_server.tool_definitions()}
    for name in (
        "google_antigravity_set_provider",
        "google_antigravity_list_profiles",
        "google_antigravity_use_profile",
        "google_antigravity_whoami",
        "google_antigravity_logout",
        "google_antigravity_compare_models",
        "google_antigravity_review_diff",
        "google_antigravity_save_profile",
        "google_antigravity_get_session_prefs",
    ):
        assert name in names
