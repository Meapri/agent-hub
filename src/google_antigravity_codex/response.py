"""Shared response helpers for MCP tools."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


DEFAULT_BACKEND = "local"
DEFAULT_PROVIDER = "google-antigravity"


def warning_list(*groups: Iterable[Any]) -> List[str]:
    seen = set()
    result: List[str] = []
    for group in groups:
        for item in group or []:
            text = str(item or "").strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
    return result


def standard_fields(
    *,
    success: bool = True,
    provider: str = DEFAULT_PROVIDER,
    backend: str = DEFAULT_BACKEND,
    model: str = "",
    usage: Dict[str, Any] | None = None,
    warnings: Iterable[Any] | None = None,
    diagnostics: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "success": bool(success),
        "provider": provider,
        "backend": backend,
        "warnings": warning_list(warnings or []),
    }
    if model:
        data["model"] = model
    if usage is not None:
        data["usage"] = usage
    if diagnostics:
        data["diagnostics"] = diagnostics
    return data
