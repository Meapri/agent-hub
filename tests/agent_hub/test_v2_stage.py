from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.stage import apply_stage, plan_stage


def _source(root: Path) -> None:
    (root / "src/package").mkdir(parents=True)
    (root / "src/package/__init__.py").write_text("VALUE = 1\n")
    (root / "README.md").write_text("# Fixture\n")
    (root / "LICENSE").write_text("Fixture license\n")
    (root / "NOTICE.md").write_text("Fixture notice\n")
    (root / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "agent-hub-fixture"
version = "2.1.0"
""".strip()
        + "\n"
    )


def _fake_builder(_source: Path, destination: Path, _python: Path) -> None:
    for command in ("agent-hubd", "agent-hub-mcp", "agent-hub"):
        executable = destination / "bin" / command
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o700)


def _temporary_shebang_builder(_source: Path, destination: Path, python: Path) -> None:
    for command in ("agent-hubd", "agent-hub-mcp", "agent-hub"):
        executable = destination / "bin" / command
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text(f"#!{destination}/bin/python\n")
        executable.chmod(0o700)
    (destination / "bin/python").symlink_to(python)


def test_stage_release_is_digest_fenced_and_immutable(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _source(source)
    releases = tmp_path / "releases"
    proposal = plan_stage(source, releases_root=releases)
    assert len(proposal["python_sha256"]) == 64

    result = apply_stage(
        proposal,
        proposal_sha256=proposal["proposal_sha256"],
        builder=_fake_builder,
    )

    runtime = Path(result["runtime_root"])
    assert runtime.name == f"2.1.0-{proposal['source_sha256'][:12]}"
    assert (runtime / "bin/agent-hubd").is_file()
    with pytest.raises(HubV2Error) as duplicate:
        plan_stage(source, releases_root=releases)
    assert duplicate.value.code == "release_already_staged"


def test_stage_release_rewrites_temporary_console_shebangs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _source(source)
    releases = tmp_path / "releases"
    proposal = plan_stage(source, releases_root=releases)

    result = apply_stage(
        proposal,
        proposal_sha256=proposal["proposal_sha256"],
        builder=_temporary_shebang_builder,
    )

    runtime = Path(result["runtime_root"])
    assert result["relocated_shebang_count"] == 3
    assert (runtime / "bin/agent-hubd").read_text().splitlines()[0] == (f"#!{runtime}/bin/python")


def test_stage_release_rejects_source_drift(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _source(source)
    releases = tmp_path / "releases"
    proposal = plan_stage(source, releases_root=releases)
    (source / "src/package/__init__.py").write_text("VALUE = 2\n")

    with pytest.raises(HubV2Error) as conflict:
        apply_stage(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            builder=_fake_builder,
        )
    assert conflict.value.code == "release_source_conflict"


def test_stage_release_digest_covers_packaging_metadata(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _source(source)
    releases = tmp_path / "releases"
    proposal = plan_stage(source, releases_root=releases)
    (source / "NOTICE.md").write_text("Changed notice\n")

    with pytest.raises(HubV2Error) as conflict:
        apply_stage(
            proposal,
            proposal_sha256=proposal["proposal_sha256"],
            builder=_fake_builder,
        )
    assert conflict.value.code == "release_source_conflict"
