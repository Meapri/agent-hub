"""Small persistent defaults shared by non-Gemini provider adapters."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict


ALLOWED = {
    "claude": {"model", "temperature", "max_tokens"},
    "grok": {"model", "temperature", "max_tokens", "api_mode"},
}


def settings_path() -> Path:
    root = os.getenv("AGENT_HUB_CONFIG_DIR", "").strip()
    directory = Path(root).expanduser() if root else Path.home() / ".config" / "agent-hub"
    return directory / "settings.json"


def _read() -> Dict[str, Any]:
    try:
        value = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(value: Dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def get(provider: str) -> Dict[str, Any]:
    value = _read().get(provider, {})
    return dict(value) if isinstance(value, dict) else {}


def update(provider: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    if provider not in ALLOWED:
        raise ValueError(f"provider settings are not stored here: {provider}")
    unknown = set(changes) - ALLOWED[provider]
    if unknown:
        raise ValueError(f"unsupported {provider} settings: {', '.join(sorted(unknown))}")
    if "model" in changes and not str(changes["model"] or "").strip():
        raise ValueError("model must not be empty")
    if "max_tokens" in changes and int(changes["max_tokens"]) < 1:
        raise ValueError("max_tokens must be at least 1")
    if "temperature" in changes:
        changes["temperature"] = float(changes["temperature"])
    if "api_mode" in changes and str(changes["api_mode"]) not in {"chat", "responses"}:
        raise ValueError("api_mode must be chat or responses")
    all_settings = _read()
    current = get(provider)
    current.update({key: value for key, value in changes.items() if value is not None})
    all_settings[provider] = current
    _write(all_settings)
    return current


def reset(provider: str) -> Dict[str, Any]:
    all_settings = _read()
    removed = all_settings.pop(provider, None)
    if removed is not None:
        _write(all_settings)
    return {"provider": provider, "removed": bool(removed)}
