"""Daemon-backed egress approval operations for the connection GUI."""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping

from .connect_types import ConnectionError
from .v2.daemon import HubDaemonClient
from .v2.errors import HubV2Error


def _daemon_data(response: Mapping[str, Any], *, fallback_code: str) -> Dict[str, Any]:
    if response.get("success") is not True:
        error = response.get("error")
        safe_error = error if isinstance(error, Mapping) else {}
        raise ConnectionError(
            str(safe_error.get("message") or "Agent Hub daemon 요청을 완료하지 못했습니다."),
            code=str(safe_error.get("code") or fallback_code),
        )
    data = response.get("data")
    if not isinstance(data, Mapping):
        raise ConnectionError(
            "Agent Hub daemon 응답을 확인하지 못했습니다.",
            code=fallback_code,
        )
    return dict(data)


class EgressGateway:
    """Expose only the connection GUI's egress review API."""

    def __init__(
        self,
        daemon_client_factory: Callable[[], HubDaemonClient] = HubDaemonClient,
    ) -> None:
        self._daemon_client_factory = daemon_client_factory

    def reviews(self, *, use_daemon: bool) -> Dict[str, Any]:
        if not use_daemon:
            return {
                "success": True,
                "schema": "agent_hub_egress_review_list_v1",
                "reviews": [],
                "pending_count": 0,
                "approved_count": 0,
            }
        try:
            response = self._daemon_client_factory().request(
                "egress/reviews",
                timeout=5.0,
            )
        except HubV2Error as exc:
            raise ConnectionError(
                "외부 전송 검토 목록을 불러오지 못했습니다.",
                code=exc.code,
            ) from exc
        return {
            "success": True,
            **_daemon_data(response, fallback_code="egress_reviews_unavailable"),
        }

    def settings(self, *, use_daemon: bool) -> Dict[str, Any]:
        if not use_daemon:
            return {
                "success": True,
                "schema": "agent_hub_egress_settings_v1",
                "revision": 0,
                "auto_approve": False,
                "updated_at": 0.0,
            }
        try:
            response = self._daemon_client_factory().request(
                "egress/settings",
                timeout=5.0,
            )
        except HubV2Error as exc:
            raise ConnectionError(
                "전역 외부 전송 설정을 불러오지 못했습니다.",
                code=exc.code,
            ) from exc
        return {
            "success": True,
            **_daemon_data(response, fallback_code="egress_settings_unavailable"),
        }

    def update_settings(
        self,
        *,
        use_daemon: bool,
        auto_approve: bool,
        expected_revision: int,
    ) -> Dict[str, Any]:
        if not use_daemon:
            raise ConnectionError(
                "실행 중인 Agent Hub daemon에서만 전역 설정을 바꿀 수 있습니다.",
                code="daemon_required",
            )
        try:
            response = self._daemon_client_factory().request(
                "egress/settings/update",
                {
                    "auto_approve": auto_approve,
                    "expected_revision": expected_revision,
                },
                timeout=5.0,
            )
        except HubV2Error as exc:
            raise ConnectionError(
                "전역 외부 전송 설정을 저장하지 못했습니다.",
                code=exc.code,
            ) from exc
        return {
            "success": True,
            **_daemon_data(response, fallback_code="egress_settings_update_failed"),
        }

    def decide(
        self,
        review_id: str,
        *,
        use_daemon: bool,
        decision: str,
    ) -> Dict[str, Any]:
        if not use_daemon:
            raise ConnectionError(
                "실행 중인 Agent Hub daemon에서만 외부 전송을 승인할 수 있습니다.",
                code="daemon_required",
            )
        try:
            response = self._daemon_client_factory().request(
                "egress/decide",
                {"review_id": review_id, "decision": decision},
                timeout=5.0,
            )
        except HubV2Error as exc:
            raise ConnectionError(
                "외부 전송 검토를 저장하지 못했습니다.",
                code=exc.code,
            ) from exc
        return {
            "success": True,
            "review": _daemon_data(
                response,
                fallback_code="egress_review_failed",
            ),
        }
