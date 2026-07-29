"""What agent_hub_handoff tells a caller, and what it says when they get it wrong.

Of 76 calls on this machine, 12 failed, and 6 of those came back as
`internal_error` -- the runtime reporting an unexplained fault for what was
actually a missing or misnamed argument. The tool published `arguments` as a
bare object, so the caller had no way to learn that apply_update needs `file`
and `content` rather than the `body` prepare_update takes.

Two rules are checked here. Every argument-shape refusal names the field the
caller has to fix, and every description is checked against the code that
enforces it.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from agent_hub.core import handoff
from agent_hub.v2.service import HubService
from agent_hub.v2.tools import tool_definitions


@pytest.fixture(scope="module")
def handoff_arguments():
    tool = next(item for item in tool_definitions() if item["name"] == "agent_hub_handoff")
    return tool["inputSchema"]["properties"]["arguments"]


# --- a wrong argument must not look like a runtime fault --------------------


def test_a_missing_file_names_the_field_instead_of_failing_internally():
    with pytest.raises(handoff.HandoffArgumentError) as refused:
        handoff.apply_handoff_update(
            ".",
            file="",
            content="",
            expected_sha256=None,
        )

    assert refused.value.field == "file"
    assert "prepare_update" in str(refused.value)


def test_sending_the_body_where_content_belongs_says_so(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "HANDOFF.md").write_text("placeholder\n", encoding="utf-8")

    with pytest.raises(handoff.HandoffArgumentError) as refused:
        handoff.apply_handoff_update(
            tmp_path,
            file=str(tmp_path / "HANDOFF.md"),
            content="- **다음 한 걸음**: 무언가 하세요.\n",
            expected_sha256=None,
        )

    assert refused.value.field == "content"
    assert "body" in str(refused.value)


def test_a_prepared_expected_sha256_is_the_whole_file_one():
    with pytest.raises(handoff.HandoffArgumentError) as refused:
        handoff.apply_handoff_update(
            ".",
            file="HANDOFF.md",
            content="",
            expected_sha256="not-a-digest",
        )

    assert refused.value.field == "expected_sha256"


def test_the_runtime_turns_an_argument_error_into_invalid_request_with_the_field():
    public = HubService._safe_handoff_error(  # noqa: SLF001
        handoff.HandoffArgumentError("file is required.", field="file")
    )

    assert public.code == "invalid_request"
    assert public.safe_details["field"] == "file"
    # Without this the caller reads "The HANDOFF.md request is invalid." and
    # still does not know which argument to change.
    assert "file" in public.message


def test_a_limit_refusal_reports_the_limit():
    public = HubService._safe_handoff_error(  # noqa: SLF001
        handoff.HandoffArgumentError("body is too long.", field="body", limit=4096)
    )

    assert public.safe_details == {"field": "body", "limit": 4096}


def test_every_argument_message_is_a_literal_so_it_can_be_handed_back():
    """The runtime returns these messages verbatim.

    That is only safe because each one is authored here. An f-string could
    interpolate a path or a body excerpt into a public error.
    """

    tree = ast.parse(pathlib.Path(inspect.getfile(handoff)).read_text(encoding="utf-8"))
    raised = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "HandoffArgumentError"
    ]

    assert len(raised) >= 9
    for node in raised:
        message = node.exc.args[0]
        parts = message.values if isinstance(message, ast.JoinedStr) else [message]
        assert all(isinstance(part, ast.Constant) for part in parts), ast.unparse(node)


# --- the descriptions, against what actually enforces them ------------------


def test_the_schema_documents_every_argument_the_runtime_reads(handoff_arguments):
    source = inspect.getsource(HubService._tool_handoff)  # noqa: SLF001
    source += inspect.getsource(HubService._handoff_diff)  # noqa: SLF001
    tree = ast.parse(source.replace("    def ", "def ", 1).replace("\n    ", "\n"))

    read = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "extra"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }

    documented = set(handoff_arguments["properties"])
    assert read - documented == set()
    # additionalProperties is False, so anything undocumented is now refused at
    # the caller rather than silently ignored here.
    assert handoff_arguments["additionalProperties"] is False


def test_apply_only_and_prepare_only_arguments_say_which(handoff_arguments):
    described = handoff_arguments["properties"]

    for name in ("content", "expected_sha256"):
        assert "apply_update only" in described[name]["description"], name
    for name in ("body", "base_managed_sha256", "include_diff"):
        assert "prepare_update only" in described[name]["description"], name
    assert "takeover only" in described["run_id"]["description"]


def test_the_described_search_defaults_are_the_real_ones(handoff_arguments):
    described = handoff_arguments["properties"]["search"]["description"]
    assert "prepare_update defaults to project-only" in described

    source = inspect.getsource(HubService._tool_handoff)  # noqa: SLF001
    prepare, _, reads = source.partition('if action == "prepare_update"')
    assert '"search": str(extra.get("search") or "project-only")' in reads
    assert 'extra.get("search") or "nearest"' in prepare


def test_the_described_marker_rule_is_the_enforced_one(handoff_arguments):
    assert "Agent Hub adds them" in handoff_arguments["properties"]["body"]["description"]

    with pytest.raises(handoff.HandoffArgumentError) as refused:
        handoff._marker_block(f"{handoff.START_MARKER}\n- 무언가\n{handoff.END_MARKER}")  # noqa: SLF001

    assert refused.value.field == "body"


def test_the_surface_is_still_fourteen_tools():
    assert len(tool_definitions()) == 14
