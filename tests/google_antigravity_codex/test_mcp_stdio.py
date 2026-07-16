from __future__ import annotations

import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(os.name != "posix", reason="persistent pipe regression uses POSIX select")
def test_persistent_mcp_stdin_is_not_inherited_by_agy_models(tmp_path):
    fake_agy = tmp_path / "agy"
    fake_agy.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  echo 1.1.2\n"
        "elif [ \"$1\" = \"models\" ]; then\n"
        "  IFS= read -r ignored || true\n"
        "  echo 'Gemini 3.5 Flash (Low)'\n"
        "else\n"
        "  exit 1\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_agy.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    env = os.environ.copy()
    env["GOOGLE_ANTIGRAVITY_CLI"] = str(fake_agy)
    env["GOOGLE_ANTIGRAVITY_RUNNING_UNDER_CODEX_MCP"] = "1"
    process = subprocess.Popen(
        [sys.executable, "./scripts/google_antigravity_mcp.py"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "google_antigravity_list_models", "arguments": {}},
        }
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "persistent MCP request blocked while agy inherited the open stdin pipe"
        response = json.loads(process.stdout.readline())
    finally:
        process.terminate()
        process.wait(timeout=5)

    result = response["result"]["structuredContent"]
    assert result["success"] is True
    # Without OAuth, catalog is static fallback (no agy-cli session pull).
    assert result["source"] in {"static_fallback", "agy-oauth"}
    assert result["text_models"]
