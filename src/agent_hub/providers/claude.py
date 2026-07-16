"""Claude provider adapter (raw-leaf: tools/call result is the dispatch payload)."""

from __future__ import annotations

from claude_codex import mcp_server as _claude

from .base import RawLeafProvider

claude_provider = RawLeafProvider("claude", _claude)
