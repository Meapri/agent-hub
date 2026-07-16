"""Filesystem paths for Google Antigravity Codex cache and local settings."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "google-antigravity-codex"


def config_dir() -> Path:
    override = os.getenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / APP_NAME


def cache_dir() -> Path:
    override = os.getenv("GOOGLE_ANTIGRAVITY_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / APP_NAME


def images_dir() -> Path:
    return cache_dir() / "images"
