"""Durable document fact packs for product docs (README / technical-doc).

Leaf-side helper aligned with orchestrate-codex durable policy:
version, skills, MCP tool names — never git diary or session work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from orchestrate_codex.gather import gather_durable_facts


def collect_durable_facts(project_root: str | Path) -> Dict[str, Any]:
    """Use the canonical bounded fact collector for every durable writer."""

    return gather_durable_facts(project_root)
