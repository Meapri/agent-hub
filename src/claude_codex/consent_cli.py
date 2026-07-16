"""CLI for granting/revoking user consent."""

from __future__ import annotations

import argparse
import json
import sys

from . import security


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude_codex_consent.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    grant = sub.add_parser("grant", help="Grant local consent")
    grant.add_argument("--i-understand-and-consent", action="store_true", required=True)
    sub.add_parser("revoke", help="Revoke local consent")
    sub.add_parser("status", help="Show consent status")
    args = parser.parse_args(argv)
    if args.cmd == "grant":
        path = security.grant_consent()
        print(json.dumps({"ok": True, "consent_file": str(path)}, indent=2))
        return 0
    if args.cmd == "revoke":
        removed = security.revoke_consent()
        print(json.dumps({"ok": True, "removed": removed}, indent=2))
        return 0
    print(json.dumps(security.consent_status(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
