#!/usr/bin/env python3
"""A tiny stand-in leaf MCP server for broker tests.

Speaks the same line-delimited JSON-RPC the real leaves use. Behaviour is driven
by env vars so tests can simulate success, tool-level failure, or a crash:

  MOCK_LEAF_TOOL   : tool name to advertise (default "mock_chat")
  MOCK_LEAF_FAIL   : if "1", every tools/call returns isError with MOCK_LEAF_MSG
  MOCK_LEAF_MSG    : error message text (default "mock failure")
  MOCK_LEAF_ECHO   : if "1", echo the arguments back in the text (default on)
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    tool = os.environ.get("MOCK_LEAF_TOOL", "mock_chat")
    fail = os.environ.get("MOCK_LEAF_FAIL") == "1"
    msg = os.environ.get("MOCK_LEAF_MSG", "mock failure")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        method = req.get("method")
        if rid is None:
            continue  # notification
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-leaf", "version": "0.0.1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": tool, "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            args = (req.get("params") or {}).get("arguments") or {}
            if fail:
                result = {"content": [{"type": "text", "text": msg}], "isError": True}
            elif os.environ.get("MOCK_LEAF_SOFT") == "1":
                # a backend error handed back as "successful" text (e.g. Grok HTTP 503)
                result = {"content": [{"type": "text", "text": msg}], "isError": False}
            else:
                text = f"MOCK[{tool}] " + json.dumps(args, ensure_ascii=False)
                result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
