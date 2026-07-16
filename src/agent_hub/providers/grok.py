"""Grok provider adapter (raw-leaf: tools/call result is the dispatch payload)."""

from __future__ import annotations

from grok_codex import mcp_server as _grok

from .base import RawLeafProvider

grok_provider = RawLeafProvider("grok", _grok)
