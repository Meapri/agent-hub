"""Contract test: the args this orchestrator sends must be valid for the *real* leaf
schemas, which live in sibling plugin repos (Antigravity/Claude/Grok Codex).

These four plugins evolve independently. If a leaf renames a field or tightens its
schema, the orchestrator would silently send rejected calls. This test reads each
leaf's `inputSchema` straight from its `mcp_server.py` and validates the arguments
`runner` produces for each domain recipe against it. Skips cleanly if the sibling
repos aren't checked out next to this one.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

import pytest

from orchestrate_codex import runner

_SIBLINGS = Path(__file__).resolve().parents[2]
_LEAF_FILES = {
    "antigravity": _SIBLINGS / "Antigravity Codex" / "google_antigravity_codex" / "mcp_server.py",
    "claude": _SIBLINGS / "Claude Codex" / "claude_codex" / "mcp_server.py",
    "grok": _SIBLINGS / "Grok Codex" / "grok_codex" / "mcp_server.py",
}


def _schema_dict_node(module: ast.Module, var_name: str) -> Optional[ast.Dict]:
    for node in module.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id == var_name and isinstance(node.value, ast.Dict):
                    return node.value
    return None


def _dict_get(dnode: ast.Dict, key: str) -> Optional[ast.AST]:
    for k, v in zip(dnode.keys, dnode.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    return None


def _prop_keys(module: ast.Module, var_name: str, _seen: Optional[Set[str]] = None) -> Tuple[Set[str], list, Optional[bool]]:
    """Return (property keys, required list, additionalProperties bool) for a *_SCHEMA var."""
    _seen = _seen or set()
    node = _schema_dict_node(module, var_name)
    if node is None or var_name in _seen:
        return set(), [], None
    _seen.add(var_name)
    props: Set[str] = set()
    props_node = _dict_get(node, "properties")
    if isinstance(props_node, ast.Dict):
        for k in props_node.keys:
            if isinstance(k, ast.Constant):
                props.add(str(k.value))
            elif k is None:  # ** spread, e.g. **OTHER_SCHEMA["properties"]
                pass
        # resolve **OTHER_SCHEMA["properties"] spreads
        for k, v in zip(props_node.keys, props_node.values):
            if k is None and isinstance(v, ast.Subscript) and isinstance(v.value, ast.Name):
                more, _, _ = _prop_keys(module, v.value.id, _seen)
                props |= more
    required = []
    req_node = _dict_get(node, "required")
    if isinstance(req_node, ast.List):
        required = [c.value for c in req_node.elts if isinstance(c, ast.Constant)]
    add_node = _dict_get(node, "additionalProperties")
    additional = add_node.value if isinstance(add_node, ast.Constant) else None
    return props, required, additional


def _tool_schema_var(source: str, tool: str) -> Optional[str]:
    """Find the inputSchema variable bound to a tool name in tool_definitions()."""
    import re

    m = re.search(rf'"name":\s*"{re.escape(tool)}"(.*?)"inputSchema":\s*([A-Za-z_][A-Za-z0-9_]*)', source, re.S)
    return m.group(2) if m else None


def _validate(leaf_key: str, tool: str, args: Dict[str, Any]) -> None:
    path = _LEAF_FILES[leaf_key]
    if not path.is_file():
        pytest.skip(f"sibling leaf repo not present: {path}")
    source = path.read_text(encoding="utf-8")
    var = _tool_schema_var(source, tool)
    assert var, f"could not find inputSchema for {tool} in {path}"
    module = ast.parse(source)
    props, required, additional = _prop_keys(module, var)
    assert props, f"no properties parsed for {var}"
    arg_keys = set(args)
    if additional is False:
        unknown = arg_keys - props
        assert not unknown, f"{tool}: orchestrator sends keys the leaf rejects: {sorted(unknown)}"
    missing = set(required) - arg_keys
    assert not missing, f"{tool}: missing required args {sorted(missing)}"


def _args(recipe: str, tmp_path, **extra) -> Tuple[str, Dict[str, Any]]:
    import subprocess

    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n', encoding="utf-8")
    # change-class recipes auto-run a local gather_git; give them a real repo.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=False)
    state = runner.start_run(recipe, args={"prompt": "go", **extra}, project_root=str(tmp_path))
    na = state["next_action"]
    return na["tool"], na.get("arguments") or {}


CASES = [
    ("durable_readme", "antigravity", {}),
    ("change_pr", "antigravity", {}),
    ("translate_doc", "antigravity", {"source_text": "hi", "target_language": "Korean"}),
    ("generate_image", "antigravity", {}),
    ("compare_models", "antigravity", {"models": ["a", "b"]}),
    ("review_diff", "antigravity", {"focus": "security"}),
    ("release_draft", "antigravity", {"version": "1.2.0"}),
    ("direct_chat", "claude", {}),
]


@pytest.mark.parametrize("recipe,leaf,extra", CASES)
def test_orchestrator_args_match_leaf_schema(recipe, leaf, extra, tmp_path):
    tool, args = _args(recipe, tmp_path, **extra)
    _validate(leaf, tool, args)


def test_write_chat_fallback_matches_claude_schema(tmp_path):
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n', encoding="utf-8")
    state = runner.start_run("durable_readme", args={"prompt": "go"}, project_root=str(tmp_path))
    out = runner.continue_run(run_id=state["run_id"], success=False, error="quota")
    na = out["next_action"]
    assert na["tool"] == "claude_codex_chat"
    _validate("claude", na["tool"], na["arguments"])
