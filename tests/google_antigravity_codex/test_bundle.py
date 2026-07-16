from __future__ import annotations
import pytest
pytestmark = pytest.mark.skip(reason="standalone plugin bundle/release-version consistency is not used in the agent-hub monorepo; distribution + versioning are managed at the monorepo level")


import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent


def test_bundle_is_allowlisted_and_platform_aware(tmp_path):
    posix = tmp_path / "posix bundle with spaces"
    windows = tmp_path / "windows"
    script = ROOT / "scripts" / "build_plugin_bundle.py"

    subprocess.run(
        [sys.executable, str(script), "--output", str(posix), "--platform", "posix"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [sys.executable, str(script), "--output", str(windows), "--platform", "windows"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    relative_files = {path.relative_to(posix).as_posix() for path in posix.rglob("*") if path.is_file()}
    assert "plugin.json" in relative_files
    assert "google_antigravity_codex/mcp_server.py" in relative_files
    assert "google_antigravity_codex/google_api.py" not in relative_files
    assert "google_antigravity_codex/auth_cli.py" not in relative_files
    assert "scripts/google_antigravity_auth.py" not in relative_files
    assert not any(part in {".git", ".pytest_cache", ".ruff_cache", "tests", "build"} for path in relative_files for part in Path(path).parts)

    posix_config = json.loads((posix / "mcp_config.json").read_text(encoding="utf-8"))
    windows_config = json.loads((windows / "mcp_config.json").read_text(encoding="utf-8"))
    posix_codex_config = json.loads((posix / ".mcp.json").read_text(encoding="utf-8"))
    windows_codex_config = json.loads((windows / ".mcp.json").read_text(encoding="utf-8"))
    assert posix_config["mcpServers"]["google-antigravity-codex"]["command"] == "python3"
    assert windows_config["mcpServers"]["google-antigravity-codex"]["command"] == "py"
    assert windows_config["mcpServers"]["google-antigravity-codex"]["args"][0] == "-3"
    assert posix_codex_config["mcpServers"]["google-antigravity-codex"]["cwd"] == "."
    assert windows_codex_config["mcpServers"]["google-antigravity-codex"]["cwd"] == "."
    assert (
        posix_codex_config["mcpServers"]["google-antigravity-codex"]["env"]
        ["GOOGLE_ANTIGRAVITY_RUNNING_UNDER_CODEX_MCP"]
        == "1"
    )
    manifest = json.loads((posix / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert len(manifest["interface"]["defaultPrompt"]) <= 3
    server = posix_config["mcpServers"]["google-antigravity-codex"]
    assert server["env"]["GOOGLE_ANTIGRAVITY_RUNNING_UNDER_AGY"] == "1"
    assert "google_antigravity_chat" in server["disabledTools"]
    assert "google_grounded_search" in server["disabledTools"]
    assert "google_antigravity_generate_image" in server["disabledTools"]
    assert "google_antigravity_write" in server["disabledTools"]

    requests = [
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "server/discover",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {"name": "bundle-test", "version": "1"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        },
    ]
    process = subprocess.run(
        [sys.executable, "./scripts/google_antigravity_mcp.py"],
        cwd=posix,
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    assert process.returncode == 0
    assert process.stderr == ""
    assert responses[0]["result"]["protocolVersion"] == "2025-11-25"
    assert responses[1]["result"]["resultType"] == "complete"
