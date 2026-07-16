"""Leaf launch registry: how the autonomous broker spawns each leaf MCP server.

The broker runs in its own process and has no access to Codex's MCP config, so
launch commands must be provided explicitly. Config is JSON, from env
`ORCHESTRATE_CODEX_LEAVES` or `~/.orchestrate_codex/leaves.json`:

    {
      "claude_codex_chat": {"command": "python3", "args": ["/path/claude_codex_mcp.py"]},
      "google_antigravity": {"command": "python3", "args": ["/path/agy_mcp.py"], "cwd": "/path"}
    }

Keys may be an exact leaf tool name or a provider prefix (e.g. "google_antigravity"
matches every google_antigravity_* tool). Exact match wins; otherwise the longest
matching prefix is used.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

_ENV = "ORCHESTRATE_CODEX_LEAVES"


def _config_file() -> Path:
    override = os.environ.get(_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".orchestrate_codex" / "leaves.json"


def load_leaves() -> Dict[str, Dict[str, Any]]:
    try:
        raw = json.loads(_config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, spec in raw.items():
        if isinstance(spec, dict) and spec.get("command"):
            out[str(key)] = spec
    return out


def resolve_launch(tool: str, leaves: Optional[Dict[str, Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    reg = load_leaves() if leaves is None else leaves
    if tool in reg:
        return reg[tool]
    # longest provider-prefix match (e.g. "google_antigravity" for *_chat/_write/…)
    candidates = [k for k in reg if tool.startswith(k)]
    if candidates:
        return reg[max(candidates, key=len)]
    return None


def configured() -> bool:
    return bool(load_leaves())
