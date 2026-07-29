"""Live canaries: the only tests here that talk to a real provider.

Every user-visible breakage found in this project so far was found by calling a
provider by hand. The unit suite passed through all of them, because each one
sat at a seam the unit tests stub: the argument the GUI sends the daemon, the
tool list the bridge serves, the success flag an adapter sets. A stub proves the
caller sent something; only a real call proves the thing on the other end
accepts it and answers.

These stay off by default -- they cost money and need credentials. Run them
deliberately:

    AGENT_HUB_LIVE=1 ./.venv/bin/python -m pytest -m live -v

They assert that a response is *usable*, never what a model said. A canary that
asserts on wording fails when a model changes its mind, and a test that fails
for reasons nobody acts on is a test people learn to skip.
"""

from __future__ import annotations

import os
import pathlib
import struct
import zlib

import pytest

LIVE_DIR = pathlib.Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    """Skip the live canaries unless they were asked for.

    This hook receives every collected item, not only the ones under this
    directory, so it has to check where each test lives -- an earlier version
    skipped the entire suite.
    """

    if os.environ.get("AGENT_HUB_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="set AGENT_HUB_LIVE=1 to call real providers")
    for item in items:
        if LIVE_DIR in pathlib.Path(str(item.fspath)).parents:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def canary_png_bytes() -> bytes:
    """A 960x240 checkerboard: small, unambiguous, and describable in one line."""

    width, height = 960, 240

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    rows = []
    for y in range(height):
        row = b"".join(
            b"\xff\x40\x40" if (x // 120 + y // 120) % 2 else b"\x20\x40\xff" for x in range(width)
        )
        rows.append(b"\x00" + row)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def canary_project(tmp_path, canary_png_bytes):
    (tmp_path / "canary.png").write_bytes(canary_png_bytes)
    return tmp_path


@pytest.fixture(scope="session")
def ready_providers() -> dict:
    """Which providers can actually be called right now.

    A canary that fails because a token expired says nothing about this
    repository, so those are skipped rather than failed -- but the reason is
    reported, so "everything skipped" cannot be mistaken for "everything green".
    """

    from agent_hub.v2 import provider_runtime

    state = {}
    for provider in provider_runtime.PROVIDERS:
        try:
            payload = provider_runtime.status(provider)
            info = payload["data"]["providers"][provider]
            state[provider] = bool(info.get("ready") or info.get("invocation_ready"))
        except Exception as exc:  # noqa: BLE001 - a status failure is a not-ready
            state[provider] = False
            print(f"[live] {provider} status failed: {type(exc).__name__}")
    return state


@pytest.fixture
def require_provider(ready_providers):
    def _require(provider: str):
        if not ready_providers.get(provider):
            pytest.skip(f"{provider} is not ready; connect or refresh it first")

    return _require
