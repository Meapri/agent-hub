"""What a caller can learn from the tool schema before making a mistake.

This surface is consumed by other agents over MCP, which show them the schema
and nothing else. project_root is required by nine of the fourteen tools and
must be an absolute path to a directory that already exists -- and it was
declared as a bare {"type": "string"}. A caller passing "." got
invalid_project_root with no way to know what was wanted, which is what three
consecutive failures in this machine's metrics look like from the inside.
"""

from __future__ import annotations

import pytest

from agent_hub.v2.contracts import canonical_project_root
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.tools import tool_definitions

TOOLS_WITH_PROJECT_ROOT = [
    tool["name"]
    for tool in tool_definitions()
    if "project_root" in tool["inputSchema"]["properties"]
]


def test_the_surface_is_still_exactly_fourteen_tools():
    assert len(tool_definitions()) == 14


@pytest.mark.parametrize("name", TOOLS_WITH_PROJECT_ROOT)
def test_every_tool_taking_project_root_says_what_it_must_be(name):
    tool = next(item for item in tool_definitions() if item["name"] == name)
    described = tool["inputSchema"]["properties"]["project_root"].get("description", "")

    assert "Absolute" in described
    # The specific mistake worth naming, because it is the one callers make.
    assert '"."' in described


def test_the_description_matches_what_validation_actually_enforces(tmp_path):
    # A description that drifts from the check is worse than none: it tells the
    # caller a rule the code does not apply.
    assert canonical_project_root(str(tmp_path)) == str(tmp_path.resolve())

    with pytest.raises(HubV2Error) as relative:
        canonical_project_root(".")
    assert relative.value.code == "invalid_project_root"

    with pytest.raises(HubV2Error) as missing:
        canonical_project_root(str(tmp_path / "nope"))
    assert missing.value.code == "invalid_project_root"

    file_path = tmp_path / "a-file"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(HubV2Error) as not_a_dir:
        canonical_project_root(str(file_path))
    assert not_a_dir.value.code == "invalid_project_root"


def test_at_least_half_the_surface_needs_it_so_the_gap_mattered():
    assert len(TOOLS_WITH_PROJECT_ROOT) >= 7
