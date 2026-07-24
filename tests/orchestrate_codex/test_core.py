from __future__ import annotations

from contextlib import contextmanager
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_hub.core import repository_facts
from orchestrate_codex import catalog, errors, gather, policy, recipes, runner, store, verify
from orchestrate_codex.mcp_server import dispatch_tool, handle_request, tool_definitions


def test_advise_returns_latest_models_and_strengths():
    out = dispatch_tool("orchestrate_advise", {})
    assert out["success"] is True
    assert out["latest_models"]["claude_codex_chat"] == "claude-opus-4-8"
    assert out["latest_models"]["grok_codex_chat"] == "grok-4.5"
    roles = {c["role"] for c in out["capabilities"]}
    assert {"reasoning-claude", "reasoning-grok", "author-gemini"} <= roles
    assert out["do_directly"]


def test_step_delegation_resolves_latest_model_and_context(tmp_path):
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n', encoding="utf-8")
    (tmp_path / "m.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    # host delegates architecture analysis to Claude with code context
    out = dispatch_tool(
        "orchestrate_step",
        {
            "capability": "chat",
            "instruction": "Analyze architecture",
            "doc_class": "durable",
            "gather": "code",
            "project_root": str(tmp_path),
        },
    )
    assert out["tool"] == "claude_codex_chat"
    assert out["model"] == "claude-opus-4-8"  # latest, not the leaf's stale default
    assert out["verify_after"] is True
    assert "CODE CONTEXT" in out["arguments"]["prompt"]
    assert out["fallback_tools"][0] == "claude_codex_chat"


def test_step_write_synthesis_uses_findings_as_source(tmp_path):
    out = dispatch_tool(
        "orchestrate_step",
        {
            "capability": "write",
            "write_task": "readme",
            "instruction": "Write README",
            "doc_class": "durable",
            "context": "FINDINGS: an MCP plugin",
            "project_root": str(tmp_path),
        },
    )
    assert out["tool"] == "google_antigravity_write"
    assert out["model"] == "gemini-3.1-pro-high"
    assert out["arguments"]["task"] == "readme"
    assert "FINDINGS" in out["arguments"]["source_text"]
    assert "prompt" not in out["arguments"]  # write schema shape


def test_cross_provider_fallback_fixes_model():
    # regression: a model carried across a provider fallback (grok-4.5 -> a Gemini/Claude
    # leaf) 404s. The selected tool's provider model must be substituted.
    step = {"id": "s", "capability": "chat", "instruction": "x"}
    ua = {"prompt": "hi", "model": "grok-4.5"}
    assert (
        runner._args_for_tool(
            step, "grok_codex_chat", user_args=ua, pol={"doc_class": "direct"}, artifacts={}
        )["model"]
        == "grok-4.5"
    )
    assert (
        runner._args_for_tool(
            step, "claude_codex_chat", user_args=ua, pol={"doc_class": "direct"}, artifacts={}
        )["model"]
        == "claude-opus-4-8"
    )
    assert (
        runner._args_for_tool(
            step, "google_antigravity_chat", user_args=ua, pol={"doc_class": "direct"}, artifacts={}
        )["model"]
        == "gemini-3.1-pro-high"
    )
    assert (
        runner._args_for_tool(
            step, "openai_codex_chat", user_args=ua, pol={"doc_class": "direct"}, artifacts={}
        )["model"]
        == "gpt-5.6-sol"
    )


def test_step_can_force_leaf_and_model():
    out = dispatch_tool(
        "orchestrate_step",
        {
            "capability": "chat",
            "instruction": "x",
            "leaf": "grok_codex_chat",
            "model": "grok-4.5",
        },
    )
    assert out["tool"] == "grok_codex_chat"
    assert out["arguments"]["model"] == "grok-4.5"


def test_verify_tool_flags_hallucinated_tool(tmp_path):
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n', encoding="utf-8")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    # a real detected tool so verify knows the fact-pack tool set (else it can't flag unknowns)
    (pkg / "mcp_server.py").write_text(
        'TOOLS = [{"name": "claude_codex_chat"}]\n', encoding="utf-8"
    )
    out = dispatch_tool(
        "orchestrate_verify",
        {
            "text": "Today we shipped. Call google_madeup_tool.",
            "doc_class": "durable",
            "project_root": str(tmp_path),
        },
    )
    assert any("recency" in w for w in out["warnings"])
    assert any("google_madeup_tool" in w for w in out["warnings"])


def test_catalog_latest_for():
    assert catalog.latest_for("claude_codex_chat") == "claude-opus-4-8"
    assert catalog.latest_for("unknown") is None


def test_ok_text_is_full_json_even_when_payload_has_text(tmp_path):
    # Regression: verify's payload carries its own "text" ("verify ok"); it must NOT clobber
    # the canonical JSON serialization that the stdio content[] and handoff depend on.
    import json as _json

    resp = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "orchestrate_verify",
                "arguments": {
                    "text": "clean doc",
                    "doc_class": "durable",
                    "project_root": str(tmp_path),
                },
            },
        }
    )
    content_text = resp["result"]["content"][0]["text"]
    parsed = _json.loads(content_text)  # must be valid JSON, not "verify ok"
    assert "warnings" in parsed
    # gather-bearing results have the same shape hazard
    out = dispatch_tool(
        "orchestrate_step",
        {
            "capability": "chat",
            "instruction": "x",
            "gather": "facts",
            "project_root": str(tmp_path),
        },
    )
    _json.loads(out["text"])  # round-trips


def test_run_survives_process_restart(tmp_path):
    state = runner.start_run("direct_chat", args={"prompt": "hi"}, project_root=str(tmp_path))
    rid = state["run_id"]
    assert store.load(rid) is not None  # mirrored to disk
    runner._RUNS.clear()  # simulate MCP process restart
    restored = runner.get_run(rid)
    assert restored["run_id"] == rid
    # and it can still be continued
    done = runner.continue_run(run_id=rid, stage_id="chat", result_text="ok", success=True)
    assert done["status"] == "completed"


def test_fixed_run_continue_uses_revision_cas(tmp_path):
    state = runner.start_run(
        "direct_chat",
        args={"prompt": "hi"},
        project_root=str(tmp_path),
    )
    run_id = state["run_id"]

    assert state["state_schema_version"] == 2
    assert state["store_revision"] == 0
    done = runner.continue_run(
        run_id=run_id,
        stage_id="chat",
        result_text="first",
        success=True,
        expected_revision=0,
    )
    assert done["store_revision"] == 1
    assert done["artifacts"]["draft"] == "first"

    with pytest.raises(store.RunRevisionConflict):
        runner.continue_run(
            state=state,
            stage_id="chat",
            result_text="stale",
            success=True,
        )
    assert runner.get_run(run_id)["artifacts"]["draft"] == "first"


def test_fixed_next_action_id_is_stable_and_revision_bound(tmp_path):
    state = runner.start_run(
        "direct_chat",
        args={"prompt": "hi"},
        project_root=str(tmp_path),
    )
    action = state["next_action"]

    assert action["schema"] == runner.FIXED_ACTION_SCHEMA
    assert len(action["action_id"]) == 64
    assert runner.get_run(state["run_id"])["next_action"]["action_id"] == action["action_id"]
    runner._RUNS.clear()
    assert runner.get_run(state["run_id"])["next_action"]["action_id"] == action["action_id"]

    rotated = runner.continue_run(
        run_id=state["run_id"],
        stage_id="chat",
        success=False,
        error="HTTP 429 rate limit",
        expected_revision=0,
    )
    assert rotated["next_action"]["tool"] != action["tool"]
    assert rotated["next_action"]["action_id"] != action["action_id"]
    assert rotated["next_action"]["expected_revision"] == 1


def test_fixed_action_claim_fences_duplicate_dispatch_and_token_replay(tmp_path):
    state = runner.start_run(
        "direct_chat",
        args={"prompt": "hi"},
        project_root=str(tmp_path),
    )
    action = state["next_action"]
    claimed = runner.claim_next_action(
        run_id=state["run_id"],
        expected_revision=state["store_revision"],
        action_id=action["action_id"],
        lease_seconds=60,
    )
    claim_payload = claimed.public()

    assert claim_payload["action"]["tool"] == action["tool"]
    assert claim_payload["base_revision"] == 0
    assert claim_payload["claim_token"] not in json.dumps(runner.get_run(state["run_id"]))
    with pytest.raises(store.RunLeaseActive):
        runner.claim_next_action(
            run_id=state["run_id"],
            expected_revision=0,
            action_id=action["action_id"],
        )

    completed = runner.continue_run(
        run_id=state["run_id"],
        action_id=action["action_id"],
        claim_token=claim_payload["claim_token"],
        base_revision=claim_payload["base_revision"],
        result_text="done",
        success=True,
    )
    assert completed["status"] == "completed"
    assert completed["store_revision"] == 1

    with pytest.raises(store.RunLeaseLost):
        runner.continue_run(
            run_id=state["run_id"],
            action_id=action["action_id"],
            claim_token=claim_payload["claim_token"],
            base_revision=claim_payload["base_revision"],
            result_text="replay",
        )
    assert runner.get_run(state["run_id"])["artifacts"]["draft"] == "done"


def test_wrong_fixed_action_id_releases_claim_without_provider_state_change(tmp_path):
    state = runner.start_run(
        "direct_chat",
        args={"prompt": "hi"},
        project_root=str(tmp_path),
    )

    with pytest.raises(ValueError, match="action_id"):
        runner.claim_next_action(
            run_id=state["run_id"],
            expected_revision=0,
            action_id="f" * 64,
        )

    persisted = store.load_strict(state["run_id"])
    assert persisted["store_revision"] == 0
    assert "_lease" not in persisted
    assert persisted["steps"][0]["status"] == "pending"


def test_fixed_terminal_runs_are_immutable(tmp_path):
    completed = runner.start_run(
        "direct_chat",
        args={"prompt": "hi"},
        project_root=str(tmp_path),
    )
    completed = runner.continue_run(
        run_id=completed["run_id"],
        stage_id="chat",
        result_text="done",
        expected_revision=0,
    )
    completed_again = runner.continue_run(
        run_id=completed["run_id"],
        stage_id="chat",
        result_text="replacement",
        expected_revision=completed["store_revision"],
    )

    assert completed_again["status"] == "completed"
    assert completed_again["store_revision"] == completed["store_revision"]
    assert completed_again["artifacts"]["draft"] == "done"

    failed = runner.start_run(
        "direct_chat",
        args={"prompt": "hi"},
        project_root=str(tmp_path),
    )
    failed = runner.continue_run(
        run_id=failed["run_id"],
        stage_id="chat",
        success=False,
        error="invalid request",
        expected_revision=0,
    )
    failed_again = runner.continue_run(
        run_id=failed["run_id"],
        stage_id="chat",
        result_text="must-not-revive",
        expected_revision=failed["store_revision"],
    )

    assert failed_again["status"] == "failed"
    assert failed_again["store_revision"] == failed["store_revision"]
    assert "draft" not in failed_again["artifacts"]


def test_legacy_continue_schema_and_dispatch_enforce_revision_cas(tmp_path):
    state = runner.start_run(
        "direct_chat",
        args={"prompt": "hi"},
        project_root=str(tmp_path),
    )
    run_id = state["run_id"]
    continue_spec = next(
        item for item in tool_definitions() if item["name"] == "orchestrate_continue_recipe"
    )

    assert continue_spec["inputSchema"]["properties"]["expected_revision"] == {
        "type": "integer",
        "minimum": 0,
    }
    first = dispatch_tool(
        "orchestrate_continue_recipe",
        {
            "run_id": run_id,
            "stage_id": "chat",
            "result_text": "first",
            "expected_revision": 0,
        },
    )
    assert first["success"] is True
    stale = dispatch_tool(
        "orchestrate_continue_recipe",
        {
            "run_id": run_id,
            "stage_id": "chat",
            "result_text": "stale",
            "expected_revision": 0,
        },
    )
    assert stale["success"] is False
    assert stale["error_type"] == "RunRevisionConflict"
    assert runner.get_run(run_id)["artifacts"]["draft"] == "first"


def test_fixed_runner_rejects_adaptive_run_id_without_mutating_state():
    adaptive = store.create(
        {
            "run_id": "a1b2c3d4e5f6",
            "run_kind": "adaptive",
            "state_schema_version": 2,
            "store_revision": 0,
            "status": "paused",
            "plan": {"steps": []},
            "results": {},
        }
    )

    with pytest.raises(ValueError, match="not a fixed recipe run"):
        runner.continue_run(
            run_id=adaptive["run_id"],
            expected_revision=0,
        )

    persisted = store.load(adaptive["run_id"])
    assert persisted is not None
    assert persisted["run_kind"] == "adaptive"
    assert persisted["status"] == "paused"
    assert persisted["store_revision"] == 0
    assert "_lease" not in persisted


def test_legacy_get_and_resource_reject_adaptive_state():
    adaptive = store.create(
        {
            "run_id": "d1d2d3d4d5d6",
            "run_kind": "adaptive",
            "state_schema_version": 2,
            "store_revision": 0,
            "status": "paused",
            "plan": {"steps": [{"id": "pending"}]},
            "results": {},
        }
    )

    loaded = dispatch_tool(
        "orchestrate_get_run",
        {"run_id": adaptive["run_id"]},
    )
    resource = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {
                "uri": f"orchestrate://run/{adaptive['run_id']}",
            },
        }
    )

    assert loaded["success"] is False
    assert loaded["error_type"] == "ValueError"
    assert resource["error"]["code"] == -32602
    listed = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/list",
            "params": {},
        }
    )
    assert f"orchestrate://run/{adaptive['run_id']}" not in {
        item["uri"] for item in listed["result"]["resources"]
    }


def test_fixed_runner_rejects_run_id_free_adaptive_state():
    with pytest.raises(ValueError, match="not a fixed recipe run"):
        runner.continue_run(
            state={
                "run_kind": "adaptive",
                "status": "paused",
                "steps": [{"id": "adaptive-step"}],
            }
        )


def test_fixed_get_and_resource_redact_active_lease(tmp_path):
    state = runner.start_run(
        "direct_chat",
        args={"prompt": "hi"},
        project_root=str(tmp_path),
    )
    claim = store.claim(
        state["run_id"],
        expected_revision=state["store_revision"],
        lease_seconds=60,
    )
    try:
        loaded = runner.get_run(state["run_id"])
        resource = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/read",
                "params": {
                    "uri": f"orchestrate://run/{state['run_id']}",
                },
            }
        )

        assert "_lease" not in loaded
        assert claim.token not in json.dumps(loaded)
        resource_text = resource["result"]["contents"][0]["text"]
        assert "_lease" not in resource_text
        assert claim.token not in resource_text
    finally:
        store.abort_claim(claim)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_fixed_get_never_falls_back_to_stale_memory(tmp_path, damage):
    state = runner.start_run(
        "direct_chat",
        args={"prompt": "hi"},
        project_root=str(tmp_path),
    )
    state_path = store.state_dir() / f"{state['run_id']}.json"
    if damage == "missing":
        state_path.unlink()
        expected_error = store.RunNotFound
    else:
        state_path.write_text("{broken", encoding="utf-8")
        expected_error = store.RunPersistenceError

    with pytest.raises(expected_error):
        runner.get_run(state["run_id"])


def test_run_store_rejects_path_traversal_and_invalid_ids(tmp_path, monkeypatch):
    state_root = tmp_path / "runs"
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": "do-not-read"}', encoding="utf-8")
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(state_root))

    for run_id in (
        "../../outside",
        "/tmp/outside",
        "ABCDEF123456",
        "a" * 11,
        "a" * 12 + "\n",
        "가" * 12,
        "a" * 11 + "\0",
        123456789012,
        "",
    ):
        with pytest.raises(ValueError, match="run_id"):
            store.load(run_id)
        with pytest.raises(ValueError, match="run_id"):
            store.save({"run_id": run_id, "payload": "overwrite"})
        with pytest.raises(ValueError, match="run_id"):
            store.delete(run_id)

    assert outside.read_text(encoding="utf-8") == '{"secret": "do-not-read"}'


def test_run_store_uses_private_permissions_and_rejects_symlinks(tmp_path, monkeypatch):
    state_root = tmp_path / "runs"
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": "unchanged"}', encoding="utf-8")
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(state_root))

    run_id = "a" * 12
    store.save({"run_id": run_id, "payload": "safe"})
    state_file = state_root / f"{run_id}.json"
    assert stat.S_IMODE(state_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600

    state_file.unlink()
    state_file.symlink_to(outside)
    assert store.load(run_id) is None
    store.save({"run_id": run_id, "payload": "must-not-follow"})
    assert outside.read_text(encoding="utf-8") == '{"secret": "unchanged"}'
    assert run_id not in store.list_run_ids()


def test_run_store_claim_and_commit_are_revision_fenced(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    run_id = "1" * 12
    created = store.create({"run_id": run_id, "payload": "before"})

    assert created["store_revision"] == 0
    claim = store.claim(run_id, expected_revision=0, lease_seconds=60)
    assert claim.state["payload"] == "before"
    assert claim.base_revision == 0

    with pytest.raises(store.RunLeaseActive):
        store.claim(run_id, expected_revision=0, lease_seconds=60)

    committed = store.commit_claim(
        claim,
        {**claim.state, "payload": "after"},
    )
    assert committed["store_revision"] == 1
    assert committed["payload"] == "after"
    assert "_lease" not in committed

    with pytest.raises(store.RunLeaseLost):
        store.commit_claim(claim, {**committed, "payload": "stale"})
    assert store.load(run_id)["payload"] == "after"


def test_missing_claim_does_not_create_lock_or_local_registry(tmp_path, monkeypatch):
    state_root = tmp_path / "runs"
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(state_root))
    run_id = "e" * 12
    before_keys = set(store._LOCAL_LOCKS)

    with pytest.raises(store.RunNotFound):
        store.claim(run_id)

    assert set(store._LOCAL_LOCKS) == before_keys
    assert not (state_root / f".{run_id}.lock").exists()


def test_run_store_expired_lease_takeover_fences_old_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    run_id = "2" * 12
    now = [100.0]
    monkeypatch.setattr(store.time, "time", lambda: now[0])
    store.create({"run_id": run_id})

    old_claim = store.claim(run_id, lease_seconds=10)
    now[0] = 111.0
    new_claim = store.claim(run_id, expected_revision=0, lease_seconds=10)

    with pytest.raises(store.RunLeaseLost):
        store.commit_claim(old_claim, old_claim.state)
    committed = store.commit_claim(new_claim, new_claim.state)
    assert committed["store_revision"] == 1


def test_run_store_starts_lease_clock_after_lock_acquisition(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    run_id = "8" * 12
    now = [100.0]
    monkeypatch.setattr(store.time, "time", lambda: now[0])
    store.create({"run_id": run_id})
    original_locked_state_dir = store._locked_state_dir

    @contextmanager
    def delayed_lock(target_run_id):
        with original_locked_state_dir(target_run_id) as directory_fd:
            now[0] = 150.0
            yield directory_fd

    monkeypatch.setattr(store, "_locked_state_dir", delayed_lock)

    claim = store.claim(run_id, lease_seconds=10)

    assert claim.expires_at == 160.0


def test_run_store_strict_create_does_not_overwrite_existing_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    run_id = "3" * 12
    store.create({"run_id": run_id, "payload": "original"})

    with pytest.raises(store.RunAlreadyExists):
        store.create({"run_id": run_id, "payload": "replacement"})

    assert store.load(run_id)["payload"] == "original"


def test_run_store_strict_lock_is_private_and_never_follows_symlinks(tmp_path, monkeypatch):
    state_root = tmp_path / "runs"
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(state_root))
    safe_run_id = "5" * 12
    store.create({"run_id": safe_run_id})
    assert stat.S_IMODE((state_root / f".{safe_run_id}.lock").stat().st_mode) == 0o600

    outside = tmp_path / "outside.lock"
    outside.write_text("unchanged", encoding="utf-8")
    blocked_run_id = "6" * 12
    (state_root / f".{blocked_run_id}.lock").symlink_to(outside)

    with pytest.raises(store.RunPersistenceError):
        store.create({"run_id": blocked_run_id})

    assert outside.read_text(encoding="utf-8") == "unchanged"
    assert not (state_root / f"{blocked_run_id}.json").exists()


def test_run_store_strict_paths_reject_hardlinks_without_chmod(tmp_path, monkeypatch):
    state_root = tmp_path / "runs"
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(state_root))
    store.create({"run_id": "9" * 12})

    lock_target = tmp_path / "outside.lock"
    lock_target.write_text("unchanged", encoding="utf-8")
    lock_target.chmod(0o644)
    os.link(lock_target, state_root / f".{'a' * 12}.lock")

    with pytest.raises(store.RunPersistenceError):
        store.create({"run_id": "a" * 12})

    assert stat.S_IMODE(lock_target.stat().st_mode) == 0o644
    assert lock_target.read_text(encoding="utf-8") == "unchanged"

    state_target = tmp_path / "outside-state.json"
    state_target.write_text(
        json.dumps({"run_id": "9" * 12, "payload": "outside"}),
        encoding="utf-8",
    )
    state_target.chmod(0o644)
    state_path = state_root / f"{'9' * 12}.json"
    state_path.unlink()
    os.link(state_target, state_path)

    with pytest.raises(store.RunPersistenceError):
        store.load_strict("9" * 12)
    assert store.load("9" * 12) is None
    assert stat.S_IMODE(state_target.stat().st_mode) == 0o644


def test_run_store_fifo_state_never_blocks_readers(tmp_path, monkeypatch):
    state_root = tmp_path / "runs"
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(state_root))
    store.create({"run_id": "c" * 12})
    state_path = state_root / f"{'c' * 12}.json"
    state_path.unlink()
    os.mkfifo(state_path, 0o600)
    script = """
from orchestrate_codex import store

assert store.load("cccccccccccc") is None
try:
    store.load_strict("cccccccccccc")
except store.RunPersistenceError:
    print("REJECTED")
else:
    raise AssertionError("FIFO state was accepted")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "ORCHESTRATE_CODEX_STATE_DIR": str(state_root),
        },
        text=True,
        capture_output=True,
        timeout=2,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "REJECTED"


def test_abort_claim_checks_revision_and_lease_base(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    run_id = "b" * 12
    store.create({"run_id": run_id})
    claim = store.claim(run_id, expected_revision=0, lease_seconds=60)
    with store._locked_state_dir(run_id) as directory_fd:
        tampered = store._read_state_from_dir(
            directory_fd,
            run_id,
            strict_owner=True,
        )
        tampered["store_revision"] = 1
        tampered["_lease"]["base_revision"] = 1
        store._write_state_to_dir(directory_fd, tampered)

    with pytest.raises(store.RunLeaseLost):
        store.abort_claim(claim)

    persisted = store.load_strict(run_id)
    assert persisted["_lease"]["token"] == claim.token
    assert persisted["store_revision"] == 1


def test_run_store_claim_is_serialized_across_processes(tmp_path, monkeypatch):
    state_root = tmp_path / "runs"
    trigger = tmp_path / "start"
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(state_root))
    run_id = "7" * 12
    store.create({"run_id": run_id})
    script = """
import os
from pathlib import Path
import time
from orchestrate_codex import store

original_read = store._read_state_from_dir
def slow_read(*args, **kwargs):
    state = original_read(*args, **kwargs)
    time.sleep(0.1)
    return state
store._read_state_from_dir = slow_read
trigger = Path(os.environ["AGENT_HUB_TEST_TRIGGER"])
while not trigger.exists():
    time.sleep(0.005)
try:
    store.claim("777777777777", lease_seconds=60)
    print("CLAIMED")
except store.RunLeaseActive:
    print("ACTIVE")
"""
    env = {
        **os.environ,
        "ORCHESTRATE_CODEX_STATE_DIR": str(state_root),
        "AGENT_HUB_TEST_TRIGGER": str(trigger),
    }
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    trigger.write_text("go", encoding="utf-8")
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        outputs.append(stdout.strip())

    assert sorted(outputs) == ["ACTIVE", "CLAIMED"]


def test_run_store_rejects_broad_or_symlinked_state_directories(tmp_path, monkeypatch):
    home_mode = stat.S_IMODE(Path.home().stat().st_mode)
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(Path.home()))
    with pytest.raises(ValueError, match="too broad"):
        store.state_dir()
    assert stat.S_IMODE(Path.home().stat().st_mode) == home_mode

    target = tmp_path / "actual-runs"
    target.mkdir()
    linked = tmp_path / "linked-runs"
    linked.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(linked))
    with pytest.raises(ValueError, match="symlink"):
        store.state_dir()

    parent_target = tmp_path / "outside-parent"
    parent_target.mkdir()
    parent_link = tmp_path / "linked-parent"
    parent_link.symlink_to(parent_target, target_is_directory=True)
    monkeypatch.setenv(
        "ORCHESTRATE_CODEX_STATE_DIR",
        str(parent_link / "nested-runs"),
    )
    with pytest.raises(ValueError, match="symlink"):
        store.state_dir()
    assert not (parent_target / "nested-runs").exists()


def test_run_store_allows_trusted_macos_var_alias(tmp_path, monkeypatch):
    var_alias = Path("/var")
    if not var_alias.is_symlink():
        pytest.skip("macOS /var system alias is not present")
    try:
        relative = tmp_path.resolve().relative_to("/private/var")
    except ValueError:
        pytest.skip("pytest temp directory is not under /private/var")
    aliased_state_dir = var_alias / relative / "alias-runs"
    monkeypatch.setenv(
        "ORCHESTRATE_CODEX_STATE_DIR",
        str(aliased_state_dir),
    )

    resolved = store.state_dir()

    assert resolved == aliased_state_dir.resolve()
    assert stat.S_IMODE(resolved.stat().st_mode) == 0o700


def test_run_store_keeps_file_access_on_validated_directory_fd(tmp_path, monkeypatch):
    state_root = tmp_path / "runs"
    outside = tmp_path / "outside"
    outside.mkdir()
    run_id = "c" * 12
    (outside / f"{run_id}.json").write_text(
        f'{{"run_id": "{run_id}", "secret": "outside"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(state_root))
    original_open = store._open_state_dir
    swapped = False

    def open_then_swap():
        nonlocal swapped
        root, directory_fd = original_open()
        if not swapped:
            swapped = True
            validated = tmp_path / "validated-runs"
            root.rename(validated)
            root.symlink_to(outside, target_is_directory=True)
        return root, directory_fd

    monkeypatch.setattr(store, "_open_state_dir", open_then_swap)

    assert store.load(run_id) is None


def test_run_store_rejects_symlink_created_while_directory_is_opened(tmp_path, monkeypatch):
    state_root = tmp_path / "runs"
    outside = tmp_path / "outside"
    outside.mkdir()
    run_id = "d" * 12
    (outside / f"{run_id}.json").write_text(
        f'{{"run_id": "{run_id}", "secret": "outside"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(state_root))
    original_lstat = Path.lstat
    injected = False

    def lstat_then_inject(path):
        nonlocal injected
        try:
            return original_lstat(path)
        except FileNotFoundError:
            if Path(path) == state_root and not injected:
                injected = True
                state_root.symlink_to(outside, target_is_directory=True)
            raise

    monkeypatch.setattr(Path, "lstat", lstat_then_inject)

    assert store.load(run_id) is None


def test_run_id_contract_applies_to_memory_resources_and_legacy_schemas(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    runner._RUNS["../../outside"] = {"run_id": "../../outside", "steps": []}

    run_specs = {
        item["name"]: item["inputSchema"]
        for item in tool_definitions()
        if item["name"] in {"orchestrate_continue_recipe", "orchestrate_get_run"}
    }
    assert run_specs["orchestrate_continue_recipe"]["properties"]["run_id"]["pattern"] == (
        "^[0-9a-f]{12}$"
    )
    assert run_specs["orchestrate_get_run"]["properties"]["run_id"]["pattern"] == ("^[0-9a-f]{12}$")
    assert "../../outside" not in {
        item["uri"].removeprefix("orchestrate://run/")
        for item in handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}}
        )["result"]["resources"]
    }

    malformed = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": "orchestrate://run/../../outside"},
        }
    )
    assert malformed["error"]["code"] == -32602
    with pytest.raises(ValueError, match="run_id"):
        runner.get_run("../../outside")


def test_runner_rejects_mismatched_full_state_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATE_CODEX_STATE_DIR", str(tmp_path / "runs"))
    state = runner.start_run(
        "direct_chat",
        args={"prompt": "hello"},
        project_root=str(tmp_path),
    )

    with pytest.raises(ValueError, match="does not match"):
        runner.continue_run(
            run_id="b" * 12,
            state=state,
            stage_id="chat",
            result_text="done",
        )
    with pytest.raises(ValueError, match="run_id"):
        runner.continue_run(
            state={**state, "run_id": 0},
            stage_id="chat",
            result_text="done",
        )


def test_looks_like_leaf_error():
    err = "HTTP 503: upstream connect error or disconnect/reset before headers. Connection refused"
    assert errors.looks_like_leaf_error(err) is True
    assert errors.classify(err) == "transient"  # -> rotatable
    # a long real document that merely mentions an error code is NOT flagged
    doc = "# README\n\n" + ("This tool handles HTTP 500 responses gracefully. " * 40)
    assert errors.looks_like_leaf_error(doc) is False
    assert errors.looks_like_leaf_error("") is False
    # 4xx backend errors handed back as text are also leaf failures (regression: 404 missed)
    assert (
        errors.looks_like_leaf_error(
            "Antigravity Code Assist returned HTTP 404; response body omitted."
        )
        is True
    )


def test_error_classification_and_no_rotate_on_bad_request(tmp_path):
    assert errors.classify("HTTP 429 rate limit") == "rate_limit"
    assert errors.classify("401 Unauthorized") == "auth"
    assert errors.classify("unknown property 'prompt'") == "bad_request"
    state = runner.start_run("direct_chat", args={"prompt": "hi"}, project_root=str(tmp_path))
    # bad_request must NOT rotate providers (a different leaf won't fix a schema error)
    out = runner.continue_run(run_id=state["run_id"], success=False, error="400 invalid argument")
    assert out["status"] == "failed"
    assert out["steps"][0]["error_category"] == "bad_request"


def test_list_recipes():
    items = recipes.list_recipes()
    ids = {r["id"] for r in items}
    assert "durable_readme" in ids
    assert "change_pr" in ids


def test_unified_workflows_group_presets_without_legacy_aliases():
    workflows = recipes.list_workflows()
    assert {item["id"] for item in workflows} == {
        "repo_document",
        "git_document",
        "research_brief",
        "deep_readme",
    }
    readme = recipes.resolve_workflow("repo_document", "readme")
    assert readme["recipe_id"] == "durable_readme"
    with pytest.raises(ValueError, match="unknown workflow"):
        recipes.resolve_workflow("research_then_write")
    with pytest.raises(ValueError, match="unknown recipe"):
        runner.start_run("research_then_write", args={"prompt": "q"})
    with pytest.raises(ValueError, match="unknown workflow"):
        recipes.resolve_workflow("durable_readme")


def test_one_stage_recipes_are_not_presented_as_workflows():
    with pytest.raises(ValueError, match="unknown workflow"):
        recipes.resolve_workflow("direct_chat")


def test_multi_domain_recipes_registered():
    ids = {r["id"] for r in recipes.list_recipes()}
    for rid in (
        "technical_doc",
        "proposal",
        "release_notes",
        "translate_doc",
        "polish_text",
        "summarize_text",
        "blog_post",
        "email_draft",
        "product_copy",
        "research_brief",
        "review_diff",
        "release_draft",
        "generate_image",
        "compare_models",
    ):
        assert rid in ids, rid


def test_domain_recipes_route_to_expected_leaf(tmp_path):
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n', encoding="utf-8")
    cases = {
        "technical_doc": ("google_antigravity_write", {"task": "technical-doc"}),
        "translate_doc": ("google_antigravity_write", {"task": "translate"}),
        "generate_image": ("google_antigravity_generate_image", {}),
        "review_diff": ("google_antigravity_review_diff", {}),
        "release_draft": ("google_antigravity_release_draft", {}),
        "compare_models": ("google_antigravity_compare_models", {}),
    }
    for rid, (tool, must_have) in cases.items():
        state = runner.start_run(rid, args={"prompt": "go"}, project_root=str(tmp_path))
        na = state["next_action"]
        assert na["tool"] == tool, rid
        for k, v in must_have.items():
            assert na["arguments"].get(k) == v, (rid, k)
        assert "prompt" not in na["arguments"] or tool.endswith(("_image", "_compare_models"))


def test_research_brief_feeds_search_into_write_source(tmp_path):
    state = runner.start_run("research_brief", args={"prompt": "q"}, project_root=str(tmp_path))
    assert state["next_action"]["tool"] == "google_grounded_search"
    state2 = runner.continue_run(
        run_id=state["run_id"], stage_id="search", result_text="S1\nS2", success=True
    )
    na = state2["next_action"]
    assert na["tool"] == "google_antigravity_write"
    assert na["arguments"]["task"] == "summarize"
    assert na["arguments"]["source_text"] == "S1\nS2"


def test_transform_fallback_folds_source_text(tmp_path):
    state = runner.start_run(
        "translate_doc",
        args={"prompt": "translate", "source_text": "Bonjour", "target_language": "Korean"},
        project_root=str(tmp_path),
    )
    assert state["next_action"]["tool"] == "google_antigravity_write"
    state2 = runner.continue_run(run_id=state["run_id"], success=False, error="quota")
    na = state2["next_action"]
    assert na["tool"] == "claude_codex_chat"
    assert "task" not in na["arguments"]
    assert "SOURCE TEXT" in na["arguments"]["prompt"]


def test_verify_reruns_and_triggers_revision(tmp_path):
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n', encoding="utf-8")
    state = runner.start_run(
        "durable_readme", args={"prompt": "readme"}, project_root=str(tmp_path)
    )
    # A draft full of recency/session-diary language must bounce back to the draft stage.
    bad = runner.continue_run(
        run_id=state["run_id"],
        stage_id="draft",
        result_text="Today we fixed the parser in this session.",
        success=True,
    )
    assert bad["status"] == "running"
    assert bad["next_action"]["stage_id"] == "draft"
    assert bad["revisions"] == 1
    assert "REVISE" in str(bad["next_action"]["arguments"])
    # A clean redraft completes; the revision budget prevents an infinite loop.
    good = runner.continue_run(
        run_id=state["run_id"],
        stage_id="draft",
        result_text="# Project\n\nInstall with pip. Does X.",
        success=True,
    )
    assert good["status"] == "completed"
    assert good["revisions"] <= good["revision_budget"]


def test_revision_budget_zero_disables_loop(tmp_path):
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n', encoding="utf-8")
    state = runner.start_run(
        "durable_readme", args={"prompt": "r", "revision_budget": 0}, project_root=str(tmp_path)
    )
    out = runner.continue_run(
        run_id=state["run_id"],
        stage_id="draft",
        result_text="today we changed things this session",
        success=True,
    )
    assert out["status"] == "failed"  # a blocking quality failure cannot be accepted
    assert out["revisions"] == 0
    assert any("recency" in w for w in out.get("warnings", []))  # still surfaced as a warning


def test_korean_style_failure_triggers_revision_then_fails_closed(tmp_path):
    state = runner.start_run(
        "durable_readme",
        args={"prompt": "README를 작성해 주세요."},
        project_root=str(tmp_path),
    )
    first = runner.continue_run(
        run_id=state["run_id"],
        stage_id="draft",
        result_text="# 안내\n\n이전 이름은 지원하지 않습니다.",
        success=True,
    )
    assert first["status"] == "running"
    assert first["next_action"]["stage_id"] == "draft"
    assert first["revisions"] == 1

    second = runner.continue_run(
        run_id=state["run_id"],
        stage_id="draft",
        result_text="# 안내\n\n이전 이름은 지원하지 않습니다.",
        success=True,
    )
    assert second["status"] == "failed"
    assert "document_quality_failed" in second["error"]


def test_verify_allows_cli_commands(tmp_path):
    # A README that references a real CLI script/console-script must NOT be flagged as a
    # hallucinated tool (regression from a live multi-LLM run on Grok Codex).
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "1.0.0"\n\n[project.scripts]\ngrok_codex_mcp = "x:serve"\n',
        encoding="utf-8",
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "grok_codex_login.py").write_text("# login\n", encoding="utf-8")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mcp_server.py").write_text('T = [{"name": "grok_codex_chat"}]\n', encoding="utf-8")
    facts = gather.gather_durable_facts(tmp_path)
    assert "grok_codex_login" in facts["cli_commands"]
    assert facts["install_commands"]  # pip install -e . detected
    body = (
        "Run `python3 scripts/grok_codex_login.py` and start grok_codex_mcp. Also grok_codex_chat."
    )
    result = verify.verify_text(body, doc_class="durable", fact_pack=facts)
    assert not any("tool_not_in_fact_pack" in w for w in result["warnings"])
    # a genuinely invented tool is still flagged
    bad = verify.verify_text("Call grok_codex_teleport now.", doc_class="durable", fact_pack=facts)
    assert any("grok_codex_teleport" in w for w in bad["warnings"])


def test_verify_flags_truncated_document():
    # a long doc cut off after a heading (leaf hit max_tokens) must not pass as clean
    body = (
        "# Title\n\n## 개요\n" + "This section has real content. " * 12 + "\n\n## 아키텍처\n\nGrok"
    )
    r = verify.verify_text(body, doc_class="durable")
    assert r["ok"] is False
    assert any(w.startswith("truncated") for w in r["warnings"])
    # a properly closed doc of the same size passes
    ok_body = (
        "# Title\n\n## 개요\n"
        + "This section has real content. " * 12
        + "\n\n## 라이선스\nReleased under the MIT license."
    )
    assert verify.verify_text(ok_body, doc_class="durable")["ok"] is True


def test_verify_flags_unclosed_code_fence():
    body = "# T\n\n" + "x" * 220 + "\n\n```bash\npip install -e ."  # fence never closed
    r = verify.verify_text(body, doc_class="durable")
    assert "unclosed_code_fence" in r["warnings"] and r["ok"] is False


def test_write_step_sets_max_tokens_budget():
    out = dispatch_tool(
        "orchestrate_step",
        {
            "capability": "write",
            "write_task": "readme",
            "instruction": "x",
            "doc_class": "durable",
            "project_root": ".",
        },
    )
    assert out["arguments"]["max_tokens"] == runner.DEFAULT_WRITE_MAX_TOKENS
    # caller can override
    out2 = dispatch_tool(
        "orchestrate_step",
        {
            "capability": "write",
            "write_task": "readme",
            "instruction": "x",
            "extra_args": {"max_tokens": 2000},
        },
    )
    assert out2["arguments"]["max_tokens"] == 2000


def test_verify_allows_package_provider_and_leaf_names(tmp_path):
    # meta-doc regression: a README ABOUT the orchestrator names the package, provider
    # prefixes, wildcards, and leaf tools defined in sibling repos — none are hallucinations.
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    pkg = tmp_path / "orchestrate_codex"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mcp_server.py").write_text('T = [{"name": "orchestrate_advise"}]\n', encoding="utf-8")
    facts = gather.gather_durable_facts(tmp_path)
    assert "orchestrate_codex" in facts["packages"]
    body = (
        "The orchestrate_codex package routes to the google_antigravity_write leaf and "
        "claude_codex_chat. Wildcards like google_antigravity_* are supported. " * 4
    )
    r = verify.verify_text(body, doc_class="durable", fact_pack=facts)
    assert not any(w.startswith("tool_not_in_fact_pack") for w in r["warnings"]), r["warnings"]
    # a truly invented tool is still caught
    bad = verify.verify_text(
        "Call orchestrate_teleport." * 20, doc_class="durable", fact_pack=facts
    )
    assert any("orchestrate_teleport" in w for w in bad["warnings"])
    # sibling-repo launcher commands referenced in a meta-doc are legitimate
    meta = "Run the leaf servers grok_codex_mcp, claude_codex_mcp, google_antigravity_mcp. " * 4
    r2 = verify.verify_text(meta, doc_class="durable", fact_pack=facts)
    assert not any(w.startswith("tool_not_in_fact_pack") for w in r2["warnings"]), r2["warnings"]


def test_verify_session_diary_noun_not_flagged():
    # "session diary" as policy vocabulary (not recency tone) must not trigger recency
    body = "이 문서는 durable 정책을 따르며 session diary 를 제외합니다. " * 8
    r = verify.verify_text(body, doc_class="durable")
    assert not any("recency" in w for w in r["warnings"])
    # actual recency tone still caught
    assert any(
        "recency" in w
        for w in verify.verify_text("today we fixed it " * 20, doc_class="durable")["warnings"]
    )


def test_change_doc_recency_not_flagged():
    result = verify.verify_text("today we fixed the parser", doc_class="change")
    assert not any("recency" in w for w in result["warnings"])


def test_user_recipe_from_config(tmp_path, monkeypatch):
    cfg = tmp_path / "recipes.json"
    cfg.write_text(
        '{"faq_doc": {"write_task": "technical-doc", "doc_class": "durable", "description": "FAQ"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCHESTRATE_CODEX_RECIPES", str(cfg))
    ids = {r["id"] for r in recipes.list_recipes()}
    assert "faq_doc" in ids
    recipe = recipes.get_recipe("faq_doc")
    draft = next(s for s in recipe["stages"] if s["id"] == "draft")
    assert draft["write_task"] == "technical-doc"


def test_resolve_bindings_discovery():
    # Only chat leaves connected: write degrades to chat (runnable), but review/release/compare block.
    res = runner.resolve_bindings(["claude_codex_chat", "grok_codex_chat"])
    assert res["bindings"]["chat"] == "claude_codex_chat"
    assert res["bindings"]["write"] == "claude_codex_chat"  # fell back to chat
    blocked = {b["id"] for b in res["blocked_recipes"]}
    assert "review_diff" in blocked and "compare_models" in blocked
    assert "direct_chat" in res["runnable_recipes"]


def test_mcp_prompts_and_resources(tmp_path):
    init = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )["result"]
    assert set(init["capabilities"]) >= {"tools", "prompts", "resources"}
    prompts = handle_request({"jsonrpc": "2.0", "id": 2, "method": "prompts/list", "params": {}})[
        "result"
    ]
    assert any(p["name"] == "durable_readme" for p in prompts["prompts"])
    state = runner.start_run("direct_chat", args={"prompt": "hi"}, project_root=str(tmp_path))
    rl = handle_request({"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}})[
        "result"
    ]
    uri = f"orchestrate://run/{state['run_id']}"
    assert any(r["uri"] == uri for r in rl["resources"])
    rr = handle_request(
        {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": uri}}
    )["result"]
    import json as _json

    assert _json.loads(rr["contents"][0]["text"])["run_id"] == state["run_id"]


def test_passthrough_forwards_domain_args():
    out = dispatch_tool(
        "orchestrate_start_run",
        {"recipe_id": "compare_models", "prompt": "hi", "models": ["a", "b"], "project_root": "."},
    )
    assert out["success"] is True
    assert out["next_action"]["arguments"]["models"] == ["a", "b"]


def test_durable_policy_forbids_git():
    pol = policy.get_policy("durable")
    assert pol["git"] == "off"
    assert pol["session_diary"] == "off"


def test_durable_readme_routes_to_write_leaf():
    plan = recipes.plan_recipe("durable_readme", args={"prompt": "rewrite readme"})
    draft = next(s for s in plan["steps"] if s["id"] == "draft")
    assert draft["tool"] == "google_antigravity_write"
    assert draft["suggested_arguments"]["task"] == "readme"
    # write leaf schema forbids `prompt`; we must not send it.
    assert "prompt" not in draft["suggested_arguments"]


def test_chat_binds_claude_by_default():
    plan = recipes.plan_recipe("direct_chat", args={"prompt": "hi"})
    assert plan["steps"][0]["tool"] == "claude_codex_chat"


def test_plan_binding_override():
    plan = recipes.plan_recipe(
        "direct_chat",
        args={"prompt": "hi"},
        bindings={"chat": "grok_codex_chat"},
    )
    assert plan["steps"][0]["tool"] == "grok_codex_chat"


def test_mcp_tools():
    names = {t["name"] for t in tool_definitions()}
    assert "orchestrate_start_run" in names
    assert "orchestrate_claim_next_action" in names
    assert "orchestrate_continue_recipe" in names
    listed = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert listed["result"]["tools"]
    out = dispatch_tool("orchestrate_list_recipes", {})
    assert out["success"] is True


def test_gather_durable_facts(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "demo").mkdir()
    package = tmp_path / "src" / "demo_package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    facts = gather.gather_durable_facts(tmp_path)
    assert facts["ok"] is True
    assert facts["version"] == "1.2.3"
    assert "demo" in facts["skills"]
    assert "demo_package" in facts["packages"]
    assert "DURABLE FACT PACK" in facts["text"]


def test_durable_manifest_includes_non_ignored_untracked_files(tmp_path):
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "new_runtime.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("local\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitignore", "tracked.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    facts = gather.gather_durable_facts(tmp_path)

    assert facts["repository_manifest_complete"] is True
    assert set(facts["repository_files"]) == {
        ".gitignore",
        "new_runtime.py",
        "tracked.py",
    }


def test_durable_manifest_bounds_parent_directory_output(tmp_path):
    for index in range(10):
        relative = Path(
            *[f"branch_{index}_{depth:02}" for depth in range(30)],
            "module.py",
        )
        target = tmp_path / relative
        target.parent.mkdir(parents=True)
        target.write_text("VALUE = 1\n", encoding="utf-8")

    manifest = repository_facts.collect_repository_manifest(
        tmp_path,
        max_files=1_000,
        max_chars=1_000,
    )
    output_chars = sum(
        len(item) + 1
        for key in ("repository_files", "repository_directories")
        for item in manifest[key]
    )

    assert output_chars <= 1_000
    assert manifest["repository_directories_truncated"] is True


def test_durable_facts_do_not_read_git_ignored_readme(tmp_path):
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("README.md\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "# Private notes\n\nIGNORED_README_SECRET\n",
        encoding="utf-8",
    )

    facts = gather.gather_durable_facts(tmp_path)

    assert "README.md" not in facts["repository_files"]
    assert facts["readme_preview_chars"] == 0
    assert "IGNORED_README_SECRET" not in facts["text"]


def test_empty_git_repository_does_not_fall_back_to_ignored_files(tmp_path):
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("ignored.json\n", encoding="utf-8")
    (tmp_path / "ignored.json").write_text('{"token": "secret"}\n', encoding="utf-8")

    facts = gather.gather_durable_facts(tmp_path)
    context = gather.gather_code_context(tmp_path, depth="shallow")

    assert facts["repository_files"] == [".gitignore"]
    assert context["candidate_count"] == 0
    assert "ignored.json" not in context["text"]


def test_durable_facts_do_not_follow_symlinks_or_include_sensitive_files(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
    outside.write_text("OUTSIDE_DURABLE_SECRET\n", encoding="utf-8")
    (tmp_path / "README.md").symlink_to(outside)
    (tmp_path / "pyproject.toml").symlink_to(outside)
    (tmp_path / ".mcp.json").symlink_to(outside)
    package = tmp_path / "package"
    package.mkdir()
    (package / "mcp_server.py").symlink_to(outside)
    sensitive = tmp_path / ".claude"
    sensitive.mkdir()
    (sensitive / ".credentials.json").write_text(
        '{"token": "CLAUDE_SECRET"}\n',
        encoding="utf-8",
    )
    (tmp_path / "oauth-token.json").write_text(
        '{"token": "OAUTH_SECRET"}\n',
        encoding="utf-8",
    )
    (tmp_path / "auth.json").write_text(
        '{"token": "AUTH_SECRET"}\n',
        encoding="utf-8",
    )

    facts = gather.gather_durable_facts(tmp_path)
    context = gather.gather_code_context(tmp_path, depth="deep")
    combined = facts["text"] + "\n" + context["text"]

    assert "OUTSIDE_DURABLE_SECRET" not in combined
    assert "CLAUDE_SECRET" not in combined
    assert "OAUTH_SECRET" not in combined
    assert "AUTH_SECRET" not in combined
    assert not {
        "README.md",
        "pyproject.toml",
        ".mcp.json",
        "package/mcp_server.py",
        ".claude/.credentials.json",
        "oauth-token.json",
        "auth.json",
    } & set(facts["repository_files"])


def test_durable_facts_reject_root_swap_during_manifest_collection(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text(
        "# Inside\n\nINSIDE_ROOT_MARKER\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "README.md").write_text(
        "# Outside\n\nOUTSIDE_ROOT_SWAP_SECRET\n",
        encoding="utf-8",
    )
    moved_root = tmp_path / "repo-original"
    original_collect = gather.collect_repository_manifest

    def swap_then_collect(project_root, **kwargs):
        root.rename(moved_root)
        root.symlink_to(outside, target_is_directory=True)
        return original_collect(project_root, **kwargs)

    monkeypatch.setattr(gather, "collect_repository_manifest", swap_then_collect)

    with pytest.raises(ValueError, match="changed during collection"):
        gather.gather_durable_facts(root)


def test_durable_manifest_is_anchored_during_temporary_root_swap(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "inside.py").write_text("INSIDE = 1\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_name = "OUTSIDE_FILENAME_SECRET.py"
    (outside / outside_name).write_text("OUTSIDE_CONTENT_SECRET = 1\n", encoding="utf-8")
    moved_root = tmp_path / "repo-original"
    original_collect = gather.collect_repository_manifest

    def collect_during_temporary_swap(project_root, **kwargs):
        root.rename(moved_root)
        root.symlink_to(outside, target_is_directory=True)
        try:
            return original_collect(project_root, **kwargs)
        finally:
            root.unlink()
            moved_root.rename(root)

    monkeypatch.setattr(
        gather,
        "collect_repository_manifest",
        collect_during_temporary_swap,
    )

    facts = gather.gather_durable_facts(root)

    assert facts["repository_files"] == ["inside.py"]
    assert outside_name not in facts["text"]
    assert "OUTSIDE_CONTENT_SECRET" not in facts["text"]


def test_code_context_git_metadata_is_anchored_during_root_swap(tmp_path, monkeypatch):
    import subprocess

    root = tmp_path / "project"
    root.mkdir()
    (root / "inside.py").write_text("INSIDE = 1\n", encoding="utf-8")
    outside = tmp_path / "outside-git"
    outside.mkdir()
    subprocess.run(["git", "init"], cwd=outside, check=True, capture_output=True)
    outside_name = "OUTSIDE_GIT_FILENAME_SECRET.py"
    (outside / outside_name).write_text("OUTSIDE_GIT_CONTENT = 1\n", encoding="utf-8")
    moved_root = tmp_path / "project-original"
    original_gather_git = gather.gather_git

    def gather_git_during_temporary_swap(project_root, **kwargs):
        root.rename(moved_root)
        root.symlink_to(outside, target_is_directory=True)
        try:
            return original_gather_git(project_root, **kwargs)
        finally:
            root.unlink()
            moved_root.rename(root)

    monkeypatch.setattr(gather, "gather_git", gather_git_during_temporary_swap)

    context = gather.gather_code_context(root, depth="shallow")

    assert "inside.py" in context["files"]
    assert context["git"]["ok"] is False
    assert outside_name not in context["text"]
    assert "OUTSIDE_GIT_CONTENT" not in context["text"]


def test_deep_code_context_covers_interfaces_tests_and_git_state(tmp_path):
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "mcp_server.py").write_text("TOOL = 'x'\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_public.py").write_text("def test_public():\n    assert True\n", encoding="utf-8")

    context = gather.gather_code_context(tmp_path, depth="deep")

    assert context["depth"] == "deep"
    assert "src/pkg/mcp_server.py" in context["files"]
    assert "tests/test_public.py" in context["files"]
    assert "Coverage checklist" in context["text"]
    assert "Git state" in context["text"]
    assert "     1 |" in context["text"]


def test_code_context_scopes_git_candidates_to_requested_subdirectory(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    target = repo / "packages" / "target"
    target.mkdir(parents=True)
    (target / "inside.py").write_text("INSIDE = 1\n", encoding="utf-8")
    (repo / "sibling.py").write_text("SIBLING_SECRET = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    commit_secret = "SIBLING_COMMIT_SECRET"
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            commit_secret,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    private_sibling = "SIBLING_PRIVATE_NAME.py"
    (repo / private_sibling).write_text("PRIVATE = 1\n", encoding="utf-8")

    context = gather.gather_code_context(target, depth="deep")

    assert "inside.py" in context["files"]
    assert "sibling.py" not in context["files"]
    assert "SIBLING_SECRET" not in context["text"]
    assert private_sibling not in context["text"]
    assert private_sibling not in context["git"]["status"]
    assert commit_secret not in context["text"]
    assert context["git"]["branch"] == "[scoped]"


def test_code_context_respects_gitignore_and_blocks_sensitive_or_symlinked_files(tmp_path):
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("ignored.json\n", encoding="utf-8")
    (tmp_path / "tracked.py").write_text("TRACKED_MARKER = 1\n", encoding="utf-8")
    (tmp_path / "visible.py").write_text("VISIBLE_MARKER = 1\n", encoding="utf-8")
    (tmp_path / "ignored.json").write_text('{"token": "IGNORED_SECRET"}\n', encoding="utf-8")
    (tmp_path / "credentials.json").write_text(
        '{"token": "TRACKED_SECRET"}\n',
        encoding="utf-8",
    )
    outside = tmp_path.parent / "outside_target.py"
    outside.write_text("OUTSIDE_SECRET = 1\n", encoding="utf-8")
    (tmp_path / "linked.py").symlink_to(outside)
    (tmp_path / "linked_inside.py").symlink_to(tmp_path / "tracked.py")
    subprocess.run(
        ["git", "add", ".gitignore", "tracked.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-f", "credentials.json"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    context = gather.gather_code_context(tmp_path, depth="deep")

    assert {"tracked.py", "visible.py"} <= set(context["files"])
    assert not {
        "ignored.json",
        "credentials.json",
        "linked.py",
        "linked_inside.py",
    } & set(context["files"])
    assert "IGNORED_SECRET" not in context["text"]
    assert "TRACKED_SECRET" not in context["text"]
    assert "OUTSIDE_SECRET" not in context["text"]


def test_gather_blocks_sensitive_yaml_names(tmp_path):
    (tmp_path / "visible.py").write_text("VISIBLE = 1\n", encoding="utf-8")
    (tmp_path / "secrets.yaml").write_text(
        "token: YAML_SECRET_SENTINEL\n",
        encoding="utf-8",
    )
    (tmp_path / "credentials.yml").write_text(
        "password: YAML_CREDENTIAL_SENTINEL\n",
        encoding="utf-8",
    )
    (tmp_path / "secrets.txt").write_text(
        "TEXT_SECRET_SENTINEL\n",
        encoding="utf-8",
    )
    (tmp_path / "credentials.py").write_text(
        "PYTHON_CREDENTIAL_SENTINEL = 1\n",
        encoding="utf-8",
    )
    (tmp_path / "credentials.local.py").write_text(
        "VARIANT_CREDENTIAL_SENTINEL = 1\n",
        encoding="utf-8",
    )
    (tmp_path / "secrets.backup.txt").write_text(
        "VARIANT_SECRET_SENTINEL\n",
        encoding="utf-8",
    )
    (tmp_path / ".credentials.py").write_text(
        "HIDDEN_CREDENTIAL_SENTINEL = 1\n",
        encoding="utf-8",
    )
    (tmp_path / ".secrets.txt").write_text(
        "HIDDEN_SECRET_SENTINEL\n",
        encoding="utf-8",
    )

    facts = gather.gather_durable_facts(tmp_path)
    context = gather.gather_code_context(tmp_path, depth="deep")
    combined = facts["text"] + "\n" + context["text"]

    assert "secrets.yaml" not in facts["repository_files"]
    assert "credentials.yml" not in facts["repository_files"]
    assert "secrets.txt" not in facts["repository_files"]
    assert "credentials.py" not in facts["repository_files"]
    assert "credentials.local.py" not in facts["repository_files"]
    assert "secrets.backup.txt" not in facts["repository_files"]
    assert ".credentials.py" not in facts["repository_files"]
    assert ".secrets.txt" not in facts["repository_files"]
    assert "YAML_SECRET_SENTINEL" not in combined
    assert "YAML_CREDENTIAL_SENTINEL" not in combined
    assert "TEXT_SECRET_SENTINEL" not in combined
    assert "PYTHON_CREDENTIAL_SENTINEL" not in combined
    assert "VARIANT_CREDENTIAL_SENTINEL" not in combined
    assert "VARIANT_SECRET_SENTINEL" not in combined
    assert "HIDDEN_CREDENTIAL_SENTINEL" not in combined
    assert "HIDDEN_SECRET_SENTINEL" not in combined


def test_code_context_rejects_parent_symlink_swap_during_read(tmp_path, monkeypatch):
    package = tmp_path / "package"
    package.mkdir()
    target = package / "target.py"
    target.write_text("INSIDE_MARKER = 1\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "target.py").write_text(
        "OUTSIDE_RACE_SECRET = 1\n",
        encoding="utf-8",
    )
    original_package = tmp_path / "package-original"
    original_safe = gather._safe_code_file
    target_checks = 0

    def swap_after_validation(path, root, **kwargs):
        nonlocal target_checks
        result = original_safe(path, root, **kwargs)
        if path == target and result[0]:
            target_checks += 1
            if target_checks == 2:
                package.rename(original_package)
                package.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(gather, "_safe_code_file", swap_after_validation)

    context = gather.gather_code_context(tmp_path, depth="shallow")

    assert target_checks >= 2
    assert "OUTSIDE_RACE_SECRET" not in context["text"]


def test_code_context_rejects_broad_roots():
    with pytest.raises(ValueError, match="too broad"):
        gather.gather_code_context(Path.home())
    with pytest.raises(ValueError, match="too broad"):
        gather.gather_code_context(Path(Path.home().anchor))


def test_project_root_validation_rejects_broad_ancestor_and_system_roots():
    import tempfile

    candidates = {
        Path.home().resolve().parent,
        Path(tempfile.gettempdir()).resolve(),
    }
    for candidate in (Path("/private/var"), Path("/private/tmp")):
        if candidate.is_dir():
            candidates.add(candidate.resolve())

    for candidate in candidates:
        with pytest.raises(ValueError, match="too broad"):
            gather.validate_project_root(candidate)


def test_code_context_skips_oversized_files(tmp_path):
    (tmp_path / "small.py").write_text("SMALL_MARKER = 1\n", encoding="utf-8")
    (tmp_path / "large.txt").write_text("LARGE_SECRET\n" * 100_000, encoding="utf-8")

    context = gather.gather_code_context(tmp_path, depth="deep")

    assert "small.py" in context["files"]
    assert "large.txt" not in context["files"]
    assert "LARGE_SECRET" not in context["text"]


def test_code_context_reads_candidates_lazily(tmp_path, monkeypatch):
    for index in range(1_050):
        (tmp_path / f"module_{index:03}.py").write_text(
            f"VALUE_{index} = {index}\n",
            encoding="utf-8",
        )
    original = gather._read_code_text
    reads = []

    def recording_read(*args, **kwargs):
        reads.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(gather, "_read_code_text", recording_read)
    context = gather.gather_code_context(tmp_path, depth="shallow")

    assert context["candidate_count"] == 1_000
    assert context["candidate_truncated"] is True
    assert len(reads) <= 25


def test_non_git_gather_does_not_use_materializing_os_walk(tmp_path, monkeypatch):
    (tmp_path / "visible.py").write_text("VISIBLE = 1\n", encoding="utf-8")

    def fail_walk(*_args, **_kwargs):
        raise AssertionError("os.walk materializes an unbounded directory listing")

    monkeypatch.setattr(repository_facts.os, "walk", fail_walk)

    context = gather.gather_code_context(tmp_path, depth="shallow")
    facts = gather.gather_durable_facts(tmp_path)

    assert "visible.py" in context["files"]
    assert "visible.py" in facts["repository_files"]


def test_git_state_and_complete_context_respect_max_chars(tmp_path):
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-F",
            "-",
        ],
        cwd=tmp_path,
        input="G" * 200_000,
        text=True,
        check=True,
        capture_output=True,
    )

    context = gather.gather_code_context(
        tmp_path,
        depth="shallow",
        max_chars=1_000,
    )

    assert len(context["text"]) <= 1_000
    assert context["text_char_limit"] == 1_000
    assert context["git"]["output_truncated"] is True


def test_public_gather_git_anchors_root_before_commands(tmp_path, monkeypatch):
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    inside_name = "INSIDE_STATUS.py"
    (root / inside_name).write_text("INSIDE = 1\n", encoding="utf-8")

    replacement = tmp_path / "replacement"
    replacement.mkdir()
    subprocess.run(["git", "init"], cwd=replacement, check=True, capture_output=True)
    outside_name = "OUTSIDE_REPLACEMENT_SECRET.py"
    (replacement / outside_name).write_text("OUTSIDE = 1\n", encoding="utf-8")
    moved_root = tmp_path / "repo-original"
    original_run = gather._run_bounded
    swapped = False

    def swap_before_first_git_command(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            root.rename(moved_root)
            replacement.rename(root)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(gather, "_run_bounded", swap_before_first_git_command)

    result = gather.gather_git(root)

    assert result["ok"] is True
    assert inside_name in result["status"]
    assert outside_name not in result["status"]


def test_git_file_stream_stops_on_oversized_unterminated_record(tmp_path, monkeypatch):
    class FakeStdout:
        def __init__(self):
            self.read_calls = 0

        def read(self, _size):
            self.read_calls += 1
            if self.read_calls <= 8:
                return b"x" * (64 * 1024)
            return b""

        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.stdout = FakeStdout()
            self.terminated = False

        def poll(self):
            return None if not self.terminated else -15

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

        def wait(self, timeout=None):
            return -15 if self.terminated else 0

    fake_process = FakeProcess()
    monkeypatch.setattr(repository_facts, "_git_root", lambda root: root)
    monkeypatch.setattr(
        repository_facts.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake_process,
    )

    paths, truncated = repository_facts.git_repository_files(
        tmp_path,
        max_entries=10,
        max_path_bytes=1_000,
    )

    assert paths == []
    assert truncated is True
    assert fake_process.stdout.read_calls <= 2


def test_git_file_stream_times_out_when_child_never_produces_output(
    tmp_path,
    monkeypatch,
):
    class HangingStdout:
        def fileno(self):
            return 123

        def close(self):
            return None

    class HangingProcess:
        def __init__(self):
            self.stdout = HangingStdout()
            self.terminated = False

        def poll(self):
            return None if not self.terminated else -15

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

        def wait(self, timeout=None):
            return -15 if self.terminated else 0

    class NeverReadySelector:
        def register(self, *_args, **_kwargs):
            return None

        def select(self, timeout=None):
            return []

        def close(self):
            return None

    fake_process = HangingProcess()
    monotonic_values = iter([0.0, 1.0])
    monkeypatch.setattr(repository_facts, "_git_root", lambda root: root)
    monkeypatch.setattr(
        repository_facts.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake_process,
    )
    monkeypatch.setattr(
        repository_facts.selectors,
        "DefaultSelector",
        NeverReadySelector,
    )
    monkeypatch.setattr(
        repository_facts.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    with pytest.raises(ValueError, match="timed out"):
        repository_facts.git_repository_files(
            tmp_path,
            max_entries=10,
            max_path_bytes=1_000,
            timeout=0.1,
        )

    assert fake_process.terminated is True


def test_deep_code_context_reads_a_focused_key_file_completely(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    lines = [f"VALUE_{index} = {index}" for index in range(1, 420)]
    lines[380] = "def gather_code_context():"
    lines[381] = "    return 'late implementation'"
    target = src / "gather.py"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")

    context = gather.gather_code_context(
        tmp_path,
        depth="deep",
        focus="Inspect src/gather.py and gather_code_context before writing docs.",
    )

    assert "src/gather.py" in context["complete_files"]
    assert "   381 | def gather_code_context():" in context["text"]
    assert any(
        segment["path"] == "src/gather.py" and segment["mode"] == "complete"
        for segment in context["evidence_segments"]
    )


def test_deep_code_context_uses_late_symbol_windows_for_very_large_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    lines = [f"entry_{index} = '{'x' * 48}'" for index in range(1, 1_401)]
    lines[1_250] = "def critical_symbol_for_docs():"
    lines[1_251] = "    return 'keep this evidence'"
    target = src / "large_worker.py"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    context = gather.gather_code_context(
        tmp_path,
        depth="deep",
        focus="Review large_worker.py critical_symbol_for_docs.",
    )

    assert "src/large_worker.py" in context["partial_files"]
    assert "  1251 | def critical_symbol_for_docs():" in context["text"]
    assert any(
        segment["path"] == "src/large_worker.py"
        and segment["mode"] == "focused_window"
        and segment["start_line"] <= 1_251 <= segment["end_line"]
        for segment in context["evidence_segments"]
    )


def test_focused_windows_prioritize_named_symbols_over_frequent_generic_matches(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    lines = [f"GEMINI_VALUE_{index} = '{'x' * 48}'" for index in range(1, 1_901)]
    lines[1_800] = "def status(*, probe: bool = False):"
    lines[1_801] = "    return agy_auth.status(probe=probe)"
    target = src / "antigravity_api.py"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    context = gather.gather_code_context(
        tmp_path,
        depth="deep",
        focus="Verify Gemini status refresh.",
        max_chars=12_000,
    )

    assert "  1801 | def status(*, probe: bool = False):" in context["text"]
    assert "  1802 |     return agy_auth.status(probe=probe)" in context["text"]


def test_verify_flags_recency():
    result = verify.verify_text("today we fixed HTTP 400 in this session", doc_class="durable")
    assert result["warning_count"] >= 1
    assert any("recency" in w for w in result["warnings"])


def test_verify_blocks_known_korean_translationese():
    result = verify.verify_text(
        "# 안내\n\n이전 이름은 지원하지 않습니다.",
        doc_class="durable",
    )
    assert result["ok"] is False
    assert any(w.startswith("korean_style:translation_like") for w in result["warnings"])


def test_verify_flags_stiff_user_facing_readme_style():
    result = verify.verify_text(
        "# 안내\n\n"
        "이 시스템은 콕핏을 제공한다.\n"
        "작업을 실행한다.\n"
        "결과를 저장한다.\n"
        "설정을 관리한다.",
        doc_class="durable",
        user_facing=True,
    )

    assert result["ok"] is False
    assert any("unexplained_jargon" in warning for warning in result["warnings"])
    assert any("declarative_monologue_density" in warning for warning in result["warnings"])


def test_start_run_auto_gather_and_next_leaf(tmp_path):
    (tmp_path / "pyproject.toml").write_text('version = "9.9.9"\n', encoding="utf-8")
    state = runner.start_run(
        "durable_readme",
        args={"prompt": "Write a short README"},
        project_root=str(tmp_path),
    )
    assert state["run_id"]
    # gather auto-completed
    gather_step = state["steps"][0]
    assert gather_step["status"] == "completed"
    assert "9.9.9" in (state.get("artifacts") or {}).get("facts_text", "")
    nxt = state["next_action"]
    assert nxt["type"] == "call_tool"
    assert nxt["tool"] == "google_antigravity_write"
    args = nxt.get("arguments") or {}
    assert args.get("task") == "readme"
    assert args.get("project_root")
    assert "prompt" not in args  # write leaf shape, not chat shape


def test_write_falls_back_to_chat_and_morphs_args(tmp_path):
    (tmp_path / "pyproject.toml").write_text('version = "2.0.0"\n', encoding="utf-8")
    state = runner.start_run(
        "durable_readme",
        args={"prompt": "Write README"},
        project_root=str(tmp_path),
    )
    assert state["next_action"]["tool"] == "google_antigravity_write"
    # Antigravity write fails -> fallback rotates to a chat leaf; args must reshape.
    state2 = runner.continue_run(run_id=state["run_id"], success=False, error="quota")
    nxt = state2["next_action"]
    assert state2["status"] == "running"
    assert nxt["tool"] == "claude_codex_chat"
    assert "task" not in nxt["arguments"]
    assert "FACT PACK" in nxt["arguments"].get("prompt", "")


def test_manual_local_stage_completes_run(tmp_path):
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n', encoding="utf-8")
    state = runner.start_run(
        "durable_readme",
        args={"prompt": "x"},
        project_root=str(tmp_path),
        auto_local=False,
    )
    # gather (local) not auto-run; advance it manually
    runner.continue_run(run_id=state["run_id"], auto_local=False)
    # draft (write) — complete it
    runner.continue_run(
        run_id=state["run_id"],
        stage_id="draft",
        result_text="# README",
        success=True,
        auto_local=False,
    )
    # verify (local) is now current; continuing must complete the run, not hang in "running"
    final = runner.continue_run(run_id=state["run_id"], auto_local=False)
    assert final["status"] == "completed"
    assert final["done"] is True


def test_missing_prompt_is_warned(tmp_path):
    state = runner.start_run("direct_chat", args={}, project_root=str(tmp_path))
    assert any("missing_prompt" in w for w in state.get("warnings", []))


def test_tools_call_wraps_mcp_content():
    resp = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "orchestrate_list_recipes", "arguments": {}},
        }
    )
    res = resp["result"]
    assert res["content"][0]["type"] == "text"
    assert res["isError"] is False
    assert res["success"] is True  # flat fields retained for supervised handoff


def test_continue_with_fallback_on_failure(tmp_path):
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n', encoding="utf-8")
    state = runner.start_run(
        "direct_chat",
        args={"prompt": "hi"},
        project_root=str(tmp_path),
    )
    assert state["next_action"]["tool"] == "claude_codex_chat"
    state2 = runner.continue_run(
        run_id=state["run_id"],
        success=False,
        error="capacity",
    )
    # still same step, fallback tool
    assert state2["status"] == "running"
    assert state2["next_action"]["tool"] == "grok_codex_chat"


def test_continue_success_completes_direct(tmp_path):
    state = runner.start_run("direct_chat", args={"prompt": "hi"}, project_root=str(tmp_path))
    state2 = runner.continue_run(
        run_id=state["run_id"],
        stage_id="chat",
        result_text="hello world",
        success=True,
    )
    assert state2["done"] is True
    assert state2["status"] == "completed"
