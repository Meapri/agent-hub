"""File-backed run store so supervised runs survive an MCP process restart.

In-process memory stays the fast path; this mirrors each run to disk so
`orchestrate_get_run` / `orchestrate_continue_recipe` keep working after the
stdio server is relaunched. Zero dependencies — plain JSON files.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENV_DIR = "ORCHESTRATE_CODEX_STATE_DIR"


def state_dir() -> Path:
    override = os.environ.get(_ENV_DIR)
    base = Path(override).expanduser() if override else Path.home() / ".orchestrate_codex" / "runs"
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except OSError:
        # Home not writable (sandboxes) — fall back to a temp dir.
        fallback = Path(tempfile.gettempdir()) / "orchestrate_codex_runs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _path(run_id: str) -> Path:
    return state_dir() / f"{run_id}.json"


def save(state: Dict[str, Any]) -> None:
    run_id = str(state.get("run_id") or "")
    if not run_id:
        return
    try:
        tmp = _path(run_id).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _path(run_id))  # atomic on POSIX
    except (OSError, TypeError, ValueError):
        pass  # persistence is best-effort; memory store still holds the run


def load(run_id: str) -> Optional[Dict[str, Any]]:
    if not run_id:
        return None
    try:
        return json.loads(_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_run_ids() -> List[str]:
    try:
        return sorted(p.stem for p in state_dir().glob("*.json"))
    except OSError:
        return []


def delete(run_id: str) -> None:
    try:
        _path(run_id).unlink(missing_ok=True)
    except OSError:
        pass
