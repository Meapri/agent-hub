#!/usr/bin/env python3
"""SuperGrok / xAI device-code OAuth login."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from grok_codex import auth, oauth_login  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    s = sub.add_parser("start")
    s.add_argument("--no-browser", action="store_true")
    sub.add_parser("complete")
    i = sub.add_parser("interactive")
    i.add_argument("--no-browser", action="store_true")
    sub.add_parser("logout")
    args = p.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps({"oauth": oauth_login.status(), "auth": auth.status()}, indent=2))
        return 0
    if args.cmd == "start":
        print(json.dumps(oauth_login.start_login(open_browser=not args.no_browser), indent=2))
        return 0
    if args.cmd == "complete":
        print(json.dumps(oauth_login.complete_login(), indent=2))
        return 0
    if args.cmd == "interactive":
        print(json.dumps(oauth_login.interactive_login(open_browser=not args.no_browser), indent=2))
        return 0
    if args.cmd == "logout":
        print(json.dumps({"removed": oauth_login.clear_tokens()}, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
