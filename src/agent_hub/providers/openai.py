"""GPT provider adapter (private leaf, public access stays in Agent Hub)."""

from __future__ import annotations

from openai_codex import mcp_server as _openai

from .base import RawLeafProvider

gpt_provider = RawLeafProvider("gpt", _openai)
