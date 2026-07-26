from __future__ import annotations

import pytest

from agent_hub.connect_egress import EgressGateway
from agent_hub.connect_types import ConnectionError
from agent_hub.v2.errors import HubV2Error


def test_non_daemon_gateway_returns_read_only_empty_state():
    gateway = EgressGateway(lambda: pytest.fail("daemon client must not be created"))

    assert gateway.reviews(use_daemon=False) == {
        "success": True,
        "schema": "agent_hub_egress_review_list_v1",
        "reviews": [],
        "pending_count": 0,
        "approved_count": 0,
    }
    assert gateway.settings(use_daemon=False)["auto_approve"] is False
    with pytest.raises(ConnectionError) as error:
        gateway.update_settings(
            use_daemon=False,
            auto_approve=True,
            expected_revision=0,
        )
    assert error.value.code == "daemon_required"


def test_daemon_transport_error_is_reduced_to_safe_connection_error():
    class FailingClient:
        def request(self, *_args, **_kwargs):
            raise HubV2Error(
                code="daemon_unavailable",
                message="private socket detail",
            )

    gateway = EgressGateway(FailingClient)

    with pytest.raises(ConnectionError) as error:
        gateway.reviews(use_daemon=True)
    assert error.value.code == "daemon_unavailable"
    assert "private socket detail" not in str(error.value)


def test_malformed_daemon_success_response_is_rejected():
    class MalformedClient:
        def request(self, *_args, **_kwargs):
            return {"success": True, "data": "not-an-object"}

    gateway = EgressGateway(MalformedClient)

    with pytest.raises(ConnectionError) as error:
        gateway.settings(use_daemon=True)
    assert error.value.code == "egress_settings_unavailable"
