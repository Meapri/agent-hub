#!/usr/bin/env python3
"""Entrypoint for the Claude Codex MCP stdio server."""

from __future__ import annotations

from pathlib import Path
import sys

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from claude_codex.mcp_server import serve  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(serve())
