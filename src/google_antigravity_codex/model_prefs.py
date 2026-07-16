"""Persistent Antigravity model selection for Codex MCP tools.

Stores user defaults under ``~/.config/google-antigravity-codex/model-prefs.json``
so chat/write/search/image/route tools share one selection without re-passing
``model=`` every call.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
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


def load_prefs() -> Dict[str, Any]:
    path = prefs_path()
    if not path.is_file():
        return _default_prefs()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return _default_prefs()
    if not isinstance(data, dict):
        return _default_prefs()
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


def save_prefs(prefs: Dict[str, Any]) -> Path:
    path = prefs_path()
    payload = {
        "version": PREFS_VERSION,
        "default_model": str(prefs.get("default_model") or "").strip(),
        "task_models": dict(prefs.get("task_models") or {}),
        "notes": str(prefs.get("notes") or ""),
    }
    return io_util.write_json_secure(path, payload)


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


def _available_model_ids() -> Optional[List[str]]:
    try:
        from . import models as models_mod

        listed = models_mod.list_models({})
        ids: List[str] = []
        for key in ("text_models", "image_models"):
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

    warnings: List[str] = []
    if validate:
        available = _available_model_ids()
        if available is not None:
            normalized_available = {normalize_model_id(i) for i in available}
            # Also accept if any available id contains / equals the selection.
            ok = model_id in normalized_available or any(
                model_id == a or model_id in a or a in model_id for a in normalized_available
            )
            if not ok:
                warnings.append("model_not_in_live_catalog")
                # Still allow set — catalogs can lag; warn only.

    prefs = load_prefs()
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
        "prefs": load_prefs(),
        **response.standard_fields(
            model=model_id,
            backend="local-model-prefs",
            warnings=warnings,
        ),
    }


def clear_prefs(*, task: Optional[str] = None, all_prefs: bool = False) -> Dict[str, Any]:
    prefs = load_prefs()
    if all_prefs:
        path = save_prefs(_default_prefs())
        return {
            "text": "Cleared all Antigravity model preferences.",
            "success": True,
            "prefs_file": str(path),
            "prefs": load_prefs(),
            **response.standard_fields(backend="local-model-prefs"),
        }
    if task is not None and str(task).strip():
        task_key = str(task).strip().lower().replace("_", "-")
        if task_key not in TASK_KEYS:
            raise ModelPrefsError(f"Unknown task '{task}'.", code="task_invalid")
        tasks = dict(prefs.get("task_models") or {})
        tasks.pop(task_key, None)
        prefs["task_models"] = tasks
        path = save_prefs(prefs)
        return {
            "text": f"Cleared Antigravity model preference for task '{task_key}'.",
            "success": True,
            "task": task_key,
            "prefs_file": str(path),
            "prefs": load_prefs(),
            **response.standard_fields(backend="local-model-prefs"),
        }
    prefs["default_model"] = ""
    path = save_prefs(prefs)
    return {
        "text": "Cleared default Antigravity model preference.",
        "success": True,
        "prefs_file": str(path),
        "prefs": load_prefs(),
        **response.standard_fields(backend="local-model-prefs"),
    }


def get_prefs_tool(_: Dict[str, Any] | None = None) -> Dict[str, Any]:
    prefs = load_prefs()
    effective = {
        "default": resolve_model(fallback="gemini-3.5-flash-high"),
        "tasks": {
            task: resolve_model(task=task, fallback="")
            for task in TASK_KEYS
        },
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
    )
