"""Shared high-capacity defaults for provider and workflow execution."""

from __future__ import annotations


# Unified schemas expose the largest currently accepted provider request.
# Provider adapters clamp to their exact model/API limits.
MAX_OUTPUT_TOKENS = 131_072
CLAUDE_MAX_OUTPUT_TOKENS = 128_000
GEMINI_MAX_OUTPUT_TOKENS = 65_536

# MCP hosts give a single tool call 30 minutes. Adaptive calls reserve a small
# return margin so they can persist state and return a structured result.
MAX_PROVIDER_TIMEOUT_SECONDS = 1_800
MCP_RETURN_MARGIN_SECONDS = 10
MAX_ADAPTIVE_TIMEOUT_SECONDS = MAX_PROVIDER_TIMEOUT_SECONDS - MCP_RETURN_MARGIN_SECONDS

# These are the existing public schema maxima. Keeping them here prevents
# direct leaves and unified workflows from drifting back to smaller defaults.
MAX_PROVIDER_RETRIES = 5
MAX_SEARCH_SOURCES = 10
MAX_LEAF_CALLS = 100
MAX_WAVES_PER_CALL = 8
MAX_PLANNER_REPAIRS = 5
MAX_ADAPTIVE_RESULT_CHARS = 2_000_000
