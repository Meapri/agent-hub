from __future__ import annotations

from unittest.mock import patch

from google_antigravity_codex import quota


def test_quota_status_reports_provider_health_without_inventing_buckets():
    with patch.object(
        quota.provider,
        "status",
        return_value={
            "configured": True,
            "enabled": True,
            "healthy": True,
            "model_count": 4,
            "backend": "agy-cli",
        },
    ):
        result = quota.quota_status({})

    assert result["success"] is True
    assert result["quota_available"] is False
    assert result["buckets"] == []
    assert result["provider_status"]["model_count"] == 4
    assert result["warnings"] == ["quota_not_exposed_by_agy_provider"]


def test_quota_status_reports_unconfigured_provider_truthfully():
    with patch.object(
        quota.provider,
        "status",
        return_value={"configured": False, "enabled": False, "healthy": None, "error_type": ""},
    ):
        result = quota.quota_status({})

    assert result["success"] is False
    assert result["quota_available"] is False
    assert "agy_provider_not_configured" in result["warnings"]
