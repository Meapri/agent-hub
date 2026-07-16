#!/usr/bin/env python3
"""Live verification for plugin OAuth: models, grounded search, image gen.

Requires prior consent + successful plugin login (oauth-token.json).
Does not use agy CLI sessions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google_antigravity_codex import grounding, image, models, oauth_login, provider, security  # noqa: E402


def main() -> int:
    report: dict = {
        "consent": security.user_consent_enabled(),
        "login": oauth_login.login_status(),
        "provider": None,
        "list_models": None,
        "grounded_search": None,
        "generate_image": None,
    }
    ok = True
    try:
        report["provider"] = provider.status(probe=True)
    except Exception as exc:
        report["provider"] = {"error": str(exc), "error_type": getattr(exc, "code", type(exc).__name__)}
        ok = False

    if not report["login"].get("success"):
        report["error"] = (
            "Plugin OAuth login required. Run:\n"
            "  python3 scripts/google_antigravity_consent.py grant --i-understand-and-consent\n"
            "  python3 scripts/google_antigravity_login.py interactive"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    try:
        catalog = models.list_models({})
        report["list_models"] = {
            "success": True,
            "source": catalog.get("source"),
            "text_count": len(catalog.get("text_models") or []),
            "image_count": len(catalog.get("image_models") or []),
            "sample": [m.get("id") for m in (catalog.get("text_models") or [])[:5]],
        }
    except Exception as exc:
        ok = False
        report["list_models"] = {"success": False, "error": str(exc), "error_type": getattr(exc, "code", type(exc).__name__)}

    try:
        search = grounding.run_grounded_search(
            {
                "query": "What is the current year according to Google Search? One short sentence with a source.",
                "max_sources": 3,
                "resolve_sources": True,
                "timeout_sec": 90,
                "retry_count": 0,
            }
        )
        report["grounded_search"] = {
            "success": bool(search.get("success")),
            "answer_preview": str(search.get("answer") or search.get("text") or "")[:400],
            "source_count": len(search.get("sources") or []),
            "sources": [
                {"url": s.get("resolved_url") or s.get("url"), "title": s.get("title")}
                for s in (search.get("sources") or [])[:3]
            ],
            "backend": search.get("backend"),
            "warnings": search.get("warnings"),
        }
        if not report["grounded_search"]["success"]:
            ok = False
    except Exception as exc:
        ok = False
        report["grounded_search"] = {
            "success": False,
            "error": str(exc),
            "error_type": getattr(exc, "code", type(exc).__name__),
        }

    try:
        img = image.generate_image(
            {
                "prompt": "A simple flat icon of a blue circle on white background, minimal",
                "aspect_ratio": "square",
                "timeout_sec": 120,
                "retry_count": 0,
            }
        )
        report["generate_image"] = {
            "success": bool(img.get("success")),
            "path": img.get("path") or img.get("image") or img.get("file"),
            "model": img.get("model"),
            "mime_type": img.get("mime_type"),
            "bytes": img.get("bytes") or img.get("size"),
            "backend": img.get("backend"),
            "warnings": img.get("warnings"),
            "error": img.get("error"),
        }
        path = report["generate_image"].get("path")
        if path and Path(str(path)).is_file():
            report["generate_image"]["file_exists"] = True
            report["generate_image"]["file_size"] = Path(str(path)).stat().st_size
        if not report["generate_image"]["success"]:
            ok = False
    except Exception as exc:
        ok = False
        report["generate_image"] = {
            "success": False,
            "error": str(exc),
            "error_type": getattr(exc, "code", type(exc).__name__),
        }

    report["ok"] = ok
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
