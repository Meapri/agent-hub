from __future__ import annotations

from hashlib import sha256

import pytest

from agent_hub.v2.context import (
    collect_scoped_fact_pack,
    index_project,
    search_fact_pack,
)
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.store import HubStore


def test_context_index_respects_gitignore_and_uses_fts5(tmp_path):
    (tmp_path / ".gitignore").write_text("private.txt\n")
    (tmp_path / "public.py").write_text("def durable_workflow():\n    return 'checkpoint'\n")
    (tmp_path / "private.txt").write_text("durable_workflow secret")
    ignored = tmp_path / ".venv"
    ignored.mkdir()
    (ignored / "library.py").write_text("durable_workflow dependency")
    store = HubStore(tmp_path / "state" / "state.sqlite3")

    indexed = index_project(store, project_root=str(tmp_path))
    facts = search_fact_pack(
        store,
        project_root=str(tmp_path),
        query="durable workflow checkpoint",
    )

    assert indexed["cloud_embedding_used"] is False
    assert [item["path"] for item in indexed["indexed"]] == ["public.py"]
    assert [item["path"] for item in facts["items"]] == ["public.py"]
    assert facts["items"][0]["start_line"] == 1
    assert facts["items"][0]["end_line"] == 2
    assert facts["items"][0]["complete"] is True
    assert facts["items"][0]["scope_complete"] is False
    assert facts["coverage"]["complete"] is False
    assert facts["coverage"]["reason"] == "unbounded_query"
    assert "secret" not in str(facts)


def test_context_index_redacts_secret_candidates_before_persisting(tmp_path):
    (tmp_path / "public.py").write_text("checkpoint = True\napi_key = '123456789-secret-value'\n")
    store = HubStore(tmp_path / "state" / "state.sqlite3")

    indexed = index_project(store, project_root=str(tmp_path))
    facts = search_fact_pack(
        store,
        project_root=str(tmp_path),
        query="checkpoint",
    )

    assert indexed["secret_candidates_redacted"] == 1
    assert "123456789-secret-value" not in str(facts)
    assert "[REDACTED SECRET CANDIDATE]" in facts["items"][0]["content"]


def test_context_search_reports_partial_real_line_window(tmp_path):
    lines = [f"line_{index}" for index in range(1, 81)]
    lines[59] = "durable checkpoint recovery"
    (tmp_path / "long.py").write_text("\n".join(lines) + "\n")
    store = HubStore(tmp_path / "state" / "state.sqlite3")
    index_project(store, project_root=str(tmp_path))

    facts = search_fact_pack(
        store,
        project_root=str(tmp_path),
        query="durable checkpoint recovery",
    )

    item = facts["items"][0]
    assert item["start_line"] == 42
    assert item["end_line"] == 78
    assert item["complete"] is False
    assert item["content"].splitlines()[18] == "durable checkpoint recovery"


def test_complete_reindex_removes_deleted_documents(tmp_path):
    removed = tmp_path / "removed.py"
    removed.write_text("obsolete_unique_symbol = True\n")
    (tmp_path / "kept.py").write_text("current_symbol = True\n")
    store = HubStore(tmp_path / "state" / "state.sqlite3")
    index_project(store, project_root=str(tmp_path))
    removed.unlink()

    indexed = index_project(store, project_root=str(tmp_path))
    facts = search_fact_pack(
        store,
        project_root=str(tmp_path),
        query="obsolete_unique_symbol",
    )

    assert indexed["complete"] is True
    assert indexed["stale_documents_removed"] == 1
    assert facts["items"] == []


def test_scoped_fact_pack_is_complete_line_numbered_and_digest_fenced(tmp_path):
    source = tmp_path / "module.py"
    source.write_text("def checkpoint_recovery():\n    return True\n")
    expected = {"module.py": sha256(source.read_bytes()).hexdigest()}

    facts = collect_scoped_fact_pack(
        project_root=str(tmp_path),
        source_paths=["module.py"],
        expected_sources=expected,
    )

    assert facts["coverage"] == {
        "requested_paths": ["module.py"],
        "covered_paths": ["module.py"],
        "missing_paths": [],
        "complete": True,
    }
    assert facts["items"][0]["start_line"] == 1
    assert facts["items"][0]["end_line"] == 2
    assert facts["items"][0]["complete"] is True
    assert facts["items"][0]["content"].startswith("def checkpoint_recovery")

    source.write_text("def checkpoint_recovery():\n    return False\n")
    with pytest.raises(HubV2Error) as changed:
        collect_scoped_fact_pack(
            project_root=str(tmp_path),
            source_paths=["module.py"],
            expected_sources=expected,
        )
    assert changed.value.code == "inspection_source_changed"
