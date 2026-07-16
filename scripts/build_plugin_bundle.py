#!/usr/bin/env python3
"""Build a deterministic, allowlisted Antigravity/Codex plugin directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "dist" / "antigravity-plugin" / "google-antigravity-codex"
ROOT_FILES = (
    ".mcp.json",
    "CHANGELOG.md",
    "LICENSE",
    "NOTICE.md",
    "README.md",
    "SECURITY.md",
    "mcp_config.json",
    "plugin.json",
    "pyproject.toml",
)


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def build_bundle(output: Path, platform: str) -> Path:
    output = output.resolve()
    if output == ROOT or ROOT in output.parents and output == ROOT.parent:
        raise ValueError("output must not replace the source checkout")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    for relative in ROOT_FILES:
        source = ROOT / relative
        if source.is_file():
            _copy_file(source, output / relative)
    for pattern in (
        "google_antigravity_codex/*.py",
        "scripts/*.py",
        "skills/*/SKILL.md",
        "docs/*.md",
        ".codex-plugin/plugin.json",
    ):
        for source in sorted(ROOT.glob(pattern)):
            _copy_file(source, output / source.relative_to(ROOT))

    for config_name in ("mcp_config.json", ".mcp.json"):
        path = output / config_name
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        server = data["mcpServers"]["google-antigravity-codex"]
        if platform == "windows":
            server["command"] = "py"
            server["args"] = ["-3", *server["args"]]
        else:
            server["command"] = "python3"
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--platform",
        choices=("posix", "windows"),
        default="windows" if sys.platform == "win32" else "posix",
    )
    args = parser.parse_args(argv)
    built = build_bundle(args.output, args.platform)
    print(built)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
