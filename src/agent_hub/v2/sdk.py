"""Provider and workflow SDK conformance helpers."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping

from .contracts import (
    PLAN_SCHEMA,
    PROVIDER_MANIFEST_SCHEMA,
    TASK_SCHEMA,
    ensure_public_model_id,
    validate_plan,
    validate_provider_manifest,
)
from .errors import HubV2Error
from .experimental import ExperimentalRuntime, ExperimentalRuntimeRegistry

WORKFLOW_SCHEMA = "workflow_v2"
__all__ = [
    "AuthStub",
    "ExperimentalRuntime",
    "ExperimentalRuntimeRegistry",
    "MockTransport",
    "TimeoutCancelFixture",
    "approve_provider_registration",
    "check_provider",
    "load_workflow",
    "prepare_provider_registration",
    "scan_redaction",
]
_FORBIDDEN_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "api_key",
        "authorization",
        "device_code",
        "oauth_code",
        "client_secret",
    }
)


@dataclass(frozen=True)
class ConformanceCheck:
    name: str
    passed: bool
    reason_code: str

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "reason_code": self.reason_code,
        }


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS or _contains_forbidden_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def scan_redaction(value: Any) -> dict[str, Any]:
    """Return safe field paths only; never echo a discovered secret value."""

    findings: list[str] = []

    def visit(item: Any, path: str) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}" if path else key_text
                if key_text.lower() in _FORBIDDEN_KEYS:
                    findings.append(child_path)
                else:
                    visit(child, child_path)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")

    visit(value, "")
    return {
        "schema": "agent_hub_redaction_scan_v1",
        "passed": not findings,
        "finding_paths": findings[:100],
        "truncated": len(findings) > 100,
    }


class MockTransport:
    """Scripted provider transport for conformance and adapter tests."""

    def __init__(self, responses: Mapping[str, Mapping[str, Any]]) -> None:
        self.responses = {key: dict(value) for key, value in responses.items()}
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(
            {
                "method": method,
                "params_sha256": sha256(
                    json.dumps(
                        dict(params),
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
        if method not in self.responses:
            raise HubV2Error(
                "mock_method_unavailable",
                "The mock transport has no scripted response.",
                scope="provider_sdk",
            )
        return dict(self.responses[method])


class AuthStub:
    """Test auth owner that reports state without exposing credential material."""

    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready

    def status(self) -> dict[str, Any]:
        return {
            "schema": "agent_hub_auth_stub_v1",
            "ready": self.ready,
            "credential_exposed": False,
        }


class TimeoutCancelFixture:
    """Deterministic blocking fixture used to verify timeout and cancellation."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def invoke(self, timeout: float) -> None:
        self.started.set()
        if not self.cancelled.wait(timeout):
            raise HubV2Error(
                "provider_timeout",
                "The provider fixture exceeded its time budget.",
                scope="provider",
                retryable=True,
            )

    def cancel(self) -> bool:
        self.cancelled.set()
        return True


def prepare_provider_registration(
    manifest: Mapping[str, Any],
    *,
    package_sha256: str,
) -> dict[str, Any]:
    normalized = validate_provider_manifest(manifest)
    if len(package_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in package_sha256
    ):
        raise HubV2Error(
            "invalid_package_digest",
            "The provider package digest is invalid.",
            scope="provider_sdk",
        )
    review = {
        "provider_id": normalized["provider_id"],
        "capabilities": normalized["capabilities"],
        "allowed_domains": normalized["allowed_domains"],
        "auth_owner": normalized["auth_owner"],
        "supports_cancel": normalized["supports_cancel"],
        "supports_streaming": normalized["supports_streaming"],
    }
    proposal = {
        "schema": "agent_hub_provider_registration_proposal_v1",
        "manifest": normalized,
        "package_sha256": package_sha256,
        "permission_review": review,
        "automatic_install": False,
    }
    proposal["proposal_sha256"] = sha256(
        json.dumps(
            proposal,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return proposal


def approve_provider_registration(
    proposal: Mapping[str, Any],
    *,
    proposal_sha256: str,
) -> dict[str, Any]:
    copy = dict(proposal)
    supplied = str(copy.pop("proposal_sha256", ""))
    calculated = sha256(
        json.dumps(
            copy,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if supplied != calculated or supplied != proposal_sha256:
        raise HubV2Error(
            "proposal_digest_conflict",
            "The provider registration digest does not match.",
            scope="provider_sdk",
        )
    return {
        "schema": "agent_hub_provider_registration_lock_v1",
        "provider_id": proposal["manifest"]["provider_id"],
        "package_sha256": proposal["package_sha256"],
        "manifest_sha256": sha256(
            json.dumps(
                proposal["manifest"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "approved": True,
        "approved_at": time.time(),
    }


def check_provider(
    provider_id: str,
    request: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    checks: list[ConformanceCheck] = []
    try:
        initialized = request("initialize", {})
        manifest = initialized.get("manifest")
        normalized = validate_provider_manifest(manifest)
        checks.append(
            ConformanceCheck(
                "initialize_manifest",
                normalized["provider_id"] == provider_id,
                "ok"
                if normalized["provider_id"] == provider_id
                else "provider_id_mismatch",
            )
        )
    except (HubV2Error, KeyError, TypeError):
        checks.append(ConformanceCheck("initialize_manifest", False, "invalid_manifest"))
        normalized = None
    for method in ("status", "catalog"):
        try:
            result = request(method, {})
            safe = not _contains_forbidden_key(result)
            checks.append(
                ConformanceCheck(
                    f"{method}_redaction",
                    safe,
                    "ok" if safe else "secret_field_exposed",
                )
            )
            if method == "catalog":
                models = result.get("models", [])
                if isinstance(models, list):
                    for model in models:
                        if isinstance(model, Mapping) and model.get("id"):
                            ensure_public_model_id(model["id"])
        except (HubV2Error, TypeError):
            checks.append(
                ConformanceCheck(
                    f"{method}_redaction",
                    False,
                    f"{method}_failed",
                )
            )
    return {
        "schema": "agent_hub_provider_conformance_v2",
        "provider_id": provider_id,
        "manifest_schema": PROVIDER_MANIFEST_SCHEMA,
        "passed": bool(normalized) and all(check.passed for check in checks),
        "checks": [check.public() for check in checks],
    }


def validate_workflow(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("schema") != WORKFLOW_SCHEMA:
        raise HubV2Error(
            "unsupported_workflow_schema",
            "The workflow package schema is not supported.",
            scope="workflow",
        )
    workflow_id = str(raw.get("id") or "")
    if not workflow_id or len(workflow_id) > 128:
        raise HubV2Error(
            "invalid_workflow",
            "The workflow id is invalid.",
            scope="workflow",
        )
    intent = str(raw.get("intent") or "")
    capability = str(raw.get("capability") or "")
    plan = validate_plan(
        {
            "schema": PLAN_SCHEMA,
            "task": {
                "schema": TASK_SCHEMA,
                "intent": intent,
                "capability": capability,
                "inline_input": "",
            },
            "steps": raw.get("steps"),
            "routing_mode": str(raw.get("routing_mode") or "shadow"),
            "policy_revision": 0,
        }
    )
    return {
        "schema": WORKFLOW_SCHEMA,
        "id": workflow_id,
        "version": str(raw.get("version") or "1"),
        "description": str(raw.get("description") or ""),
        "plan_template": plan,
    }


def load_workflow(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        parsed = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HubV2Error(
            "invalid_workflow",
            "The workflow package is not valid UTF-8 JSON.",
            scope="workflow",
        ) from exc
    return validate_workflow(parsed)
