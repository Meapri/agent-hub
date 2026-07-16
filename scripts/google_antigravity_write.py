#!/usr/bin/env python3
"""CLI wrapper for the integrated Antigravity writing copilot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from google_antigravity_codex import writing  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate or polish prose with Google Antigravity.")
    parser.add_argument("--task", default="auto", choices=sorted(writing.TASKS))
    parser.add_argument("--instruction", default="")
    parser.add_argument("--source-text", default="")
    parser.add_argument("--source-file", default="")
    parser.add_argument("--context", default="")
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--tone", default="")
    parser.add_argument("--audience", default="")
    parser.add_argument("--target-language", default="")
    parser.add_argument("--length", default="")
    parser.add_argument("--output-mode", default="final")
    parser.add_argument("--project-context", default="off", choices=("off", "auto", "git-summary", "git-diff"))
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--timeout-sec", type=int, default=180)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = writing.run_writing(
        {
            "task": args.task,
            "instruction": args.instruction,
            "source_text": args.source_text,
            "source_file": args.source_file,
            "context": args.context,
            "profile": args.profile,
            "tone": args.tone,
            "audience": args.audience,
            "target_language": args.target_language,
            "length": args.length,
            "output_mode": args.output_mode,
            "project_context": args.project_context,
            "project_root": args.project_root,
            "model": args.model,
            "max_tokens": args.max_tokens,
            "timeout_sec": args.timeout_sec,
        }
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
