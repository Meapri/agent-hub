from __future__ import annotations

import os

import pytest

from google_antigravity_codex import writing


@pytest.fixture()
def empty_cwd(tmp_path, monkeypatch):
    """The v2 provider worker runs with an empty temporary directory as cwd."""

    workdir = tmp_path / "worker-runtime"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


def test_write_prompt_never_invents_a_project_fact_pack(empty_cwd):
    # provider_worker._invoke_arguments does not pass project_root for write, so
    # anything that reads the filesystem here describes the sandbox scratch
    # directory, not the user's project. Claiming "none detected" about a project
    # the worker cannot see is a fabricated premise, and the prompt then tells the
    # model to treat it as authoritative.
    built = writing.build_prompt(
        {
            "instruction": "README를 한국어로 다시 써라",
            "source_text": "기존 README 내용",
        }
    )
    prompt = built["prompt"]

    assert "DURABLE FACT PACK" not in prompt
    assert "none detected" not in prompt
    assert "Repository manifest complete" not in prompt
    assert str(empty_cwd) not in prompt


def test_write_prompt_does_not_read_the_working_directory(empty_cwd, monkeypatch):
    # A write step must not shell out to git or walk the tree it happens to sit in.
    def _fail(*args, **kwargs):
        raise AssertionError("write must not run a subprocess")

    monkeypatch.setattr("subprocess.run", _fail)
    monkeypatch.setattr(os, "walk", _fail)

    built = writing.build_prompt({"instruction": "릴리즈 노트를 써라", "source_text": "변경 요약"})

    assert built["prompt"]


def test_write_still_uses_the_evidence_the_caller_supplies(empty_cwd):
    built = writing.build_prompt(
        {
            "instruction": "이 fact pack만 근거로 문서를 써라",
            "source_text": "PROJECT FACTS: agent-hub 2.4.1, entrypoint agent-hubd",
        }
    )

    assert "agent-hub 2.4.1" in built["prompt"]
    assert "agent-hubd" in built["prompt"]
