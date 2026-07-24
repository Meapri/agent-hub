#!/usr/bin/env python3
"""Checkout-local launcher for agent_hub.local_setup."""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_hub.local_setup import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
