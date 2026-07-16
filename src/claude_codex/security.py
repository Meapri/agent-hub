"""Consent and trust-boundary helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import paths

TRUE_VALUES = {"1", "true", "yes", "on"}
CONSENT_FILE_VERSION = 1


def env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


def consent_file_path() -> Path:
    override = os.getenv("CLAUDE_CODEX_CONSENT_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return paths.config_dir() / "user-consent.json"


def user_consent_enabled() -> bool:
    if env_flag("CLAUDE_CODEX_USER_CONSENT"):
        return True
    try:
        data = json.loads(consent_file_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return bool(
        isinstance(data, dict)
        and data.get("accepted") is True
        and int(data.get("version") or 0) == CONSENT_FILE_VERSION
    )


def require_consent() -> None:
    if not user_consent_enabled():
        raise RuntimeError(
            "Explicit consent required. Run: "
            "python3 scripts/claude_codex_consent.py grant --i-understand-and-consent "
            "or set CLAUDE_CODEX_USER_CONSENT=1"
        )


def consent_status() -> dict:
    env_consent = env_flag("CLAUDE_CODEX_USER_CONSENT")
    file_consent = user_consent_enabled() and not env_consent
    master = env_consent or file_consent
    if env_consent:
        source = "CLAUDE_CODEX_USER_CONSENT"
    elif file_consent:
        source = "user-consent.json"
    else:
        source = "none"
    return {
        "user_consent": master,
        "consent_source": source,
        "consent_file": str(consent_file_path()),
        "consent_file_active": file_consent,
        "configuration": {
            "grant_command": (
                "python3 scripts/claude_codex_consent.py grant --i-understand-and-consent"
            ),
            "revoke_command": "python3 scripts/claude_codex_consent.py revoke",
            "enable_all": "CLAUDE_CODEX_USER_CONSENT=1",
        },
    }


def grant_consent() -> Path:
    path = consent_file_path()
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


def revoke_consent() -> bool:
    path = consent_file_path()
    if path.is_file():
        path.unlink()
        return True
    return False
