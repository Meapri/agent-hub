"""Deterministic, shell-free output verifiers."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping

from .errors import HubV2Error


def verify_output(text: str, verifier: Mapping[str, Any] | None) -> dict[str, Any]:
    rule = dict(verifier or {})
    kind = str(rule.get("type") or "non_empty")
    passed = False
    if kind == "non_empty":
        passed = bool(text.strip())
    elif kind == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError:
            passed = False
        else:
            passed = True
    elif kind == "contains":
        expected = str(rule.get("value") or "")
        if not expected or len(expected) > 10_000:
            raise HubV2Error(
                "invalid_verifier",
                "The contains verifier value is invalid.",
                scope="verifier",
            )
        passed = expected in text
    elif kind == "sha256":
        expected = str(rule.get("value") or "")
        passed = len(expected) == 64 and sha256(text.encode("utf-8")).hexdigest() == expected
    else:
        raise HubV2Error(
            "invalid_verifier",
            "The verifier type is not supported.",
            scope="verifier",
        )
    result = {
        "schema": "agent_hub_verification_v1",
        "type": kind,
        "passed": passed,
        "content_sha256": sha256(text.encode("utf-8")).hexdigest(),
    }
    if not passed:
        raise HubV2Error(
            "deterministic_verification_failed",
            "The step output failed its deterministic verifier.",
            scope="verifier",
            retryable=True,
            safe_details={"verifier_type": kind},
        )
    return result
