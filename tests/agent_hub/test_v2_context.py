from __future__ import annotations

from agent_hub.v2.context import index_project, search_fact_pack
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
    assert facts["items"][0]["complete"] is False
    assert "secret" not in str(facts)
