"""Small persistent defaults shared by non-Gemini provider adapters."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Dict

from agent_hub.core import limits


ALLOWED = {
    "claude": {"model", "temperature", "max_tokens"},
    "grok": {"model", "temperature", "max_tokens", "api_mode"},
    "gpt": {"model"},
}
_LOCK = threading.RLock()


def _validate_provider_value(provider: str, value: Dict[str, Any]) -> None:
    unknown = set(value) - ALLOWED[provider]
    if unknown:
        raise ValueError(f"unsupported {provider} settings: {', '.join(sorted(unknown))}")
    if "model" in value:
        model = value["model"]
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"{provider} model must be non-empty text")
    if "temperature" in value:
        temperature = value["temperature"]
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(float(temperature))
        ):
            raise ValueError(f"{provider} temperature must be a finite number")
    if "max_tokens" in value:
        max_tokens = value["max_tokens"]
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens < 1
            or max_tokens > limits.MAX_OUTPUT_TOKENS
        ):
            raise ValueError(
                f"{provider} max_tokens must be an integer within 1..{limits.MAX_OUTPUT_TOKENS}"
            )
    if "api_mode" in value:
        api_mode = value["api_mode"]
        if not isinstance(api_mode, str) or api_mode not in {"chat", "responses"}:
            raise ValueError(f"{provider} api_mode must be chat or responses")


def _normalize_changes(provider: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    unknown = set(changes) - ALLOWED[provider]
    if unknown:
        raise ValueError(f"unsupported {provider} settings: {', '.join(sorted(unknown))}")
    normalized = {key: value for key, value in changes.items() if value is not None}
    if "model" in normalized and isinstance(normalized["model"], str):
        normalized["model"] = normalized["model"].strip()
    if "temperature" in normalized:
        temperature = normalized["temperature"]
        if not isinstance(temperature, bool) and isinstance(temperature, (int, float)):
            normalized["temperature"] = float(temperature)
    _validate_provider_value(provider, normalized)
    return normalized


def settings_path() -> Path:
    root = os.getenv("AGENT_HUB_CONFIG_DIR", "").strip()
    directory = Path(root).expanduser() if root else Path.home() / ".config" / "agent-hub"
    return directory / "settings.json"


def _read(*, strict: bool = False) -> Dict[str, Any]:
    try:
        value = json.loads(settings_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as exc:
        if strict:
            raise ValueError("Agent Hub settings file is unreadable or invalid") from exc
        return {}
    if strict and not isinstance(value, dict):
        raise ValueError("Agent Hub settings file must contain a JSON object")
    if strict and isinstance(value, dict):
        for provider in ALLOWED:
            if provider not in value:
                continue
            provider_value = value[provider]
            if not isinstance(provider_value, dict):
                raise ValueError(
                    "Agent Hub provider settings must contain JSON objects: " + provider
                )
            _validate_provider_value(provider, provider_value)
    return value if isinstance(value, dict) else {}


def _write(value: Dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _process_lock():
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path.parent / ".settings.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def get(provider: str) -> Dict[str, Any]:
    settings, _error = inspect(provider)
    return settings


def inspect(provider: str) -> tuple[Dict[str, Any], str | None]:
    with _LOCK:
        try:
            value = _read(strict=True).get(provider, {})
        except ValueError:
            return {}, "settings_invalid"
    return (dict(value), None) if isinstance(value, dict) else ({}, "settings_invalid")


def update(provider: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    if provider not in ALLOWED:
        raise ValueError(f"provider settings are not stored here: {provider}")
    normalized = _normalize_changes(provider, changes)
    with _LOCK:
        with _process_lock():
            all_settings = _read(strict=True)
            value = all_settings.get(provider, {})
            current = dict(value) if isinstance(value, dict) else {}
            current.update(normalized)
            all_settings[provider] = current
            _write(all_settings)
    return current


def remove(provider: str, fields: set[str]) -> Dict[str, Any]:
    if provider not in ALLOWED:
        raise ValueError(f"provider settings are not stored here: {provider}")
    unknown = set(fields) - ALLOWED[provider]
    if unknown:
        raise ValueError(f"unsupported {provider} settings: {', '.join(sorted(unknown))}")
    with _LOCK:
        with _process_lock():
            all_settings = _read(strict=True)
            value = all_settings.get(provider, {})
            current = dict(value) if isinstance(value, dict) else {}
            for field in fields:
                current.pop(field, None)
            if current:
                all_settings[provider] = current
            else:
                all_settings.pop(provider, None)
            _write(all_settings)
    return current


def reset(provider: str) -> Dict[str, Any]:
    with _LOCK:
        with _process_lock():
            all_settings = _read(strict=True)
            removed = all_settings.pop(provider, None)
            if removed is not None:
                _write(all_settings)
    return {"provider": provider, "removed": bool(removed)}
