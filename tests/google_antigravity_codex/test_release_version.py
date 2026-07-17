from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


pytestmark = pytest.mark.skip(
    reason="standalone plugin bundle/release-version consistency is not used in the agent-hub monorepo; "
    "distribution + versioning are managed at the monorepo level"
)


def test_release_version_fields_match():
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_release_version.py"
    spec = importlib.util.spec_from_file_location("check_release_version", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = module.versions()
    assert found
    assert len(set(found.values())) == 1
