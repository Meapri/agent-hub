"""Config/cache path resolution, parameterized by app name + env prefix.

Each package's ``paths`` module is a thin shim binding these to its own
(app_name, env_prefix) so the module-level ``config_dir()``/``cache_dir()`` API
stays unchanged for callers and tests.
"""

from __future__ import annotations

import os
from pathlib import Path


def config_dir(app_name: str, env_prefix: str) -> Path:
    override = os.getenv(f"{env_prefix}_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / app_name


def cache_dir(app_name: str, env_prefix: str) -> Path:
    override = os.getenv(f"{env_prefix}_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / app_name


def images_dir(app_name: str, env_prefix: str) -> Path:
    return cache_dir(app_name, env_prefix) / "images"
