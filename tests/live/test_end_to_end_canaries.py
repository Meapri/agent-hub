"""The paths a person actually drives, end to end, against real providers.

These go through the daemon's own dispatch rather than calling an adapter, so
they cover the seams between them: argument validation, policy, image
resolution, the worker boundary and the sandbox.
"""

from __future__ import annotations

import base64
import secrets
import time

import pytest

from agent_hub.v2.crypto import ArtifactCipher, MacOSKeychainKeyProvider
from agent_hub.v2.service import HubService
from agent_hub.v2.store import HubStore

pytestmark = pytest.mark.live


@pytest.fixture
def service(tmp_path):
    return HubService(
        HubStore(tmp_path / "state.sqlite3"),
        cipher=ArtifactCipher(MacOSKeychainKeyProvider()),
    )


def test_execute_reads_an_image_from_a_relative_path(service, canary_project, require_provider):
    """Two fixes at once.

    The tool schema says input_images takes "a path to a file inside
    project_root", and a relative path used to resolve against the daemon's own
    working directory instead -- so following the documentation failed.
    """

    require_provider("gemini")

    result = service.dispatch(
        "agent_hub_execute",
        {
            "provider": "gemini",
            "project_root": str(canary_project),
            "task": {
                "capability": "vision",
                "intent": "Describe this image in one sentence.",
                "input_images": ["canary.png"],
                "constraints": {"provider_allowlist": ["gemini"], "timeout_seconds": 180},
            },
        },
    )

    assert result["success"] is True, result.get("error")
    assert str(result["data"]["result"]["text"]).strip()


def test_a_generated_image_survives_a_durable_run(service, tmp_path, require_provider):
    """From provider temp file to encrypted artifact to bytes you can open.

    execute refuses image because a picture is not an inline answer, so this is
    the supported path -- and the one that has to keep working.
    """

    require_provider("gemini")

    plan = {
        "schema": "plan_v2",
        "task": {
            "schema": "task_v2",
            "capability": "image",
            "intent": "A plain solid red square.",
            "inline_input": "",
            "retention": "durable_private",
        },
        "steps": [
            {
                "id": "draw",
                "capability": "image",
                "instruction": "A plain solid red square on a white background.",
                "routing_requirements": {"planner_provider": "gemini"},
            }
        ],
        "routing_mode": "pinned",
        "policy_revision": 0,
    }
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": plan,
            "project_root": str(tmp_path),
            "idempotency_key": f"live.image.{secrets.token_hex(4)}",
        },
    )
    assert started["success"] is True, started.get("error")
    run_id = started["data"]["run_id"]
    service.dispatch("agent_hub_continue", {"run_id": run_id, "expected_revision": 0})

    deadline = time.time() + 300
    run = service.store.get_run(run_id)
    while run["status"] == "running" and time.time() < deadline:
        time.sleep(1.0)
        run = service.store.get_run(run_id)

    assert run["status"] == "completed", run["steps"][0]["checkpoint"]
    step = run["steps"][0]
    assert step["output_artifact_ids"], "the image produced no artifact"

    stored = service.store.get_artifact(step["output_artifact_ids"][0], include_content=False)
    assert stored["media_type"].startswith("image/")

    fetched = service.dispatch(
        "agent_hub_artifact",
        {
            "action": "get",
            "artifact_id": step["output_artifact_ids"][0],
            "include_base64": True,
            "project_root": str(tmp_path),
        },
    )
    raw = base64.b64decode(fetched["data"]["base64"])
    assert len(raw) > 1000, "the stored image is too small to be a picture"


def test_the_setup_gui_connection_test_passes(require_provider):
    """The break the user actually hit.

    agent_hub_execute requires project_root and ConnectionManager never sent
    one, so every provider failed. Five unit tests covered start_test and all
    of them passed, because the daemon call is only built for the real status
    reader.
    """

    from agent_hub.connect_service import ConnectionManager

    manager = ConnectionManager()
    try:
        assert manager._use_daemon is True, "this canary must exercise the daemon path"  # noqa: SLF001
        checked = 0
        for provider in ("claude", "grok", "gemini", "gpt"):
            state = manager.status(provider)["providers"][provider]
            if not (state["login_ready"] or state["invocation_ready"]):
                continue
            started = manager.start_test(provider)
            job = manager.job(started["id"])
            deadline = time.time() + 240
            while job["state"] == "working" and time.time() < deadline:
                time.sleep(0.5)
                job = manager.job(started["id"])
            assert job["state"] == "complete", (provider, job.get("message"))
            # Every provider's message used to say "Gemini".
            assert (
                job["message"]
                .split()[0]
                .lower()
                .startswith(
                    {"claude": "claude", "grok": "grok", "gemini": "gemini", "gpt": "gpt"}[provider]
                )
            ), job["message"]
            checked += 1
        assert checked, "no provider was ready; connect one before trusting this canary"
    finally:
        manager.close()
