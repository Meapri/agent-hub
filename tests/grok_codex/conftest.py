from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # provider-specific prefixes patched in tests as needed
    yield
