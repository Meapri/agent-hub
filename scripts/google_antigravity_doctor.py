#!/usr/bin/env python3
"""Print a secret-safe report for consent, plugin OAuth, and provider readiness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google_antigravity_codex import agy_auth, chat, oauth_login, provider, security  # noqa: E402


def live_probe() -> dict[str, object]:
    try:
        result = chat.run_chat(
            {
                "prompt": "Reply with exactly ANTIGRAVITY_DOCTOR_OK.",
                "model": "gemini-3.5-flash",
                "temperature": 0,
                "max_tokens": 32,
                "timeout_sec": 60,
                "retry_count": 0,
            }
        )
    except Exception as exc:
        return {
            "requested": True,
            "success": False,
            "error_type": getattr(exc, "code", type(exc).__name__),
            "error": str(exc),
        }
    text = str(result.get("text") or "")
    return {
        "requested": True,
        "success": result.get("success") is True or bool(text),
        "backend": result.get("backend"),
        "model": result.get("model"),
        "expected_reply": "ANTIGRAVITY_DOCTOR_OK" in text,
        "text_preview": text[:120],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument(
        "--live",
        action="store_true",
        help="send one short model request via plugin OAuth",
    )
    args = parser.parse_args()
    provider_report = dict(provider.status(probe=True))
    login_report = oauth_login.login_status()
    auth_report = agy_auth.status(probe=False)
    live = live_probe() if args.live else {"requested": False, "success": None}
    provider_ready = (
        provider_report.get("configured") is True
        and provider_report.get("healthy") is True
    )
    login_ready = bool(login_report.get("success"))
    live_ready = not args.live or bool(live.get("success"))
    if args.live:
        provider_report["live_prompt_verified"] = live_ready
    report = {
        "success": provider_ready and live_ready,
        "text": (
            f"provider {'ready' if provider_ready else 'not ready'}; "
            f"plugin OAuth login {'ready' if login_ready else 'not ready'}"
            f"{'; live prompt ok' if args.live and live_ready else ''}"
            f"{'; live prompt failed' if args.live and not live_ready else ''}"
        ),
        "consent": security.consent_status(),
        "provider": provider_report,
        "agy_auth": auth_report,
        "direct_login": login_report,
        "live": live,
        "auth_mode": "plugin_oauth_only",
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report["text"])
    return 0 if report.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
