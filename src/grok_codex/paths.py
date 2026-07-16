"""Config and cache paths."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "grok-codex"


def config_dir() -> Path:
    override = os.getenv("GROK_CODEX_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / APP_NAME


def cache_dir() -> Path:
    override = os.getenv("GROK_CODEX_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / APP_NAME
