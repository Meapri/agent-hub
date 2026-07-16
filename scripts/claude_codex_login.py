#!/usr/bin/env python3
"""Claude subscription login helpers (Claude Code OAuth)."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from claude_codex import subscription_auth, auth  # noqa: E402

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Claude Codex subscription auth")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("mirror-keychain")
    sub.add_parser("refresh")
    sub.add_parser("instructions")
    args = p.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps({"subscription": subscription_auth.status(), "auth": auth.status()}, indent=2))
        return 0
    if args.cmd == "mirror-keychain":
        print(json.dumps(subscription_auth.mirror_keychain_to_file(), indent=2))
        return 0
    if args.cmd == "refresh":
        creds = subscription_auth.read_credentials()
        if not creds or not creds.get("refreshToken"):
            print(json.dumps({"success": False, "error": "no refresh token"})); return 1
        r = subscription_auth.refresh_token_pure(creds["refreshToken"])
        subscription_auth.write_credentials(r["access_token"], r["refresh_token"], r["expires_at_ms"])
        print(json.dumps({"success": True, "expires_at_ms": r["expires_at_ms"]}, indent=2))
        return 0
    print("""Claude subscription login (plan quota via Claude Code OAuth)

1) Install/login Claude Code CLI:
     claude auth login --claudeai

2) On macOS, mirror Keychain → file (optional but recommended):
     python3 scripts/claude_codex_login.py mirror-keychain

3) Grant plugin consent:
     python3 scripts/claude_codex_consent.py grant --i-understand-and-consent

4) Check:
     python3 scripts/claude_codex_login.py status
     python3 scripts/claude_codex_doctor.py

Chat requests use subscription OAuth by default and apply the hermes-claude-auth
plan-lane fingerprint. Force API key: CLAUDE_CODEX_AUTH_MODE=api_key
""")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
