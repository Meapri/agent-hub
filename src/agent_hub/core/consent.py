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
from typing import Callable

TRUE_VALUES = {"1", "true", "yes", "on"}
CONSENT_FILE_VERSION = 1


def env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


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
        return bool(
            isinstance(data, dict)
            and data.get("accepted") is True
            and int(data.get("version") or 0) == CONSENT_FILE_VERSION
        )

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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"accepted": True, "version": CONSENT_FILE_VERSION}, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path

    def revoke_consent(self) -> bool:
        path = self.consent_file_path()
        if path.is_file():
            path.unlink()
            return True
        return False
