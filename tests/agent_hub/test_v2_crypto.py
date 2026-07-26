from __future__ import annotations

import base64
import subprocess

import pytest

from agent_hub.v2.crypto import KEY_BYTES, MacOSKeychainKeyProvider
from agent_hub.v2.errors import HubV2Error


def test_keychain_provider_reads_existing_key_without_mutation(monkeypatch):
    key = b"k" * KEY_BYTES
    encoded = base64.urlsafe_b64encode(key).decode("ascii")
    provider = MacOSKeychainKeyProvider(account="fixture")
    calls = []

    def run(arguments):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout=encoded + "\n", stderr="")

    monkeypatch.setattr(provider, "_run", run)

    assert provider.key() == key
    assert len(calls) == 1
    assert calls[0][0] == "find-generic-password"


def test_keychain_provider_rejects_invalid_stored_key(monkeypatch):
    provider = MacOSKeychainKeyProvider(account="fixture")
    monkeypatch.setattr(
        provider,
        "_run",
        lambda arguments: subprocess.CompletedProcess(
            arguments,
            0,
            stdout=base64.urlsafe_b64encode(b"short").decode("ascii"),
            stderr="",
        ),
    )

    with pytest.raises(HubV2Error) as raised:
        provider.key()

    assert raised.value.code == "invalid_artifact_key"
