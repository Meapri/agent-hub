"""Parameterized consent gate shared by provider leaves.

The per-package ``security`` module binds a ``ConsentGate`` to its own env
prefix + grant-script string and re-exports the gate's methods at module level,
so ``security.require_consent()`` / ``consent_status()`` etc. keep the exact
same signatures AND byte-identical output strings.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import tempfile
from typing import Callable

from .auth_state import auth_state_lock, consent_file_revision

TRUE_VALUES = {"1", "true", "yes", "on"}
CONSENT_FILE_VERSION = 1


def env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


def consent_payload_enabled(
    data: object,
    *,
    version: int = CONSENT_FILE_VERSION,
) -> bool:
    if not isinstance(data, dict) or data.get("accepted") is not True:
        return False
    stored_version = data.get("version")
    return type(stored_version) is int and stored_version == version


class ConsentGate:
    def __init__(self, *, env_prefix: str, grant_script: str, config_dir: Callable[[], Path]) -> None:
        self.env_prefix = env_prefix
        self.grant_script = grant_script
        self._config_dir = config_dir
        self.consent_env = f"{env_prefix}_USER_CONSENT"
        self.consent_file_env = f"{env_prefix}_CONSENT_FILE"

    def env_flag(self, name: str, *, default: bool = False) -> bool:
        return env_flag(name, default=default)

    def consent_file_path(self) -> Path:
        override = os.getenv(self.consent_file_env, "").strip()
        if override:
            return Path(override).expanduser()
        return self._config_dir() / "user-consent.json"

    def user_consent_enabled(self) -> bool:
        if env_flag(self.consent_env):
            return True
        try:
            data = json.loads(self.consent_file_path().read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        return consent_payload_enabled(data)

    def consent_revision(self) -> str:
        if env_flag(self.consent_env):
            return f"env:{self.consent_env}"
        if not self.user_consent_enabled():
            return "none"
        return f"file:{consent_file_revision(self.consent_file_path())}"

    def require_consent(self) -> None:
        if not self.user_consent_enabled():
            raise RuntimeError(
                "Explicit consent required. Run: "
                f"python3 {self.grant_script} grant --i-understand-and-consent "
                f"or set {self.consent_env}=1"
            )

    def consent_status(self) -> dict:
        env_consent = env_flag(self.consent_env)
        file_consent = self.user_consent_enabled() and not env_consent
        master = env_consent or file_consent
        if env_consent:
            source = self.consent_env
        elif file_consent:
            source = "user-consent.json"
        else:
            source = "none"
        return {
            "user_consent": master,
            "consent_source": source,
            "consent_file": str(self.consent_file_path()),
            "consent_file_active": file_consent,
            "configuration": {
                "grant_command": (
                    f"python3 {self.grant_script} grant --i-understand-and-consent"
                ),
                "revoke_command": f"python3 {self.grant_script} revoke",
                "enable_all": f"{self.consent_env}=1",
            },
        }

    def grant_consent(self) -> Path:
        path = self.consent_file_path()
        with auth_state_lock(self._config_dir()):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                current = None
            if consent_payload_enabled(current):
                return path
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
            )
            temporary = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "accepted": True,
                            "version": CONSENT_FILE_VERSION,
                            "grant_id": secrets.token_urlsafe(18),
                        },
                        handle,
                        indent=2,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return path

    def revoke_consent(self) -> bool:
        path = self.consent_file_path()
        with auth_state_lock(self._config_dir()):
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            return True
