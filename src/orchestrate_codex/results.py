"""Bounded, provider-neutral leaf results for brokered workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import re
from typing import Any, Dict, Mapping, Tuple

RESULT_SCHEMA = "operation_result_v1"
MAX_TEXT_CHARS = 2_000_000
MAX_ERROR_CHARS = 4_000
MAX_WARNINGS = 32
MAX_ARTIFACTS = 32
MAX_FIELD_CHARS = 2_048

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r'(?i)("?(?:access|refresh|id)_token"?\s*[:=]\s*")[^"]+(")'),
    re.compile(
        r"(?i)\b(?:access|refresh|id)_token\s*[:=]\s*"
        r"(?:'[^']*'|[^\s,;}\]]+)"
    ),
    re.compile(
        r"(?i)\b(?:authorization|api[-_ ]?key)\s*[:=]\s*"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
    ),
)
_PROVENANCE_KEYS = {
    "handoff_sha256",
    "policy_chars",
    "policy_loaded",
    "policy_mode",
    "policy_sha256",
    "policy_source",
    "request_sha256",
    "source",
    "source_sha256",
}
_ARTIFACT_KEYS = {
    "bytes",
    "kind",
    "mime_type",
    "name",
    "path",
    "sha256",
    "size",
    "type",
    "uri",
}


def _redact(value: Any, *, limit: int) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 2:
            text = pattern.sub(r"\1[redacted]\2", text)
        else:
            text = pattern.sub("[redacted]", text)
    return text[:limit]


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact(value, limit=MAX_FIELD_CHARS)


def _usage(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key)[:64]: raw
        for key, raw in list(value.items())[:32]
        if isinstance(raw, (int, float)) and not isinstance(raw, bool)
    }


def _warnings(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    seen = set()
    items = []
    for raw in value[:MAX_WARNINGS]:
        warning = _redact(raw, limit=MAX_FIELD_CHARS).strip()
        if warning and warning not in seen:
            items.append(warning)
            seen.add(warning)
    return tuple(items)


def _error(value: Any, *, fallback: str = "") -> Dict[str, Any] | None:
    if not value and not fallback:
        return None
    if isinstance(value, Mapping):
        result = {
            str(key): _scalar(value[key])
            for key in ("type", "code", "message")
            if key in value
        }
        if result:
            return result
    message = _redact(value or fallback, limit=MAX_ERROR_CHARS).strip()
    return {"message": message} if message else None


def _provenance(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: _scalar(value[key])
        for key in sorted(_PROVENANCE_KEYS)
        if key in value
    }


def _artifacts(value: Any) -> Tuple[Dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    items = []
    for raw in value[:MAX_ARTIFACTS]:
        if not isinstance(raw, Mapping):
            continue
        artifact = {
            key: _scalar(raw[key])
            for key in sorted(_ARTIFACT_KEYS)
            if key in raw and not (key == "uri" and str(raw[key]).startswith("data:"))
        }
        if artifact:
            items.append(artifact)
    return tuple(items)


@dataclass(frozen=True)
class OperationResult:
    """Safe projection of a provider result for traces and persisted run state."""

    success: bool
    text: str
    error: Dict[str, Any] | None = None
    provider: str | None = None
    model: str | None = None
    usage: Dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    warnings: Tuple[str, ...] = ()
    artifacts: Tuple[Dict[str, Any], ...] = ()
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema": RESULT_SCHEMA,
            "success": self.success,
            "text": self.text,
            "error": self.error,
            "provider": self.provider,
            "model": self.model,
            "usage": dict(self.usage),
            "finish_reason": self.finish_reason,
            "warnings": list(self.warnings),
            "artifacts": [dict(item) for item in self.artifacts],
            "provenance": dict(self.provenance),
        }

    def as_state_dict(self) -> Dict[str, Any]:
        """Store metadata once while the existing step.result_text owns the text."""

        value = self.as_dict()
        value.pop("text", None)
        value["text_ref"] = "result_text"
        value["text_chars"] = len(self.text)
        value["text_sha256"] = sha256(self.text.encode("utf-8")).hexdigest()
        return value

    @classmethod
    def from_result(
        cls,
        result: Any,
        *,
        success: bool | None = None,
        text: str = "",
        error: Any = None,
    ) -> "OperationResult":
        payload = result if isinstance(result, Mapping) else {}
        structured = payload.get("structuredContent")
        if isinstance(structured, Mapping):
            payload = structured
        result_text = text or _extract_text(result)
        resolved_success = bool(
            success
            if success is not None
            else not (
                isinstance(result, Mapping)
                and bool(result.get("isError"))
                or payload.get("success") is False
            )
        )
        raw_error = error if error is not None else payload.get("error")
        if not raw_error and not resolved_success and payload.get("error_type"):
            raw_error = {
                "type": payload.get("error_type"),
                "code": payload.get("status_code"),
                "message": result_text,
            }
        provenance = payload.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = payload.get("consistency")
        return cls(
            success=resolved_success,
            text=_redact(result_text, limit=MAX_TEXT_CHARS),
            error=_error(raw_error, fallback=result_text if not resolved_success else ""),
            provider=(
                _redact(payload.get("provider"), limit=128).strip() or None
            ),
            model=_redact(payload.get("model"), limit=256).strip() or None,
            usage=_usage(payload.get("usage")),
            finish_reason=(
                _redact(payload.get("finish_reason"), limit=128).strip() or None
            ),
            warnings=_warnings(payload.get("warnings")),
            artifacts=_artifacts(payload.get("artifacts")),
            provenance=_provenance(provenance),
        )


def _extract_text(result: Any) -> str:
    if not isinstance(result, Mapping):
        return str(result or "")
    content = result.get("content")
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        ]
        joined = "\n".join(part for part in parts if part)
        if joined:
            return joined
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping) and isinstance(structured.get("text"), str):
        return structured["text"]
    if isinstance(result.get("text"), str):
        return result["text"]
    return str(result or "")
