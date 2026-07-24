"""Provider-specific config/cache path binding."""

from agent_hub.core import paths as _core

APP_NAME = "openai-codex"
_PREFIX = "OPENAI_CODEX"


def config_dir():
    return _core.config_dir(APP_NAME, _PREFIX)


def cache_dir():
    return _core.cache_dir(APP_NAME, _PREFIX)
