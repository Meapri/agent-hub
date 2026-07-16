"""Named session profiles: model + grounding + thinking + provider bundles."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Optional

from . import io_util, model_prefs, paths, response, session_prefs

PREFS_VERSION = 1

BUILTIN_PROFILES: Dict[str, Dict[str, Any]] = {
    "balanced": {
        "description": "Default balanced chat",
        "model": "gemini-3.5-flash-high",
        "task": "chat",
        "grounding": "off",
        "thinking_level": "medium",
        "provider": "agy-oauth",
    },
    "coding": {
        "description": "Stronger reasoning for code/architecture",
        "model": "gemini-3.1-pro-high",
        "task": "code",
        "grounding": "off",
        "thinking_level": "high",
        "provider": "agy-oauth",
    },
    "writing": {
        "description": "Polished prose / docs",
        "model": "gemini-3.1-pro-high",
        "task": "writing",
        "grounding": "off",
        "thinking_level": "medium",
        "provider": "agy-oauth",
    },
    "research": {
        "description": "Current facts with Google Search grounding",
        "model": "gemini-3.5-flash-high",
        "task": "grounded-search",
        "grounding": "always",
        "thinking_level": "medium",
        "provider": "agy-oauth",
    },
    "fast": {
        "description": "Low-latency lightweight answers",
        "model": "gemini-3.5-flash-low",
        "task": "fast",
        "grounding": "off",
        "thinking_level": "low",
        "provider": "agy-oauth",
    },
    "pair": {
        "description": "Second-opinion pair mode (strong model, no grounding)",
        "model": "claude-opus-4-6-thinking",
        "task": "code",
        "grounding": "off",
        "thinking_level": "high",
        "provider": "agy-oauth",
    },
}


class ProfileError(RuntimeError):
    def __init__(self, message: str, *, code: str = "profile_error") -> None:
        super().__init__(message)
        self.code = code


def profiles_path() -> Path:
    return paths.config_dir() / "profiles.json"


def _load_custom() -> Dict[str, Dict[str, Any]]:
    path = profiles_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    custom = data.get("custom") if isinstance(data, dict) else None
    if not isinstance(custom, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in custom.items():
        name = str(key or "").strip().lower()
        if name and isinstance(value, dict):
            out[name] = value
    return out


def _save_custom(custom: Dict[str, Dict[str, Any]]) -> Path:
    path = profiles_path()
    return io_util.write_json_secure(path, {"version": PREFS_VERSION, "custom": custom})


def all_profiles() -> Dict[str, Dict[str, Any]]:
    merged = {k: copy.deepcopy(v) for k, v in BUILTIN_PROFILES.items()}
    for name, body in _load_custom().items():
        base = copy.deepcopy(merged.get(name) or {})
        base.update(body)
        merged[name] = base
    return merged


def get_profile(name: str) -> Dict[str, Any]:
    key = str(name or "").strip().lower()
    profiles = all_profiles()
    if key not in profiles:
        raise ProfileError(
            f"Unknown profile '{name}'. Available: {', '.join(sorted(profiles))}",
            code="profile_unknown",
        )
    body = copy.deepcopy(profiles[key])
    body["name"] = key
    return body


def active_profile() -> Optional[Dict[str, Any]]:
    name = str(session_prefs.load().get("active_profile") or "").strip().lower()
    if not name:
        return None
    try:
        return get_profile(name)
    except ProfileError:
        return None


def list_profiles_tool(_: Dict[str, Any] | None = None) -> Dict[str, Any]:
    active = str(session_prefs.load().get("active_profile") or "")
    items = []
    for name, body in sorted(all_profiles().items()):
        items.append(
            {
                "name": name,
                "description": body.get("description", ""),
                "model": body.get("model", ""),
                "task": body.get("task", "chat"),
                "grounding": body.get("grounding", "off"),
                "thinking_level": body.get("thinking_level", ""),
                "provider": body.get("provider") or None,
                "active": name == active,
                "builtin": name in BUILTIN_PROFILES,
            }
        )
    return {
        "text": f"{len(items)} profiles; active={active or 'none'}",
        "success": True,
        "active_profile": active or None,
        "profiles": items,
        **response.standard_fields(backend="local-profiles"),
    }


def use_profile_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    name = str(arguments.get("name") or arguments.get("profile") or "").strip().lower()
    if not name:
        # clear
        result = session_prefs.set_active_profile("")
        result["text"] = "Cleared active session profile."
        return result
    profile = get_profile(name)
    # Persist active profile
    session_prefs.set_active_profile(name)
    # Optionally pin model for the profile task
    if arguments.get("apply_model_pref", True) and profile.get("model"):
        task = str(profile.get("task") or "chat")
        try:
            model_prefs.set_model(
                model=str(profile["model"]),
                task=task if task in model_prefs.TASK_KEYS else None,
                validate=False,
                notes=f"from profile:{name}",
            )
            if task != "chat" and not arguments.get("skip_default"):
                # also set as default when profile is general
                if name in {"balanced", "fast"}:
                    model_prefs.set_model(model=str(profile["model"]), validate=False)
        except Exception:
            pass
    if profile.get("provider") and arguments.get("apply_provider", True):
        try:
            session_prefs.set_provider(str(profile["provider"]))
        except Exception:
            pass
    return {
        "text": (
            f"Active profile '{name}': model={profile.get('model')}, "
            f"grounding={profile.get('grounding')}, task={profile.get('task')}."
        ),
        "success": True,
        "profile": profile,
        "chat_defaults": chat_defaults_from_profile(profile),
        **response.standard_fields(model=str(profile.get("model") or ""), backend="local-profiles"),
    }


def chat_defaults_from_profile(profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    profile = profile or active_profile()
    if not profile:
        return {}
    return {
        "model": profile.get("model") or "",
        "task": profile.get("task") or "chat",
        "grounding": profile.get("grounding") or "off",
        "thinking_level": profile.get("thinking_level") or "",
    }


def apply_profile_to_chat_args(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Fill *missing* chat fields from the active profile.

    Explicit caller values always win — including ``grounding: "off"``.
    Profile defaults apply only when the key is absent or blank.
    """
    args = dict(arguments or {})
    defaults = chat_defaults_from_profile()
    if not defaults:
        return args

    def _blank(key: str) -> bool:
        # Treat missing keys and empty strings as blank; keep explicit "off"/0/False.
        if key not in arguments:
            return True
        value = arguments.get(key)
        if value is None:
            return True
        if isinstance(value, str) and not value.strip():
            return True
        return False

    if _blank("model") and defaults.get("model"):
        args["model"] = defaults["model"]
    if _blank("task") and defaults.get("task"):
        args["task"] = defaults["task"]
    if _blank("grounding") and defaults.get("grounding"):
        args["grounding"] = defaults["grounding"]
    if _blank("thinking_level") and defaults.get("thinking_level"):
        args["thinking_level"] = defaults["thinking_level"]
    return args


def save_custom_profile_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    name = str(arguments.get("name") or "").strip().lower().replace(" ", "-")
    if not name or name in BUILTIN_PROFILES:
        # allow overriding builtin via custom layer
        if not name:
            raise ProfileError("name is required", code="profile_name_required")
    custom = _load_custom()
    body = {
        "description": str(arguments.get("description") or f"Custom profile {name}"),
        "model": model_prefs.normalize_model_id(str(arguments.get("model") or "gemini-3.5-flash-high")),
        "task": str(arguments.get("task") or "chat"),
        "grounding": str(arguments.get("grounding") or "off"),
        "thinking_level": str(arguments.get("thinking_level") or ""),
        "provider": str(arguments.get("provider") or ""),
    }
    custom[name] = body
    path = _save_custom(custom)
    return {
        "text": f"Saved custom profile '{name}'.",
        "success": True,
        "profile": {"name": name, **body},
        "profiles_file": str(path),
        **response.standard_fields(model=body["model"], backend="local-profiles"),
    }
