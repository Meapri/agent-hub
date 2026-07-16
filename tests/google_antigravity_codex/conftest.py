from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_antigravity_user_state(tmp_path, monkeypatch):
    """Keep tests independent from the developer's local Antigravity state."""
    config = tmp_path / "antigravity-config"
    cache = tmp_path / "antigravity-cache"
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONFIG_DIR", str(config))
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CACHE_DIR", str(cache))
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONSENT_FILE", str(config / "user-consent.json"))
    for name in (
        "GOOGLE_ANTIGRAVITY_USER_CONSENT",
        "GOOGLE_ANTIGRAVITY_ENABLE_CLI_BRIDGE",
        "GOOGLE_ANTIGRAVITY_ENABLE_AGY_SESSION",
        "GOOGLE_ANTIGRAVITY_CLI_TOKEN_FILE",
        "GOOGLE_ANTIGRAVITY_RUNNING_UNDER_AGY",
        "GOOGLE_ANTIGRAVITY_CLI_BRIDGE_DEPTH",
        "GOOGLE_ANTIGRAVITY_ALLOWED_ROOTS",
        "GOOGLE_ANTIGRAVITY_ALLOW_MUTATING_CLI",
        "GOOGLE_ANTIGRAVITY_ALLOW_UNSANDBOXED_CLI",
        "GOOGLE_ANTIGRAVITY_ALLOW_CHECK_COMMANDS",
        "GOOGLE_ANTIGRAVITY_ALLOW_HTTP_DOWNLOADS",
        "GOOGLE_ANTIGRAVITY_PROVIDER",
        "GOOGLE_ANTIGRAVITY_CLI",
        "GOOGLE_ANTIGRAVITY_GROUNDING",
        "GOOGLE_ANTIGRAVITY_IMAGE_MODEL",
        "GOOGLE_ANTIGRAVITY_MAX_IMAGE_BYTES",
        "GOOGLE_ANTIGRAVITY_MAX_SOURCE_BYTES",
        "GOOGLE_ANTIGRAVITY_OFFICIAL_DOMAINS",
        "GOOGLE_ANTIGRAVITY_PROJECT_ID",
        "GOOGLE_ANTIGRAVITY_VERSION",
        "GOOGLE_ANTIGRAVITY_WRITING_MODEL",
        "GOOGLE_ANTIGRAVITY_PROBE_MODELS_UNDER_CODEX",
        "GOOGLE_ANTIGRAVITY_RUNNING_UNDER_CODEX_MCP",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in list(os.environ):
        if name.startswith(("CODEX_", "MCP_")):
            monkeypatch.delenv(name, raising=False)
