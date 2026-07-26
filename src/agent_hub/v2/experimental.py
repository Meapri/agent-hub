"""Fail-closed feature gates for optional v2 runtime extensions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .errors import HubV2Error

EXPERIMENTAL_FEATURES = (
    "isolated_tool_worker",
    "local_model",
    "remote_worker",
)


def normalize_experimental_flags(value: Mapping[str, Any] | None) -> dict[str, bool]:
    raw = value or {}
    unknown = sorted(set(raw) - set(EXPERIMENTAL_FEATURES))
    if unknown:
        raise HubV2Error(
            "invalid_policy",
            "The project policy contains an unknown experimental feature.",
            scope="policy",
            safe_details={"unknown_features": unknown},
        )
    normalized: dict[str, bool] = {}
    for feature in EXPERIMENTAL_FEATURES:
        enabled = raw.get(feature, False)
        if not isinstance(enabled, bool):
            raise HubV2Error(
                "invalid_policy",
                "Experimental feature flags must be booleans.",
                scope="policy",
                safe_details={"feature": feature},
            )
        normalized[feature] = enabled
    return normalized


def require_experimental_feature(
    policy: Mapping[str, Any],
    feature: str,
) -> None:
    if feature not in EXPERIMENTAL_FEATURES:
        raise HubV2Error(
            "unknown_experimental_feature",
            "The experimental feature is not recognized.",
            scope="experimental",
        )
    flags = normalize_experimental_flags(
        policy.get("experimental") if isinstance(policy.get("experimental"), Mapping) else None
    )
    if not flags[feature]:
        raise HubV2Error(
            "experimental_feature_disabled",
            "The experimental runtime is disabled by project policy.",
            scope="experimental",
            next_action={
                "type": "policy_prepare_apply",
                "feature": feature,
            },
        )


@dataclass(frozen=True)
class ExperimentalRuntime:
    feature: str
    runtime_id: str
    sandboxed: bool


class ExperimentalRuntimeRegistry:
    """In-memory extension registry; Agent Hub never installs a backend itself."""

    def __init__(self) -> None:
        self._runtimes: dict[str, ExperimentalRuntime] = {}

    def register(
        self,
        runtime: ExperimentalRuntime,
        *,
        policy: Mapping[str, Any],
    ) -> dict[str, Any]:
        require_experimental_feature(policy, runtime.feature)
        if runtime.feature == "isolated_tool_worker" and not runtime.sandboxed:
            raise HubV2Error(
                "experimental_sandbox_required",
                "An experimental tool worker must declare sandbox isolation.",
                scope="experimental",
            )
        if not runtime.runtime_id or len(runtime.runtime_id) > 128:
            raise HubV2Error(
                "invalid_experimental_runtime",
                "The experimental runtime ID is invalid.",
                scope="experimental",
            )
        self._runtimes[runtime.feature] = runtime
        return self.status(policy)

    def status(self, policy: Mapping[str, Any]) -> dict[str, Any]:
        flags = normalize_experimental_flags(
            policy.get("experimental") if isinstance(policy.get("experimental"), Mapping) else None
        )
        return {
            "schema": "agent_hub_experimental_features_v1",
            "automatic_install": False,
            "features": {
                feature: {
                    "enabled": flags[feature],
                    "registered": feature in self._runtimes,
                    "runtime_id": (
                        self._runtimes[feature].runtime_id if feature in self._runtimes else None
                    ),
                    "sandboxed": (
                        self._runtimes[feature].sandboxed if feature in self._runtimes else None
                    ),
                }
                for feature in EXPERIMENTAL_FEATURES
            },
        }
