#!/usr/bin/env python3
"""Reject the retired standalone Antigravity bundle path with clear guidance."""

from __future__ import annotations

import argparse


RETIRED_MESSAGE = (
    "The standalone Antigravity plugin bundle was retired after the Agent Hub "
    "monorepo integration. Build the Agent Hub package with `python -m build` "
    "and install its host plugins with `agent-hub setup`."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument("--platform", choices=("posix", "windows"))
    parser.parse_args(argv)
    parser.error(RETIRED_MESSAGE)


if __name__ == "__main__":
    raise SystemExit(main())
