#!/usr/bin/env python3
"""Interactive Google Antigravity OAuth login (PKCE) for the Codex plugin."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google_antigravity_codex.oauth_login import (  # noqa: E402
    OAuthLoginError,
    complete_login,
    login_status,
    run_interactive_login,
    start_login,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    interactive = sub.add_parser("interactive", help="Full browser + paste/local-callback login")
    interactive.add_argument("--no-browser", action="store_true")
    interactive.add_argument("--external-redirect", action="store_true")

    start = sub.add_parser("start", help="Print auth URL for later complete")
    start.add_argument("--external-redirect", action="store_true")

    complete = sub.add_parser("complete", help="Finish login with redirect URL or code")
    complete.add_argument("code_or_url")

    sub.add_parser("status", help="Show direct OAuth token status (no secrets)")

    args = parser.parse_args(argv)
    command = args.command or "interactive"

    try:
        if command == "status":
            state = login_status()
            print(state["text"])
            print(f"token_file={state['token_file']}")
            print(f"present={state['token_file_present']} readable={state['credentials_readable']}")
            return 0 if state.get("success") else 1
        if command == "start":
            result = start_login(use_local_redirect=not args.external_redirect)
            print(result["auth_url"])
            print("\nThen run: python3 scripts/google_antigravity_login.py complete '<redirect-url-or-code>'")
            return 0
        if command == "complete":
            result = complete_login(args.code_or_url)
            print(result["text"])
            print(f"token_file={result['token_file']}")
            return 0
        # interactive (default)
        if command == "interactive":
            result = run_interactive_login(
                use_local_server=not args.external_redirect,
                open_browser=not args.no_browser,
            )
            print(f"token_file={result['token_file']}")
            return 0
    except OAuthLoginError as exc:
        print(f"ERROR ({exc.code}): {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
