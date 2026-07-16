"""Model provider resolution — direct Google OAuth (Code Assist) only.

The official ``agy`` CLI session is intentionally **not** used for chat,
grounding, or image generation. Users sign in via this plugin's OAuth login
(``google_antigravity_login_*`` / ``scripts/google_antigravity_login.py``).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from . import antigravity_api, security

PROVIDERS = {"agy-oauth"}
PROVIDER_CAPABILITIES = {
    "agy-oauth": {
        "text": True,
        "native_grounding": True,
        "image": True,
        "hosted_tools": True,
    },
}


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str = "provider_error") -> None:
        super().__init__(message)
        self.code = code


def _oauth_ready() -> bool:
    """True when consent is on and a plugin-owned OAuth token file exists."""
    if not security.agy_session_enabled():
        return False
    try:
        from . import agy_auth

        return agy_auth.token_file_path().is_file()
    except Exception:
        return False


def _validate_provider_name(name: str) -> str:
    # Legacy env value: treat agy-cli as invalid with a clear migration message.
    if name in {"agy-cli", "cli"}:
        raise ProviderError(
            "agy-cli transport was removed. Sign in with google_antigravity_login "
            "(or scripts/google_antigravity_login.py) and use agy-oauth only.",
            code="agy_cli_removed",
        )
    if name not in PROVIDERS:
        raise ProviderError(
            "Provider must be agy-oauth (direct Google login).",
            code="provider_invalid",
        )
    return name


def selected_provider() -> str:
    """Resolve transport — always ``agy-oauth`` when configured.

    Priority:
    1. ``GOOGLE_ANTIGRAVITY_PROVIDER`` (must be agy-oauth if set)
    2. Saved session preference (agy-oauth only)
    3. Auto: plugin OAuth token present + consent → agy-oauth
    """
    env = os.getenv("GOOGLE_ANTIGRAVITY_PROVIDER", "").strip().lower()
    if env:
        return _validate_provider_name(env)

    try:
        from . import session_prefs

        saved = session_prefs.preferred_provider()
    except Exception:
        saved = None
    if saved:
        return _validate_provider_name(saved)

    if _oauth_ready():
        return "agy-oauth"
    raise ProviderError(
        "No Google Antigravity OAuth session. Run: "
        "python3 scripts/google_antigravity_login.py interactive "
        "(or MCP google_antigravity_login_start / login_complete).",
        code="provider_not_configured",
    )


def configured() -> bool:
    try:
        selected_provider()
        return True
    except ProviderError:
        return False


def capabilities(selected: str | None = None) -> Dict[str, bool]:
    provider_name = selected or selected_provider()
    return dict(PROVIDER_CAPABILITIES[provider_name])


def require_capability(name: str, *, selected: str | None = None) -> str:
    provider_name = selected or selected_provider()
    if not PROVIDER_CAPABILITIES[provider_name].get(name, False):
        raise ProviderError(
            f"The {provider_name} transport does not support {name.replace('_', ' ')}.",
            code=f"{provider_name.replace('-', '_')}_{name}_unsupported",
        )
    return provider_name


def generate_content(**kwargs: Any) -> Dict[str, Any]:
    selected_provider()  # ensure oauth configured
    return antigravity_api.generate_content(**kwargs)


def generate_content_stream(**kwargs: Any):
    selected_provider()
    yield from antigravity_api.generate_content_stream(**kwargs)


def generate_image(**kwargs: Any) -> Dict[str, Any]:
    """Image generation via Code Assist ``generateContent``.

    Cloud Code PA rejects unknown ``responseFormat.image`` fields (HTTP 400).
    Working schema uses only ``generationConfig.responseModalities: ["IMAGE"]``.
    Aspect ratio / size are requested in the prompt text when provided.
    """
    require_capability("image")
    prompt = str(kwargs["prompt"] or "").strip()
    aspect = kwargs.get("aspect_ratio")
    size = kwargs.get("image_size")
    extras: List[str] = []
    if aspect:
        extras.append(f"aspect ratio {aspect}")
    if size:
        extras.append(f"image size {size}")
    if extras:
        prompt = f"{prompt}\n\n(Please generate with {', '.join(extras)}.)"
    request = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            # Live probe: ["IMAGE"] succeeds; responseFormat.image → Unknown field 400.
            "responseModalities": ["IMAGE"],
        },
    }
    return antigravity_api.generate_content(
        model=kwargs["model"],
        request=request,
        timeout=kwargs.get("timeout", 180.0),
        max_retries=kwargs.get("max_retries", 1),
        retry_sleep_cap_seconds=kwargs.get("retry_sleep_cap_seconds", 8.0),
    )


def list_models() -> List[Dict[str, Any]]:
    selected_provider()
    return antigravity_api.list_models()


def status(*, probe: bool = False) -> Dict[str, Any]:
    try:
        selected = selected_provider()
    except ProviderError as exc:
        return {
            "configured": False,
            "healthy": False,
            "error_type": getattr(exc, "code", type(exc).__name__),
            "error": str(exc),
            "provider": "agy-oauth",
            "backend": "agy-oauth-code-assist",
            "auth_method": "plugin_oauth_login",
        }
    state = antigravity_api.status(probe=probe)
    state["capabilities"] = capabilities(selected)
    state["auth_method"] = "plugin_oauth_login"
    return state
