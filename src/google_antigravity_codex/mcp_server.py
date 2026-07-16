"""Minimal MCP stdio server for Google Antigravity Codex."""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, List, Optional

from . import (
    __version__,
    account,
    agy_auth,
    antigravity_api,
    chat,
    compare,
    diff_review,
    grounding,
    image,
    model_prefs,
    models,
    oauth_login,
    profiles,
    quota,
    release,
    response,
    routing,
    provider,
    security,
    session_prefs,
    writing,
)

SERVER_NAME = "google-antigravity-codex"
SERVER_VERSION = __version__
MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_PROTOCOL_VERSIONS = (MODERN_PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS)
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
DISCOVERY_TTL_MS = 300_000
MODERN_META_PROTOCOL = "io.modelcontextprotocol/protocolVersion"
MODERN_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
MODERN_META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"


class RpcError(ValueError):
    def __init__(self, code: int, message: str, *, data: Dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def _schema_auth_empty() -> Dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


LOGIN_START_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "use_local_redirect": {
            "type": "boolean",
            "default": True,
            "description": "Prefer http://localhost:51121/auth/callback (Hermes-compatible).",
        },
    },
    "additionalProperties": False,
}

SET_MODEL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "model": {
            "type": "string",
            "description": "Model id or alias (e.g. gemini-3.1-pro-high, flash, opus, sonnet).",
        },
        "task": {
            "type": "string",
            "enum": list(model_prefs.TASK_KEYS),
            "description": "Optional task scope. Omit to set the global default model.",
        },
        "validate": {
            "type": "boolean",
            "default": True,
            "description": "Warn when model is not in the live catalog (still saves).",
        },
        "notes": {"type": "string"},
    },
    "required": ["model"],
    "additionalProperties": False,
}

CLEAR_MODEL_PREFS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "enum": list(model_prefs.TASK_KEYS),
            "description": "Clear only this task preference.",
        },
        "all": {
            "type": "boolean",
            "default": False,
            "description": "Clear default and all task preferences.",
        },
    },
    "additionalProperties": False,
}

SET_PROVIDER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "provider": {"type": "string", "enum": ["agy-oauth"]},
        "clear": {"type": "boolean", "default": False},
    },
    "additionalProperties": False,
}

USE_PROFILE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Profile name (balanced, coding, writing, research, fast, pair) or empty to clear.",
        },
        "profile": {"type": "string"},
        "apply_model_pref": {"type": "boolean", "default": True},
        "apply_provider": {"type": "boolean", "default": True},
    },
    "additionalProperties": False,
}

SAVE_PROFILE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "model": {"type": "string"},
        "task": {"type": "string"},
        "grounding": {"type": "string", "enum": ["off", "auto", "always"]},
        "thinking_level": {"type": "string"},
        "provider": {"type": "string", "enum": ["", "agy-oauth"]},
    },
    "required": ["name", "model"],
    "additionalProperties": False,
}

LOGOUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "forget_client": {
            "type": "boolean",
            "default": False,
            "description": "Also delete saved OAuth client id/secret file.",
        },
    },
    "additionalProperties": False,
}

COMPARE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "models": {
            "description": "Two or three model ids (array or comma-separated string).",
            "oneOf": [
                {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
                {"type": "string"},
            ],
        },
        "max_tokens": {"type": "integer", "minimum": 32, "maximum": 1024, "default": 256},
        "temperature": {"type": "number"},
        "timeout_sec": {"type": "integer", "minimum": 20, "maximum": 300, "default": 90},
        "grounding": {"type": "string", "enum": ["off", "auto", "always"], "default": "off"},
    },
    "required": ["prompt"],
    "additionalProperties": False,
}

REVIEW_DIFF_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "cwd": {"type": "string", "description": "Git repository root (default: .)."},
        "repo": {"type": "string"},
        "staged": {"type": "boolean", "default": False},
        "base": {"type": "string", "description": "Optional git ref to diff against."},
        "ref": {"type": "string"},
        "paths": {"type": "array", "items": {"type": "string"}},
        "instruction": {"type": "string"},
        "focus": {"type": "string"},
        "model": {"type": "string"},
        "max_tokens": {"type": "integer", "minimum": 256, "maximum": 8192},
        "timeout_sec": {"type": "integer", "minimum": 30, "maximum": 600},
        "thinking_level": {"type": "string"},
    },
    "additionalProperties": False,
}

LOGIN_COMPLETE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "code_or_url": {
            "type": "string",
            "description": "Full OAuth redirect URL or the raw code= authorization code.",
        },
        "code": {"type": "string"},
        "url": {"type": "string"},
        "probe": {
            "type": "boolean",
            "default": True,
            "description": "After saving tokens, verify with list_models (or a tiny chat).",
        },
    },
    "additionalProperties": False,
}


CHAT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "messages": {"type": "array", "items": {"type": "object"}},
        "system": {"type": "string"},
        "model": {"type": "string", "default": chat.DEFAULT_MODEL},
        "temperature": {"type": "number"},
        "top_p": {"type": "number"},
        "max_tokens": {"type": "integer", "minimum": 1},
        "thinking_level": {
            "type": "string",
            "enum": ["minimal", "low", "medium", "high"],
        },
        "grounding": {"type": "string", "enum": ["off", "auto", "always"], "default": "off"},
        "tools": {
            "type": "array",
            "description": "Optional Gemini tools or OpenAI-style function tools.",
            "items": {"type": "object"},
        },
        "stream": {
            "type": "boolean",
            "default": False,
            "description": "When true (agy-oauth), stream tokens via MCP notifications/message deltas.",
        },
        "timeout_sec": {"type": "integer", "minimum": 20, "maximum": 600},
        "retry_count": {"type": "integer", "minimum": 0, "maximum": 5, "default": 1},
        "retry_sleep_cap_sec": {"type": "number", "minimum": 0, "maximum": 30, "default": 8},
    },
    "additionalProperties": False,
}

GROUNDING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "model": {"type": "string", "default": chat.DEFAULT_MODEL},
        "max_sources": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        "freshness": {
            "type": "string",
            "enum": ["auto", "latest", "today", "week", "month", "official"],
            "default": "auto",
        },
        "language": {"type": "string", "default": "ko"},
        "resolve_sources": {"type": "boolean", "default": True},
        "direct_source_retry": {"type": "boolean", "default": True},
        "timeout_sec": {"type": "integer", "minimum": 20, "maximum": 600},
        "retry_count": {"type": "integer", "minimum": 0, "maximum": 5, "default": 1},
        "retry_sleep_cap_sec": {"type": "number", "minimum": 0, "maximum": 30, "default": 8},
    },
    "required": ["query"],
    "additionalProperties": False,
}

IMAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "model": {"type": "string", "default": image.DEFAULT_MODEL},
        "aspect_ratio": {
            "type": "string",
            "enum": ["landscape", "square", "portrait"],
            "default": "landscape",
        },
        "image_size": {"type": "string", "enum": ["512", "1K", "2K", "4K", "1024", "2048", "4096"]},
        "timeout_sec": {"type": "integer", "minimum": 20, "maximum": 600},
        "retry_count": {"type": "integer", "minimum": 0, "maximum": 5, "default": 1},
        "retry_sleep_cap_sec": {"type": "number", "minimum": 0, "maximum": 30, "default": 8},
    },
    "required": ["prompt"],
    "additionalProperties": False,
}

WRITING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "enum": sorted(writing.TASKS),
            "default": "auto",
        },
        "instruction": {"type": "string"},
        "source_text": {"type": "string"},
        "source_file": {"type": "string"},
        "workspace_root": {
            "type": "string",
            "description": "Explicit workspace root containing source_file.",
        },
        "context": {"type": "string"},
        "profile": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ]
        },
        "tone": {"type": "string"},
        "audience": {"type": "string"},
        "target_language": {"type": "string"},
        "length": {"type": "string"},
        "output_mode": {
            "type": "string",
            "enum": ["final", "alternatives", "edit-with-notes", "diff-summary"],
            "default": "final",
        },
        "project_context": {
            "type": "string",
            "enum": ["off", "auto", "git-summary", "git-diff"],
            "default": "off",
        },
        "project_root": {
            "type": "string",
            "default": ".",
            "description": "Explicit repository root for stateless project context access.",
        },
        "max_project_context_chars": {"type": "integer", "minimum": 1000, "maximum": 50000},
        "model": {"type": "string", "default": writing.DEFAULT_MODEL},
        "temperature": {"type": "number"},
        "max_tokens": {"type": "integer", "minimum": 1},
        "timeout_sec": {"type": "integer", "minimum": 20, "maximum": 600},
        "retry_count": {"type": "integer", "minimum": 0, "maximum": 5, "default": 1},
        "retry_sleep_cap_sec": {"type": "number", "minimum": 0, "maximum": 30, "default": 8},
    },
    "additionalProperties": False,
}

RELEASE_SNAPSHOT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "repo": {
            "type": "string",
            "default": ".",
            "description": "Explicit repository root; broad and sensitive roots are blocked.",
        },
        "base_ref": {"type": "string"},
        "head_ref": {"type": "string", "default": "HEAD"},
    },
    "additionalProperties": False,
}

RELEASE_DRAFT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        **RELEASE_SNAPSHOT_SCHEMA["properties"],
        "title": {"type": "string"},
        "version": {"type": "string"},
        "tag": {"type": "string"},
        "polish": {"type": "boolean", "default": False},
        "model": {"type": "string", "default": writing.DEFAULT_MODEL},
        "max_tokens": {"type": "integer", "minimum": 1},
        "timeout_sec": {"type": "integer", "minimum": 20, "maximum": 600},
    },
    "additionalProperties": False,
}

ROUTE_MODEL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "enum": ["chat", "code", "fast", "grounded-search", "search", "writing", "release", "image"],
            "default": "chat",
        },
        "intent": {"type": "string"},
        "preferred_model": {"type": "string"},
        "speed": {
            "type": "string",
            "enum": ["balanced", "fast", "low-latency", "quality"],
            "default": "balanced",
        },
        "grounding": {"type": "string", "enum": ["off", "auto", "always", "required"]},
        "image": {"type": "boolean"},
    },
    "additionalProperties": False,
}

CLI_CHAT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "model": {"type": "string"},
        "agent": {"type": "string"},
        "mode": {"type": "string", "enum": ["accept-edits", "plan"], "default": "plan"},
        "sandbox": {"type": "boolean", "default": True},
        "cwd": {
            "type": "string",
            "description": "Required only for explicitly opted-in accept-edits mode; blocked in plan mode.",
        },
        "timeout_sec": {
            "type": "integer",
            "minimum": 20,
            "maximum": 1800,
            "default": 300,
        },
    },
    "required": ["prompt"],
    "additionalProperties": False,
}

COMMON_OUTPUT_SCHEMA: Dict[str, Any] = {
    "$schema": JSON_SCHEMA_DIALECT,
    "type": "object",
    "description": "Structured result or a structured, secret-safe tool error.",
    "additionalProperties": True,
}

TOOL_METADATA: Dict[str, Dict[str, Any]] = {
    "google_antigravity_consent_status": {
        "title": "Check Antigravity Consent",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_provider_status": {
        "title": "Check Antigravity Provider",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    "google_antigravity_agy_auth_status": {
        "title": "Check agy Session Export",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_agy_auth_refresh": {
        "title": "Refresh Plugin OAuth Token",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    "google_antigravity_login_status": {
        "title": "Check Direct Antigravity Login",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_login_start": {
        "title": "Start Antigravity Google Login",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    "google_antigravity_login_complete": {
        "title": "Complete Antigravity Google Login",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    "google_antigravity_chat": {
        "title": "Chat with Antigravity",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    "google_grounded_search": {
        "title": "Search with Google Grounding",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    "google_antigravity_generate_image": {
        "title": "Generate an Antigravity Image",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    "google_antigravity_write": {
        "title": "Write with Antigravity",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    "google_antigravity_release_snapshot": {
        "title": "Collect Release Snapshot",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_release_draft": {
        "title": "Draft Release Materials",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    "google_antigravity_list_models": {
        "title": "List Antigravity Models",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    "google_antigravity_route_model": {
        "title": "Route to an Antigravity Model",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_get_model_prefs": {
        "title": "Get Saved Antigravity Models",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_set_model": {
        "title": "Set Antigravity Model Preference",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_clear_model_prefs": {
        "title": "Clear Antigravity Model Preferences",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_set_provider": {
        "title": "Set Preferred Provider",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_get_session_prefs": {
        "title": "Get Session Preferences",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_list_profiles": {
        "title": "List Session Profiles",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_use_profile": {
        "title": "Activate Session Profile",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_save_profile": {
        "title": "Save Custom Session Profile",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_whoami": {
        "title": "Show Antigravity Account",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    "google_antigravity_logout": {
        "title": "Logout / Forget Tokens",
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    "google_antigravity_compare_models": {
        "title": "Compare Models",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    "google_antigravity_review_diff": {
        "title": "Review Git Diff",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    "google_antigravity_quota_status": {
        "title": "Check Antigravity Quota",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
}


def tool_definitions() -> List[Dict[str, Any]]:
    definitions = [
        {
            "name": "google_antigravity_consent_status",
            "description": (
                "Read the current explicit-consent state and opt-in environment variable names. "
                "This tool cannot grant or modify consent."
            ),
            "inputSchema": _schema_auth_empty(),
        },
        {
            "name": "google_antigravity_provider_status",
            "description": (
                "Check the plugin OAuth (agy-oauth / Code Assist) provider without exposing credentials."
            ),
            "inputSchema": _schema_auth_empty(),
        },
        {
            "name": "google_antigravity_agy_auth_status",
            "description": (
                "Check whether a plugin OAuth token file is available without returning token values."
            ),
            "inputSchema": _schema_auth_empty(),
        },
        {
            "name": "google_antigravity_agy_auth_refresh",
            "description": (
                "Refresh the plugin OAuth access token via Google's token endpoint using the stored refresh token."
            ),
            "inputSchema": _schema_auth_empty(),
        },
        {
            "name": "google_antigravity_login_status",
            "description": (
                "Check whether a direct Google Antigravity OAuth login (PKCE) token is present. "
                "Never returns token values."
            ),
            "inputSchema": _schema_auth_empty(),
        },
        {
            "name": "google_antigravity_login_start",
            "description": (
                "Start a direct Google Antigravity OAuth login (same PKCE flow as the Hermes "
                "google-antigravity plugin). Returns an auth_url for the user to open; finish with "
                "google_antigravity_login_complete. Requires prior consent grant."
            ),
            "inputSchema": LOGIN_START_SCHEMA,
        },
        {
            "name": "google_antigravity_login_complete",
            "description": (
                "Complete a direct Google Antigravity OAuth login by providing the browser redirect "
                "URL or authorization code from google_antigravity_login_start. Saves tokens for "
                "the agy-oauth provider (chat, grounding, image)."
            ),
            "inputSchema": LOGIN_COMPLETE_SCHEMA,
        },
        {
            "name": "google_antigravity_chat",
            "description": "Send a chat request through the selected agy provider.",
            "inputSchema": CHAT_SCHEMA,
        },
        {
            "name": "google_grounded_search",
            "description": (
                "Answer a current/source-backed question through native Google Search grounding. "
                "Requires plugin Google OAuth login (agy-oauth)."
            ),
            "inputSchema": GROUNDING_SCHEMA,
        },
        {
            "name": "google_antigravity_generate_image",
            "description": (
                "Generate an image through Google Antigravity and cache it locally. "
                "Requires plugin Google OAuth login (agy-oauth)."
            ),
            "inputSchema": IMAGE_SCHEMA,
        },
        {
            "name": "google_antigravity_write",
            "description": "Draft, rewrite, polish, translate, summarize, or prepare public prose through Antigravity chat.",
            "inputSchema": WRITING_SCHEMA,
        },
        {
            "name": "google_antigravity_release_snapshot",
            "description": "Collect a local git release snapshot: branch, diff, commits, versions, tools, checks, and bump suggestion.",
            "inputSchema": RELEASE_SNAPSHOT_SCHEMA,
        },
        {
            "name": "google_antigravity_release_draft",
            "description": "Draft PR descriptions, release notes, and changelog entries from a local git release snapshot.",
            "inputSchema": RELEASE_DRAFT_SCHEMA,
        },
        {
            "name": "google_antigravity_list_models",
            "description": "List text and image models visible to Google Antigravity.",
            "inputSchema": _schema_auth_empty(),
        },
        {
            "name": "google_antigravity_route_model",
            "description": (
                "Recommend an Antigravity model and MCP tool for a task. "
                "Honors saved user model prefs and preferred_model overrides."
            ),
            "inputSchema": ROUTE_MODEL_SCHEMA,
        },
        {
            "name": "google_antigravity_get_model_prefs",
            "description": (
                "Read saved default/per-task Antigravity model preferences and effective resolved models."
            ),
            "inputSchema": _schema_auth_empty(),
        },
        {
            "name": "google_antigravity_set_model",
            "description": (
                "Persist the preferred Antigravity model (global default or per-task: chat, code, "
                "fast, writing, grounded-search, release, image). Subsequent chat/write/search/image "
                "calls use it when model is omitted."
            ),
            "inputSchema": SET_MODEL_SCHEMA,
        },
        {
            "name": "google_antigravity_clear_model_prefs",
            "description": "Clear saved Antigravity model preferences (default, one task, or all).",
            "inputSchema": CLEAR_MODEL_PREFS_SCHEMA,
        },
        {
            "name": "google_antigravity_set_provider",
            "description": "Persist preferred transport (agy-oauth only). Env still wins when set.",
            "inputSchema": SET_PROVIDER_SCHEMA,
        },
        {
            "name": "google_antigravity_get_session_prefs",
            "description": "Read preferred provider and active session profile.",
            "inputSchema": _schema_auth_empty(),
        },
        {
            "name": "google_antigravity_list_profiles",
            "description": "List built-in and custom session profiles (coding, writing, research, fast, pair, …).",
            "inputSchema": _schema_auth_empty(),
        },
        {
            "name": "google_antigravity_use_profile",
            "description": "Activate a session profile (sets active profile and optionally model/provider prefs).",
            "inputSchema": USE_PROFILE_SCHEMA,
        },
        {
            "name": "google_antigravity_save_profile",
            "description": "Save or override a custom session profile.",
            "inputSchema": SAVE_PROFILE_SCHEMA,
        },
        {
            "name": "google_antigravity_whoami",
            "description": "Show signed-in email / project id for the local OAuth token without returning secrets.",
            "inputSchema": _schema_auth_empty(),
        },
        {
            "name": "google_antigravity_logout",
            "description": "Delete local plugin OAuth tokens (not Google revoke, not agy Keychain).",
            "inputSchema": LOGOUT_SCHEMA,
        },
        {
            "name": "google_antigravity_compare_models",
            "description": "Run the same short prompt on 2–3 models and return side-by-side results (cost-aware cap).",
            "inputSchema": COMPARE_SCHEMA,
        },
        {
            "name": "google_antigravity_review_diff",
            "description": "Collect a local git diff and review it with Antigravity (bugs, security, missing tests).",
            "inputSchema": REVIEW_DIFF_SCHEMA,
        },
        {
            "name": "google_antigravity_quota_status",
            "description": "Report agy provider readiness and whether quota data is available.",
            "inputSchema": _schema_auth_empty(),
        },
    ]
    enriched: List[Dict[str, Any]] = []
    for definition in definitions:
        item = dict(definition)
        input_schema = dict(item["inputSchema"])
        input_schema.setdefault("$schema", JSON_SCHEMA_DIALECT)
        item["inputSchema"] = input_schema
        item["outputSchema"] = dict(COMMON_OUTPUT_SCHEMA)
        item.update(TOOL_METADATA[item["name"]])
        enriched.append(item)
    return enriched


def _text_result(text: str, structured: Dict[str, Any], *, is_error: bool = False) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": is_error,
    }


def _emit_notification(method: str, params: Dict[str, Any]) -> None:
    """Write a JSON-RPC notification to stdout (MCP stream/progress side-channel)."""
    print(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False), flush=True)


def _safe_call(
    func: Callable[..., Dict[str, Any]],
    arguments: Dict[str, Any],
    *,
    progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    try:
        if progress is not None and func is chat.run_chat:
            data = func(arguments, progress=progress)
        else:
            data = func(arguments)
        text = data.get("text") or data.get("answer") or json.dumps(data, ensure_ascii=False, indent=2)
        return _text_result(str(text), data)
    except (
        agy_auth.AgyAuthError,
        antigravity_api.AntigravityApiError,
        provider.ProviderError,
        oauth_login.OAuthLoginError,
        model_prefs.ModelPrefsError,
        session_prefs.SessionPrefsError,
        profiles.ProfileError,
        account.AccountError,
    ) as exc:
        return _text_result(
            str(exc),
            {
                "error_type": exc.code,
                "status_code": getattr(exc, "status_code", None),
                "provider": "google-antigravity",
                "backend": "agy-session",
            },
            is_error=True,
        )
    except Exception as exc:
        return _text_result(
            str(exc),
            {"error_type": type(exc).__name__, "provider": "google-antigravity"},
            is_error=True,
        )


def _provider_status(_: Dict[str, Any]) -> Dict[str, Any]:
    state = provider.status(probe=True)
    configured = bool(state.get("configured"))
    healthy = state.get("healthy")
    if not configured:
        text = "An agy session provider is not configured."
        warnings = ["agy_provider_not_configured"]
    elif healthy is True:
        text = "The selected agy provider is ready for its reported readiness scope."
        warnings = []
    else:
        text = "The selected agy provider is not ready."
        warnings = [str(state.get("error_type") or "agy_provider_not_ready")]
    return {
        "text": text,
        "provider_status": state,
        **response.standard_fields(
            success=healthy is True,
            backend=str(state.get("backend") or "agy-session"),
            warnings=warnings,
        ),
    }


def dispatch_tool(
    name: str,
    arguments: Dict[str, Any],
    *,
    progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    table: Dict[str, Callable[..., Dict[str, Any]]] = {
        "google_antigravity_consent_status": lambda args: {
            "text": json.dumps(security.consent_status(), indent=2),
            **security.consent_status(),
        },
        "google_antigravity_provider_status": _provider_status,
        "google_antigravity_agy_auth_status": agy_auth.status_tool,
        "google_antigravity_agy_auth_refresh": agy_auth.refresh_tool,
        "google_antigravity_login_status": oauth_login.status_tool,
        "google_antigravity_login_start": oauth_login.start_tool,
        "google_antigravity_login_complete": oauth_login.complete_tool,
        "google_antigravity_chat": chat.run_chat,
        "google_grounded_search": grounding.run_grounded_search,
        "google_antigravity_generate_image": image.generate_image,
        "google_antigravity_write": writing.run_writing,
        "google_antigravity_release_snapshot": release.release_snapshot,
        "google_antigravity_release_draft": release.release_draft,
        "google_antigravity_list_models": models.list_models,
        "google_antigravity_route_model": routing.route_model,
        "google_antigravity_get_model_prefs": model_prefs.get_prefs_tool,
        "google_antigravity_set_model": model_prefs.set_model_tool,
        "google_antigravity_clear_model_prefs": model_prefs.clear_prefs_tool,
        "google_antigravity_set_provider": session_prefs.set_provider_tool,
        "google_antigravity_get_session_prefs": session_prefs.get_session_prefs,
        "google_antigravity_list_profiles": profiles.list_profiles_tool,
        "google_antigravity_use_profile": profiles.use_profile_tool,
        "google_antigravity_save_profile": profiles.save_custom_profile_tool,
        "google_antigravity_whoami": account.whoami,
        "google_antigravity_logout": account.logout,
        "google_antigravity_compare_models": compare.compare_models,
        "google_antigravity_review_diff": diff_review.review_diff,
        "google_antigravity_quota_status": quota.quota_status,
    }
    if name not in table:
        raise ValueError(f"unknown tool: {name}")
    return _safe_call(table[name], arguments, progress=progress)


def _modern_request_protocol(message: Dict[str, Any]) -> str:
    params = message.get("params") or {}
    if not isinstance(params, dict):
        raise RpcError(-32602, "params must be an object")
    metadata = params.get("_meta") or {}
    if not isinstance(metadata, dict):
        raise RpcError(-32602, "params._meta must be an object")
    version = str(metadata.get(MODERN_META_PROTOCOL) or "")
    if not version:
        if message.get("method") == "server/discover":
            raise RpcError(-32602, "server/discover requires the modern protocol _meta")
        return ""
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise RpcError(
            -32022,
            f"Unsupported MCP protocol version: {version}",
            data={"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": version},
        )
    if version == MODERN_PROTOCOL_VERSION:
        missing = [
            key
            for key in (MODERN_META_CLIENT_INFO, MODERN_META_CLIENT_CAPABILITIES)
            if key not in metadata
        ]
        if missing:
            raise RpcError(-32602, f"modern MCP _meta is missing: {', '.join(missing)}")
    return version


def _discovery_result() -> Dict[str, Any]:
    return {
        "resultType": "complete",
        "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "Antigravity tools are consent-gated. Pass repository and workspace roots "
            "explicitly; filesystem-wide, home, and sensitive roots are blocked."
        ),
        "ttlMs": DISCOVERY_TTL_MS,
        "cacheScope": "public",
    }


def _complete_modern_result(method: str, result: Dict[str, Any]) -> Dict[str, Any]:
    modern = dict(result)
    modern.setdefault("resultType", "complete")
    if method == "tools/list":
        modern.setdefault("ttlMs", DISCOVERY_TTL_MS)
        modern.setdefault("cacheScope", "public")
    return modern


def handle_request(message: Dict[str, Any]) -> Dict[str, Any] | None:
    request_id = message.get("id")
    if request_id is None:
        return None
    method = message.get("method")
    try:
        protocol = _modern_request_protocol(message)
        modern = protocol == MODERN_PROTOCOL_VERSION
        if modern and method in {"initialize", "ping"}:
            raise RpcError(-32601, f"{method} is not available in stateless MCP {protocol}")
        if method == "server/discover":
            if not modern:
                raise RpcError(-32602, "server/discover requires the modern MCP protocol")
            result = _discovery_result()
        elif method == "initialize":
            params = message.get("params") or {}
            requested_protocol = str(params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION)
            selected_protocol = (
                requested_protocol
                if requested_protocol in LEGACY_PROTOCOL_VERSIONS
                else DEFAULT_PROTOCOL_VERSION
            )
            result = {
                "protocolVersion": selected_protocol,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_definitions()}
        elif method == "tools/call":
            params = message.get("params") or {}
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise RpcError(-32602, "tool arguments must be an object")
            if name not in {tool["name"] for tool in tool_definitions()}:
                raise RpcError(-32602, f"unknown tool: {name}")
            # Enable stream side-channel for chat when requested.
            progress = _emit_notification if (name == "google_antigravity_chat" and arguments.get("stream")) else None
            result = dispatch_tool(name, arguments, progress=progress)
        else:
            raise RpcError(-32601, f"unsupported method: {method}")
        if modern:
            result = _complete_modern_result(str(method), result)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except RpcError as exc:
        error: Dict[str, Any] = {"code": exc.code, "message": str(exc)}
        if exc.data is not None:
            error["data"] = exc.data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}}


def serve() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        else:
            response = handle_request(message)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0
