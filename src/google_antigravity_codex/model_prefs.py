"""Persistent Antigravity model selection for Codex MCP tools.

Stores user defaults under ``~/.config/google-antigravity-codex/model-prefs.json``
so chat/write/search/image/route tools share one selection without re-passing
``model=`` every call.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import re
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional

from . import io_util, paths, response

PREFS_VERSION = 1
TASK_KEYS = (
    "chat",
    "code",
    "fast",
    "grounded-search",
    "writing",
    "release",
    "image",
)
_LOCK = threading.RLock()

# Friendly aliases → canonical ids used by this plugin / agy.
MODEL_ALIASES: Dict[str, str] = {
    "flash": "gemini-3.5-flash-high",
    "flash-high": "gemini-3.5-flash-high",
    "flash-medium": "gemini-3.5-flash-medium",
    "flash-low": "gemini-3.5-flash-low",
    "gemini-flash": "gemini-3.5-flash-high",
    "gemini-3.5-flash": "gemini-3.5-flash-high",
    "pro": "gemini-3.1-pro-high",
    "pro-high": "gemini-3.1-pro-high",
    "pro-low": "gemini-3.1-pro-low",
    "gemini-pro": "gemini-3.1-pro-high",
    "gemini-3.1-pro": "gemini-3.1-pro-high",
    "opus": "claude-opus-4-6-thinking",
    "claude-opus": "claude-opus-4-6-thinking",
    "sonnet": "claude-sonnet-4-6-thinking",
    "claude-sonnet": "claude-sonnet-4-6-thinking",
    "gpt-oss": "gpt-oss-120b",
    "nano-banana": "gemini-3.1-flash-image",
    "image": "gemini-3.1-flash-image",
}


class ModelPrefsError(RuntimeError):
    def __init__(self, message: str, *, code: str = "model_prefs_error") -> None:
        super().__init__(message)
        self.code = code


def prefs_path() -> Path:
    override = os.getenv("GOOGLE_ANTIGRAVITY_MODEL_PREFS_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return paths.config_dir() / "model-prefs.json"


def _default_prefs() -> Dict[str, Any]:
    return {
        "version": PREFS_VERSION,
        "default_model": "",
        "task_models": {},
        "notes": "",
    }


def _load_prefs(*, strict: bool = False) -> Dict[str, Any]:
    path = prefs_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default_prefs()
    except (OSError, ValueError, TypeError) as exc:
        if strict:
            raise ModelPrefsError(
                "Antigravity model preferences are unreadable or invalid.",
                code="model_prefs_invalid",
            ) from exc
        return _default_prefs()
    if not isinstance(data, dict):
        if strict:
            raise ModelPrefsError(
                "Antigravity model preferences must contain a JSON object.",
                code="model_prefs_invalid",
            )
        return _default_prefs()
    if strict:
        version = data.get("version", PREFS_VERSION)
        if isinstance(version, bool) or not isinstance(version, int) or version != PREFS_VERSION:
            raise ModelPrefsError(
                "Antigravity model preferences use an unsupported version.",
                code="model_prefs_invalid",
            )
        if "task_models" in data and not isinstance(data["task_models"], dict):
            raise ModelPrefsError(
                "Antigravity task model preferences must contain a JSON object.",
                code="model_prefs_invalid",
            )
        for field in ("default_model", "notes"):
            if field in data and data[field] is not None and not isinstance(data[field], str):
                raise ModelPrefsError(
                    f"Antigravity model preference '{field}' must be text.",
                    code="model_prefs_invalid",
                )
        for key, value in (data.get("task_models") or {}).items():
            task = key.strip().lower().replace("_", "-") if isinstance(key, str) else ""
            if task not in TASK_KEYS or not isinstance(value, str) or not value.strip():
                raise ModelPrefsError(
                    "Antigravity task model preferences contain an invalid task or model.",
                    code="model_prefs_invalid",
                )
    out = _default_prefs()
    out["default_model"] = str(data.get("default_model") or "").strip()
    tasks = data.get("task_models") if isinstance(data.get("task_models"), dict) else {}
    cleaned: Dict[str, str] = {}
    for key, value in tasks.items():
        task = str(key or "").strip().lower().replace("_", "-")
        model = str(value or "").strip()
        if task in TASK_KEYS and model:
            cleaned[task] = model
    out["task_models"] = cleaned
    out["notes"] = str(data.get("notes") or "")
    return out


def load_prefs() -> Dict[str, Any]:
    with _LOCK:
        return _load_prefs()


def inspect_prefs() -> tuple[Dict[str, Any], str | None]:
    with _LOCK:
        try:
            return _load_prefs(strict=True), None
        except ModelPrefsError as exc:
            return _default_prefs(), exc.code


def save_prefs(prefs: Dict[str, Any]) -> Path:
    path = prefs_path()
    payload = {
        "version": PREFS_VERSION,
        "default_model": str(prefs.get("default_model") or "").strip(),
        "task_models": dict(prefs.get("task_models") or {}),
        "notes": str(prefs.get("notes") or ""),
    }
    return io_util.write_json_secure(path, payload)


@contextmanager
def _process_lock():
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path.parent / ".model-prefs.lock",
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def normalize_model_id(value: str) -> str:
    text = str(value or "").strip().removeprefix("models/")
    if not text:
        return ""
    # Display strings from agy: "Gemini 3.5 Flash (High)"
    lowered = text.lower()
    if lowered in MODEL_ALIASES:
        return MODEL_ALIASES[lowered]
    compact = re.sub(r"[\s()]+", "-", lowered).strip("-")
    compact = compact.replace("--", "-")
    if compact in MODEL_ALIASES:
        return MODEL_ALIASES[compact]
    # Common display → id heuristics
    display_map = {
        "gemini-3.5-flash-high": "gemini-3.5-flash-high",
        "gemini-3.5-flash-(high)": "gemini-3.5-flash-high",
        "gemini-3.1-pro-high": "gemini-3.1-pro-high",
        "gemini-3.1-pro-(high)": "gemini-3.1-pro-high",
        "claude-opus-4.6-(thinking)": "claude-opus-4-6-thinking",
        "claude-sonnet-4.6-(thinking)": "claude-sonnet-4-6-thinking",
    }
    if compact in display_map:
        return display_map[compact]
    return text


def resolve_model(
    *,
    explicit: Optional[str] = None,
    task: Optional[str] = None,
    fallback: str = "",
) -> str:
    """Resolve model: explicit arg → task pref → default pref → fallback.

    The global default applies to text tasks. For ``image``, only an
    image-scoped task pref or an image-like default id is used, so a chat
    flash default does not leak into image generation.
    """
    if explicit and str(explicit).strip():
        return normalize_model_id(str(explicit))
    prefs = load_prefs()
    task_key = str(task or "").strip().lower().replace("_", "-")
    if task_key in TASK_KEYS:
        task_model = str((prefs.get("task_models") or {}).get(task_key) or "").strip()
        if task_model:
            return normalize_model_id(task_model)
    default = str(prefs.get("default_model") or "").strip()
    env = os.getenv("GOOGLE_ANTIGRAVITY_DEFAULT_MODEL", "").strip()
    candidate = default or env
    if candidate:
        if task_key == "image":
            lowered = candidate.lower()
            if "image" in lowered or "banana" in lowered:
                return normalize_model_id(candidate)
        else:
            return normalize_model_id(candidate)
    return normalize_model_id(fallback) if fallback else ""


def _available_model_ids(*, image: bool = False) -> Optional[List[str]]:
    try:
        from . import models as models_mod

        listed = models_mod.list_models({})
        ids: List[str] = []
        key = "image_models" if image else "text_models"
        for item in listed.get(key) or []:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
        return ids or None
    except Exception:
        return None


def set_model(
    *,
    model: str,
    task: Optional[str] = None,
    validate: bool = True,
    notes: str = "",
) -> Dict[str, Any]:
    model_id = normalize_model_id(model)
    if not model_id:
        raise ModelPrefsError("model is required.", code="model_required")

    task_key = ""
    if task is not None and str(task).strip():
        task_key = str(task).strip().lower().replace("_", "-")
        if task_key not in TASK_KEYS:
            raise ModelPrefsError(
                f"Unknown task '{task}'. Valid: {', '.join(TASK_KEYS)}",
                code="task_invalid",
            )

    if validate:
        available = _available_model_ids(image=task_key == "image")
        if available is None:
            raise ModelPrefsError(
                "The model catalog is unavailable; retry or set validate=false explicitly.",
                code="model_catalog_unavailable",
            )
        normalized_available = {normalize_model_id(item) for item in available}
        if model_id not in normalized_available:
            kind = "image" if task_key == "image" else "text"
            raise ModelPrefsError(
                f"Model '{model_id}' is not in the available {kind} model catalog.",
                code="model_not_available",
            )

    with _LOCK:
        with _process_lock():
            prefs = _load_prefs(strict=True)
            if task_key:
                tasks = dict(prefs.get("task_models") or {})
                tasks[task_key] = model_id
                prefs["task_models"] = tasks
                scope = f"task:{task_key}"
            else:
                prefs["default_model"] = model_id
                scope = "default"
            if notes:
                prefs["notes"] = str(notes)
            path = save_prefs(prefs)
    return {
        "text": f"Saved Antigravity model '{model_id}' as {scope}.",
        "success": True,
        "model": model_id,
        "scope": scope,
        "task": task_key or None,
        "prefs_file": str(path),
        "prefs": prefs,
        **response.standard_fields(
            model=model_id,
            backend="local-model-prefs",
            warnings=[],
        ),
    }


def clear_prefs(
    *,
    task: Optional[str] = None,
    all_prefs: bool = False,
    default_scopes: bool = False,
) -> Dict[str, Any]:
    task_key = ""
    if task is not None and str(task).strip():
        task_key = str(task).strip().lower().replace("_", "-")
        if task_key not in TASK_KEYS:
            raise ModelPrefsError(f"Unknown task '{task}'.", code="task_invalid")
    with _LOCK:
        with _process_lock():
            prefs = _load_prefs(strict=True)
            if all_prefs:
                prefs = _default_prefs()
                text = "Cleared all Antigravity model preferences."
            elif task_key:
                tasks = dict(prefs.get("task_models") or {})
                tasks.pop(task_key, None)
                prefs["task_models"] = tasks
                text = f"Cleared Antigravity model preference for task '{task_key}'."
            elif default_scopes:
                tasks = dict(prefs.get("task_models") or {})
                tasks.pop("chat", None)
                prefs["task_models"] = tasks
                prefs["default_model"] = ""
                text = "Cleared Antigravity chat and default model preferences."
            else:
                prefs["default_model"] = ""
                text = "Cleared default Antigravity model preference."
            path = save_prefs(prefs)
    return {
        "text": text,
        "success": True,
        "task": task_key or None,
        "prefs_file": str(path),
        "prefs": prefs,
        **response.standard_fields(backend="local-model-prefs"),
    }


def get_prefs_tool(_: Dict[str, Any] | None = None) -> Dict[str, Any]:
    prefs = load_prefs()
    effective = {
        "default": resolve_model(fallback="gemini-3.5-flash-high"),
        "tasks": {task: resolve_model(task=task, fallback="") for task in TASK_KEYS},
    }
    has_any = bool(prefs.get("default_model") or prefs.get("task_models"))
    return {
        "text": (
            f"Default model: {effective['default']}"
            + ("" if has_any else " (plugin fallback; no user preference saved)")
        ),
        "success": True,
        "prefs": prefs,
        "effective": effective,
        "tasks": list(TASK_KEYS),
        "aliases": sorted(MODEL_ALIASES.keys()),
        "prefs_file": str(prefs_path()),
        **response.standard_fields(
            model=effective["default"],
            backend="local-model-prefs",
            warnings=[] if has_any else ["no_user_model_preference"],
        ),
    }


def set_model_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return set_model(
        model=str(arguments.get("model") or arguments.get("id") or ""),
        task=arguments.get("task"),
        validate=bool(arguments.get("validate", True)),
        notes=str(arguments.get("notes") or ""),
    )


def clear_prefs_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return clear_prefs(
        task=arguments.get("task"),
        all_prefs=bool(arguments.get("all") or arguments.get("all_prefs")),
        default_scopes=bool(arguments.get("default_scopes")),
    )
