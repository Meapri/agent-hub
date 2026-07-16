"""Provider preference + active session profile state."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from . import io_util, paths, response

PREFS_VERSION = 1
VALID_PROVIDERS = ("agy-oauth",)


class SessionPrefsError(RuntimeError):
    def __init__(self, message: str, *, code: str = "session_prefs_error") -> None:
        super().__init__(message)
        self.code = code


def prefs_path() -> Path:
    override = os.getenv("GOOGLE_ANTIGRAVITY_SESSION_PREFS_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return paths.config_dir() / "session-prefs.json"


def _default() -> Dict[str, Any]:
    return {
        "version": PREFS_VERSION,
        "preferred_provider": "",
        "active_profile": "",
    }


def load() -> Dict[str, Any]:
    path = prefs_path()
    if not path.is_file():
        return _default()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return _default()
    if not isinstance(data, dict):
        return _default()
    out = _default()
    provider = str(data.get("preferred_provider") or "").strip().lower()
    if provider in VALID_PROVIDERS:
        out["preferred_provider"] = provider
    out["active_profile"] = str(data.get("active_profile") or "").strip().lower()
    return out


def save(data: Dict[str, Any]) -> Path:
    path = prefs_path()
    payload = {
        "version": PREFS_VERSION,
        "preferred_provider": str(data.get("preferred_provider") or ""),
        "active_profile": str(data.get("active_profile") or ""),
    }
    return io_util.write_json_secure(path, payload)


def preferred_provider() -> Optional[str]:
    """Return *saved* provider preference only (env is handled by provider.selected_provider)."""
    saved = str(load().get("preferred_provider") or "").strip().lower()
    return saved if saved in VALID_PROVIDERS else None


def set_provider(provider: str) -> Dict[str, Any]:
    name = str(provider or "").strip().lower().replace("_", "-")
    if name in {"agy-cli", "cli"}:
        raise SessionPrefsError(
            "agy-cli was removed; only agy-oauth (plugin Google login) is supported.",
            code="agy_cli_removed",
        )
    if name not in VALID_PROVIDERS:
        raise SessionPrefsError(
            f"provider must be one of: {', '.join(VALID_PROVIDERS)}",
            code="provider_invalid",
        )
    data = load()
    data["preferred_provider"] = name
    path = save(data)
    return {
        "text": f"Preferred provider set to '{name}'. Override anytime with GOOGLE_ANTIGRAVITY_PROVIDER.",
        "success": True,
        "provider": name,
        "prefs_file": str(path),
        **response.standard_fields(backend="local-session-prefs"),
    }


def clear_provider() -> Dict[str, Any]:
    data = load()
    data["preferred_provider"] = ""
    path = save(data)
    return {
        "text": "Cleared preferred provider; selection falls back to auto rules.",
        "success": True,
        "prefs_file": str(path),
        **response.standard_fields(backend="local-session-prefs"),
    }


def set_active_profile(name: str) -> Dict[str, Any]:
    data = load()
    data["active_profile"] = str(name or "").strip().lower()
    path = save(data)
    return {
        "text": f"Active session profile set to '{data['active_profile'] or '(none)'}'.",
        "success": True,
        "active_profile": data["active_profile"] or None,
        "prefs_file": str(path),
        **response.standard_fields(backend="local-session-prefs"),
    }


def get_session_prefs(_: Dict[str, Any] | None = None) -> Dict[str, Any]:
    data = load()
    return {
        "text": (
            f"preferred_provider={data.get('preferred_provider') or 'auto'}; "
            f"active_profile={data.get('active_profile') or 'none'}"
        ),
        "success": True,
        "prefs": data,
        "prefs_file": str(prefs_path()),
        "env_provider": os.getenv("GOOGLE_ANTIGRAVITY_PROVIDER", "") or None,
        **response.standard_fields(backend="local-session-prefs"),
    }


def set_provider_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    if arguments.get("clear"):
        return clear_provider()
    return set_provider(str(arguments.get("provider") or ""))
