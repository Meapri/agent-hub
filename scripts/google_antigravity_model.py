#!/usr/bin/env python3
"""List / get / set / clear Antigravity model preferences for the Codex plugin."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google_antigravity_codex import model_prefs, models  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List available models (live when possible)")
    sub.add_parser("get", help="Show saved + effective preferences")

    set_p = sub.add_parser("set", help="Save default or per-task model")
    set_p.add_argument("model", help="Model id or alias (flash, pro, opus, sonnet, …)")
    set_p.add_argument("--task", choices=list(model_prefs.TASK_KEYS), default=None)
    set_p.add_argument("--no-validate", action="store_true")

    clear_p = sub.add_parser("clear", help="Clear preferences")
    clear_p.add_argument("--task", choices=list(model_prefs.TASK_KEYS), default=None)
    clear_p.add_argument("--all", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            print(json.dumps(models.list_models({}), ensure_ascii=False, indent=2))
            return 0
        if args.command == "get":
            print(json.dumps(model_prefs.get_prefs_tool({}), ensure_ascii=False, indent=2))
            return 0
        if args.command == "set":
            result = model_prefs.set_model(
                model=args.model,
                task=args.task,
                validate=not args.no_validate,
            )
            print(result["text"])
            print(f"prefs_file={result['prefs_file']}")
            return 0
        if args.command == "clear":
            result = model_prefs.clear_prefs(task=args.task, all_prefs=bool(args.all))
            print(result["text"])
            return 0
    except model_prefs.ModelPrefsError as exc:
        print(f"ERROR ({exc.code}): {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
