from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path_factory, monkeypatch):
    """Keep the file-backed run store out of the real home dir during tests."""
    d = tmp_path_factory.mktemp("orchestrate_state")
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(d))
    # Drop any recipe-config override a developer might have exported.
    monkeypatch.delenv("ORCHESTRATE_CODEX_RECIPES", raising=False)
