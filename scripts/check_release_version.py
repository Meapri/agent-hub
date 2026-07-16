#!/usr/bin/env python3
"""Verify release version fields and an optional Git tag agree."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent


def versions() -> dict[str, str]:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package = (ROOT / "google_antigravity_codex" / "__init__.py").read_text(encoding="utf-8")
    codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    pyproject_match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject)
    package_match = re.search(r'__version__\s*=\s*"([^"]+)"', package)
    if not pyproject_match or not package_match:
        raise ValueError("could not read all release version fields")
    return {
        "pyproject": pyproject_match.group(1),
        "package": package_match.group(1),
        "codex_plugin": str(codex.get("version") or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    found = versions()
    expected = next(iter(found.values()))
    if any(value != expected for value in found.values()):
        raise SystemExit(f"version mismatch: {found}")
    tag_version = args.tag.removeprefix("v")
    if tag_version and tag_version != expected:
        raise SystemExit(f"tag {args.tag} does not match version {expected}")
    print(json.dumps({"version": expected, "fields": found}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
