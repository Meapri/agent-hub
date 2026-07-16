"""Compatibility shim — implementation in agent_hub.core.paths."""
from agent_hub.core import paths as _core

APP_NAME = "claude-codex"
_PREFIX = "CLAUDE_CODEX"


def config_dir():
    return _core.config_dir(APP_NAME, _PREFIX)


def cache_dir():
    return _core.cache_dir(APP_NAME, _PREFIX)
