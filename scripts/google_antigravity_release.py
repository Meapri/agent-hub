#!/usr/bin/env python3
"""CLI wrapper for integrated Antigravity release copilot helpers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from google_antigravity_codex import release  # noqa: E402


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base-ref", default="")
    parser.add_argument("--head-ref", default="HEAD")
    parser.add_argument("--check-command", action="append", default=[])
    parser.add_argument("--check-timeout-sec", type=int, default=600)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect release context and draft release prose.")
    sub = parser.add_subparsers(dest="command", required=True)
    snapshot = sub.add_parser("snapshot")
    add_common(snapshot)
    snapshot.add_argument("--format", choices=("text", "json"), default="text")

    draft = sub.add_parser("draft")
    add_common(draft)
    draft.add_argument("--title", default="")
    draft.add_argument("--version", default="")
    draft.add_argument("--tag", default="")
    draft.add_argument("--polish", action="store_true")
    draft.add_argument("--model", default="")
    draft.add_argument("--max-tokens", type=int, default=4096)
    draft.add_argument("--timeout-sec", type=int, default=180)
    draft.add_argument("--json", action="store_true")
    return parser.parse_args()


def common_args(args: argparse.Namespace) -> dict:
    return {
        "repo": args.repo,
        "base_ref": args.base_ref,
        "head_ref": args.head_ref,
        "check_commands": args.check_command,
        "check_timeout_sec": args.check_timeout_sec,
    }


def main() -> int:
    args = parse_args()
    payload = common_args(args)
    if args.command == "snapshot":
        result = release.release_snapshot(payload)
        if args.format == "json":
            print(json.dumps(result["snapshot"], ensure_ascii=False, indent=2))
        else:
            print(result["text"])
        return 0

    payload.update(
        {
            "title": args.title,
            "version": args.version,
            "tag": args.tag,
            "polish": args.polish,
            "model": args.model,
            "max_tokens": args.max_tokens,
            "timeout_sec": args.timeout_sec,
        }
    )
    result = release.release_draft(payload)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
