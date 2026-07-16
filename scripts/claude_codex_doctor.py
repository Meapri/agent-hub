#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from claude_codex.mcp_server import dispatch_tool  # noqa: E402


if __name__ == "__main__":
    print(json.dumps(dispatch_tool("claude_codex_doctor", {}), indent=2, ensure_ascii=False))
