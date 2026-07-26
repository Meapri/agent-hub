"""AES-GCM artifact encryption with a macOS Keychain-backed data key."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from hashlib import sha256
import os
import secrets
import subprocess
from typing import Protocol

from .errors import HubV2Error

KEYCHAIN_SERVICE = "agent-hub-v2-artifacts"
KEY_BYTES = 32
NONCE_BYTES = 12


class KeyProvider(Protocol):
    def key(self) -> bytes: ...


@dataclass(frozen=True)
class StaticKeyProvider:
    value: bytes

    def key(self) -> bytes:
        if len(self.value) != KEY_BYTES:
            raise HubV2Error(
                "invalid_artifact_key",
                "The artifact data key has an invalid length.",
                scope="artifact",
            )
        return self.value


class MacOSKeychainKeyProvider:
    def __init__(self, *, account: str | None = None) -> None:
        self.account = account or str(getattr(os, "getuid", lambda: "user")())

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/usr/bin/security", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10.0,
        )

    def key(self) -> bytes:
        if not os.path.exists("/usr/bin/security"):
            raise HubV2Error(
                "keychain_unavailable",
                "macOS Keychain is required for durable private artifacts.",
                scope="artifact",
                next_action={"type": "use_ephemeral_retention"},
            )
        found = self._run(
            [
                "find-generic-password",
                "-a",
                self.account,
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ]
        )
        if found.returncode == 0:
            encoded = found.stdout.strip()
        else:
            encoded = base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")
            created = self._run(
                [
                    "add-generic-password",
                    "-U",
                    "-a",
                    self.account,
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-w",
                    encoded,
                ]
            )
            if created.returncode != 0:
                raise HubV2Error(
                    "keychain_write_failed",
                    "Agent Hub could not create its artifact encryption key.",
                    scope="artifact",
                )
        try:
            key = base64.urlsafe_b64decode(encoded.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise HubV2Error(
                "invalid_artifact_key",
                "The artifact key stored in Keychain is invalid.",
                scope="artifact",
            ) from exc
        if len(key) != KEY_BYTES:
            raise HubV2Error(
                "invalid_artifact_key",
                "The artifact key stored in Keychain has an invalid length.",
                scope="artifact",
            )
        return key


class ArtifactCipher:
    def __init__(self, key_provider: KeyProvider) -> None:
        self._key_provider = key_provider

    @staticmethod
    def _aesgcm():
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise HubV2Error(
                "artifact_crypto_unavailable",
                "The cryptography package is required for durable private artifacts.",
                scope="artifact",
                next_action={"type": "install_runtime_dependency", "package": "cryptography"},
            ) from exc
        return AESGCM

    def encrypt(self, plaintext: bytes, *, aad: bytes) -> dict[str, bytes | str]:
        nonce = secrets.token_bytes(NONCE_BYTES)
        cipher = self._aesgcm()(self._key_provider.key())
        ciphertext = cipher.encrypt(nonce, plaintext, aad)
        return {
            "payload": nonce + ciphertext,
            "content_sha256": sha256(plaintext).hexdigest(),
        }

    def decrypt(
        self,
        payload: bytes,
        *,
        aad: bytes,
        expected_sha256: str,
    ) -> bytes:
        if len(payload) <= NONCE_BYTES:
            raise HubV2Error(
                "artifact_decryption_failed",
                "The encrypted artifact payload is invalid.",
                scope="artifact",
            )
        nonce, ciphertext = payload[:NONCE_BYTES], payload[NONCE_BYTES:]
        cipher = self._aesgcm()(self._key_provider.key())
        try:
            plaintext = cipher.decrypt(nonce, ciphertext, aad)
        except Exception as exc:  # noqa: BLE001
            raise HubV2Error(
                "artifact_decryption_failed",
                "The encrypted artifact could not be authenticated.",
                scope="artifact",
            ) from exc
        if sha256(plaintext).hexdigest() != expected_sha256:
            raise HubV2Error(
                "artifact_digest_conflict",
                "The decrypted artifact digest does not match.",
                scope="artifact",
            )
        return plaintext
