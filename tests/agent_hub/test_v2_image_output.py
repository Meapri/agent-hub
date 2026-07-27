"""A generated image, from the provider's temp file to something you can open.

What this replaces: the artifact used to hold the sentence
"Generated image: /Users/you/.cache/grok-codex/images/grok_abc.png". That is a
note about a file, not the file. The picture itself stayed in a directory
nothing in this repository ever cleaned up, and callers of agent_hub_execute
were handed an absolute path into the user's home directory.

These tests pin the three properties that fix implies: the bytes reach the
store, the temp file is gone afterwards, and the path never leaves the machine.
"""

from __future__ import annotations

import base64
import secrets
import struct
import time
import zlib

import pytest

from agent_hub.v2 import provider_runtime
from agent_hub.v2.contracts import PLAN_SCHEMA, TASK_SCHEMA, validate_plan
from agent_hub.v2.crypto import ArtifactCipher, StaticKeyProvider
from agent_hub.v2.errors import HubV2Error
from agent_hub.v2.provider_worker import _collect_generated_image
from agent_hub.v2.service import HubService
from agent_hub.v2.store import HubStore


def _png(width: int = 2, height: int = 2) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x11\x22\x33" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def image_cache(tmp_path, monkeypatch):
    """Stand in for the provider's own image cache directory."""

    root = tmp_path / "provider-cache" / "images"
    root.mkdir(parents=True)
    monkeypatch.setattr(provider_runtime, "generated_image_root", lambda _provider: root)
    return root


def _adapter_result(path, *, mime="image/png"):
    """The envelope shape both image adapters produce today."""

    return {
        "success": True,
        "operation": "generate_image",
        "provider": "grok",
        "model": "grok-imagine-image",
        "text": f"Generated image: {path}",
        "usage": {},
        "warnings": [],
        "error": None,
        "artifacts": [],
        "data": {
            "success": True,
            "text": f"Generated image: {path}",
            "image": str(path),
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "mime_type": mime,
            "model": "grok-imagine-image",
            "prompt": "a red square",
        },
    }


# --- the worker boundary -----------------------------------------------------


def test_the_worker_carries_the_bytes_out_and_deletes_the_file(image_cache):
    written = image_cache / "grok_abc.png"
    written.write_bytes(_png())

    collected = _collect_generated_image("grok", _adapter_result(written))

    assert base64.b64decode(collected["image_base64"]) == _png()
    assert collected["image_media_type"] == "image/png"
    assert collected["image_bytes"] == len(_png())
    # The file was a courier, not a destination.
    assert not written.exists()


def test_the_absolute_path_does_not_survive_collection(image_cache):
    written = image_cache / "grok_abc.png"
    written.write_bytes(_png())

    collected = _collect_generated_image("grok", _adapter_result(written))

    assert str(written) not in str(collected)
    assert "path" not in collected["data"]
    assert "image" not in collected["data"]


def test_a_path_outside_the_provider_cache_is_refused(image_cache, tmp_path):
    # The path arrives inside a provider response. Reading whatever it names
    # would turn a provider reply into an arbitrary file read.
    elsewhere = tmp_path / "not-the-cache.png"
    elsewhere.write_bytes(_png())

    with pytest.raises(HubV2Error) as refused:
        _collect_generated_image("grok", _adapter_result(elsewhere))

    assert refused.value.code == "provider_protocol_error"
    assert elsewhere.exists(), "a refused path must not be deleted either"


def test_a_response_with_no_output_file_is_a_protocol_error(image_cache):
    result = {"success": True, "text": "done", "data": {"prompt": "x"}}

    with pytest.raises(HubV2Error) as missing:
        _collect_generated_image("grok", result)

    assert missing.value.code == "provider_protocol_error"


def test_the_suffix_wins_over_an_adapter_that_guesses_wrong(image_cache):
    # The grok adapter reports image/jpeg for anything that is not .png, so a
    # saved .webp is announced as a JPEG. The file knows better.
    written = image_cache / "grok_abc.webp"
    written.write_bytes(_png())

    collected = _collect_generated_image("grok", _adapter_result(written, mime="image/jpeg"))

    assert collected["image_media_type"] == "image/webp"


# --- the run, end to end -----------------------------------------------------


class _ImageWorker:
    cache_root = None

    def __init__(self, provider):
        self.provider = provider

    def request(self, method, params=None, timeout=30.0, request_id=None):
        if method == "status":
            return {"success": True, "data": {"providers": {self.provider: {"ready": True}}}}
        if method == "catalog":
            return {"success": True, "warnings": [], "data": {"models": {}}}
        if method == "invoke":
            written = type(self).cache_root / f"{self.provider}_{secrets.token_hex(4)}.png"
            written.write_bytes(_png())
            return _collect_generated_image(self.provider, _adapter_result(written))
        raise AssertionError(method)

    def cancel(self):
        return True


def _image_plan():
    return validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "capability": "image",
                "intent": "A red square.",
                "inline_input": "",
                "retention": "durable_private",
            },
            "steps": [
                {
                    "id": "draw",
                    "capability": "image",
                    "instruction": "Draw a red square.",
                    "routing_requirements": {"planner_provider": "grok"},
                }
            ],
            "routing_mode": "shadow",
            "policy_revision": 0,
        }
    )


def test_a_generated_image_is_stored_as_bytes_and_can_be_read_back(tmp_path, image_cache):
    _ImageWorker.cache_root = image_cache
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_ImageWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": _image_plan(),
            "project_root": str(tmp_path),
            "idempotency_key": f"img.{secrets.token_hex(4)}",
        },
    )
    run_id = started["data"]["run_id"]
    service.dispatch("agent_hub_continue", {"run_id": run_id, "expected_revision": 0})
    for _ in range(300):
        run = service.store.get_run(run_id)
        if run["status"] != "running":
            break
        time.sleep(0.01)

    assert run["status"] == "completed", run["steps"][0]["checkpoint"]
    artifact_id = run["steps"][0]["output_artifact_ids"][0]
    stored = service.store.get_artifact(artifact_id, include_content=False)

    assert stored["media_type"] == "image/png"

    fetched = service.dispatch(
        "agent_hub_artifact",
        {"action": "get", "artifact_id": artifact_id, "include_base64": True},
    )

    assert fetched["success"] is True
    assert base64.b64decode(fetched["data"]["base64"]) == _png()


def test_reading_an_image_artifact_as_text_fails_rather_than_returning_mojibake(
    tmp_path, image_cache
):
    _ImageWorker.cache_root = image_cache
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_ImageWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": _image_plan(),
            "project_root": str(tmp_path),
            "idempotency_key": f"img.{secrets.token_hex(4)}",
        },
    )
    run_id = started["data"]["run_id"]
    service.dispatch("agent_hub_continue", {"run_id": run_id, "expected_revision": 0})
    for _ in range(300):
        run = service.store.get_run(run_id)
        if run["status"] != "running":
            break
        time.sleep(0.01)
    artifact_id = run["steps"][0]["output_artifact_ids"][0]

    result = service.dispatch(
        "agent_hub_artifact",
        {"action": "get", "artifact_id": artifact_id, "include_text": True},
    )

    assert result["success"] is False
    assert result["error"]["code"] == "artifact_not_text"


def test_verifying_a_binary_artifact_checks_the_digest_not_the_encoding(tmp_path, image_cache):
    _ImageWorker.cache_root = image_cache
    service = HubService(
        HubStore(tmp_path / "state.sqlite3"),
        worker_factory=_ImageWorker,
        cipher=ArtifactCipher(StaticKeyProvider(b"k" * 32)),
    )
    started = service.dispatch(
        "agent_hub_start",
        {
            "plan": _image_plan(),
            "project_root": str(tmp_path),
            "idempotency_key": f"img.{secrets.token_hex(4)}",
        },
    )
    run_id = started["data"]["run_id"]
    service.dispatch("agent_hub_continue", {"run_id": run_id, "expected_revision": 0})
    for _ in range(300):
        run = service.store.get_run(run_id)
        if run["status"] != "running":
            break
        time.sleep(0.01)
    artifact_id = run["steps"][0]["output_artifact_ids"][0]

    verified = service.dispatch(
        "agent_hub_artifact",
        {"action": "verify", "artifact_id": artifact_id},
    )

    assert verified["success"] is True
