"""Claude Code subscription request fingerprint for plan-lane billing.

Adapted from Meapri/hermes-claude-auth (MIT), which ports
griffinmartin/opencode-claude-auth. Pure helper for Claude Codex — no Hermes
monkey-patch.

OAuth (subscription) tokens require Claude Code-shaped requests; otherwise
Anthropic routes traffic to third-party / extra-usage billing.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
from pathlib import Path
from typing import Any, Dict, List, Tuple

__version__ = "1.5.8-codex"

_BILLING_SALT = "59cf53e54c78"
_BILLING_ENTRYPOINT = "sdk-cli"
_BILLING_PREFIX = "x-anthropic-billing-header"
_SYSTEM_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
_STAINLESS_PACKAGE_VERSION = "0.81.0"
_STAINLESS_NODE_VERSION = "v22.11.0"
_DEFAULT_CC_VERSION = "2.1.112"


def _extract_first_user_message_text(messages: List[Dict[str, Any]]) -> str:
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        return text
        return ""
    return ""


def _compute_cch(message_text: str) -> str:
    return hashlib.sha256(message_text.encode("utf-8")).hexdigest()[:5]


def _compute_version_suffix(message_text: str, version: str) -> str:
    sampled = "".join(message_text[i] if i < len(message_text) else "0" for i in (4, 7, 20))
    input_str = f"{_BILLING_SALT}{sampled}{version}"
    return hashlib.sha256(input_str.encode("utf-8")).hexdigest()[:3]


def _build_billing_header_value(messages: List[Dict[str, Any]], version: str) -> str:
    text = _extract_first_user_message_text(messages)
    suffix = _compute_version_suffix(text, version)
    cch = _compute_cch(text)
    return (
        f"x-anthropic-billing-header: "
        f"cc_version={version}.{suffix}; "
        f"cc_entrypoint={_BILLING_ENTRYPOINT}; "
        f"cch={cch};"
    )


def _stainless_arch() -> str:
    machine = (platform.machine() or "").lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("i386", "i686"):
        return "ia32"
    return machine or "unknown"


def _stainless_os() -> str:
    return {"Darwin": "MacOS", "Linux": "Linux", "Windows": "Windows"}.get(
        platform.system(), platform.system() or "Unknown"
    )


def stainless_headers() -> Dict[str, str]:
    return {
        "anthropic-dangerous-direct-browser-access": "true",
        "x-stainless-arch": _stainless_arch(),
        "x-stainless-lang": "js",
        "x-stainless-os": _stainless_os(),
        "x-stainless-package-version": _STAINLESS_PACKAGE_VERSION,
        "x-stainless-retry-count": "0",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": _STAINLESS_NODE_VERSION,
        "x-stainless-timeout": "600",
    }


def account_metadata() -> Dict[str, Any]:
    path = Path.home() / ".claude.json"
    if not path.is_file():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    oauth = config.get("oauthAccount") if isinstance(config, dict) else None
    if isinstance(oauth, dict) and isinstance(oauth.get("accountUuid"), str):
        return {"user_id": oauth["accountUuid"]}
    return {}


def detect_claude_code_version() -> str:
    return os.getenv("CLAUDE_CODE_VERSION", "").strip() or _DEFAULT_CC_VERSION


def _prepend_to_first_user_message(messages: List[Dict[str, Any]], texts: List[str]) -> None:
    if not texts:
        return
    reminder = "\n".join(f"<system-reminder>\n{t}\n</system-reminder>" for t in texts if t)
    if not reminder:
        return
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = f"{reminder}\n\n{content}"
            return
        if isinstance(content, list):
            msg["content"] = [{"type": "text", "text": reminder}, *content]
            return
    messages.insert(0, {"role": "user", "content": reminder})


def apply_subscription_fingerprint(
    body: Dict[str, Any],
    *,
    version: str | None = None,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Shape a Messages body for Claude Code OAuth plan-lane billing.

    Returns ``(body, extra_headers)``.
    """
    out = dict(body)
    messages = out.get("messages")
    if not isinstance(messages, list) or not messages:
        return out, stainless_headers()
    messages = [dict(m) if isinstance(m, dict) else m for m in messages]
    out["messages"] = messages
    version = version or detect_claude_code_version()

    raw_system = out.get("system")
    if raw_system is None:
        system: List[Any] = []
    elif isinstance(raw_system, str):
        system = [{"type": "text", "text": raw_system}] if raw_system else []
    elif isinstance(raw_system, list):
        system = list(raw_system)
    else:
        system = []

    billing_entry = {"type": "text", "text": _build_billing_header_value(messages, version)}
    kept: List[Any] = []
    moved_texts: List[str] = []
    identity_seen = False
    for entry in system:
        if not isinstance(entry, dict) or entry.get("type") != "text":
            kept.append(entry)
            continue
        text = str(entry.get("text") or "")
        if text.startswith(_BILLING_PREFIX):
            continue
        if text.startswith(_SYSTEM_IDENTITY):
            if identity_seen:
                continue
            identity_seen = True
            rest = text[len(_SYSTEM_IDENTITY) :].lstrip("\n")
            kept.append({"type": "text", "text": _SYSTEM_IDENTITY})
            if rest:
                moved_texts.append(rest)
            continue
        if text:
            moved_texts.append(text)

    if not identity_seen:
        kept.insert(0, {"type": "text", "text": _SYSTEM_IDENTITY})
    out["system"] = [billing_entry, *kept]
    if moved_texts:
        _prepend_to_first_user_message(messages, moved_texts)

    model = str(out.get("model") or "").lower()
    if "haiku" in model:
        out.pop("output_config", None)
    if "opus" in model and "4.6" in model:
        temp = out.get("temperature")
        if temp is not None and float(temp) != 1.0:
            out.pop("temperature", None)

    meta = account_metadata()
    if meta:
        existing = out.get("metadata") if isinstance(out.get("metadata"), dict) else {}
        out["metadata"] = {**meta, **existing}

    return out, stainless_headers()
