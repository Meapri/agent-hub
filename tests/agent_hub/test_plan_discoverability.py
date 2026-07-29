"""What agent_hub_plan tells a caller before they get it wrong.

Of 142 calls on this machine, 31% failed, and the failures were argument-shaped
rather than provider-shaped: invalid_request, the prepare/apply digest
handshake, and source_paths rejections. The schema declared `source_paths` as an
array of strings, `proposal` as an object, and said nothing about which
arguments belong to which mode.

Each description below is checked against the code that enforces it. A
description that drifts from the check is worse than none: it tells the caller a
rule the runtime does not apply.
"""

from __future__ import annotations

import pytest

from agent_hub.v2 import egress
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.tools import tool_definitions


@pytest.fixture(scope="module")
def plan_schema():
    tool = next(item for item in tool_definitions() if item["name"] == "agent_hub_plan")
    return tool["inputSchema"]["properties"]


def test_the_description_says_prepare_comes_first():
    tool = next(item for item in tool_definitions() if item["name"] == "agent_hub_plan")

    assert "prepare" in tool["description"]
    assert "apply" in tool["description"]


def test_every_apply_only_argument_says_so(plan_schema):
    for name in ("proposal", "proposal_sha256", "expected_policy_revision", "approval_request_id"):
        assert "apply only" in plan_schema[name]["description"], name


def test_source_paths_says_prepare_only(plan_schema):
    assert "prepare only" in plan_schema["source_paths"]["description"]


# --- the descriptions, against what actually enforces them ------------------


def test_source_paths_really_must_be_project_relative(tmp_path, plan_schema):
    described = plan_schema["source_paths"]["items"]["description"]
    assert "relative to project_root" in described
    assert "'..'" in described

    with pytest.raises(HubV2Error) as absolute:
        egress._source_path(tmp_path, "/etc/hosts")  # noqa: SLF001
    assert absolute.value.code == "invalid_source_path"

    with pytest.raises(HubV2Error) as escaping:
        egress._source_path(tmp_path, "../outside.txt")  # noqa: SLF001
    assert escaping.value.code == "invalid_source_path"


@pytest.mark.parametrize("name", [".env", ".env.local", "key.pem", "secret.key", "id_rsa"])
def test_the_credential_shapes_named_are_the_ones_refused(tmp_path, name):
    with pytest.raises(HubV2Error) as refused:
        egress._source_path(tmp_path, name)  # noqa: SLF001

    assert refused.value.code == "sensitive_source_denied"


def test_the_stated_limits_match_the_constants(plan_schema):
    assert plan_schema["source_paths"]["maxItems"] == egress.MAX_SOURCE_FILES
    assert egress.MAX_SOURCE_FILES == 100
    # "2 MB each" in the description.
    assert egress.MAX_SOURCE_BYTES == 2 * 1024 * 1024
    assert "2 MB" in plan_schema["source_paths"]["description"]


def test_the_digest_fence_is_described_because_editing_the_proposal_breaks_it(plan_schema):
    described = plan_schema["proposal_sha256"]["description"]

    assert "digest" in described
    assert "unchanged" in plan_schema["proposal"]["description"]


def test_the_surface_is_still_fourteen_tools():
    assert len(tool_definitions()) == 14
