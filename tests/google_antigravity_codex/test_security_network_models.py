from __future__ import annotations

from io import BytesIO
from pathlib import Path
import socket
from unittest.mock import patch

import pytest

from google_antigravity_codex import models, network, security


class Response:
    def __init__(self, data: bytes, headers=None):
        self._body = BytesIO(data)
        self.headers = headers or {}

    def read(self, size=-1):
        return self._body.read(size)


def public_dns(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


def test_public_url_validation_and_rejections(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", public_dns)
    assert network.validate_public_url("https://example.com/image.png").startswith("https://")
    with pytest.raises(ValueError, match="HTTPS"):
        network.validate_public_url("http://example.com/image.png")
    with pytest.raises(ValueError, match="embedded credentials"):
        network.validate_public_url("https://user:pass@example.com/image.png")


def test_public_url_rejects_dns_failures_and_empty_results(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [])
    with pytest.raises(ValueError, match="no addresses"):
        network.validate_public_url("https://example.test/image.png")

    def fail(*args, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(ValueError, match="could not be resolved"):
        network.validate_public_url("https://example.test/image.png")


def test_bounded_download_reader():
    assert network.read_limited(Response(b"image"), 5) == b"image"
    with pytest.raises(ValueError, match="size limit"):
        network.read_limited(Response(b"toolarge"), 4)
    with pytest.raises(ValueError, match="size limit"):
        network.read_limited(Response(b"x", {"Content-Length": "99"}), 4)


def test_security_path_shape_and_bounded_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ALLOWED_ROOTS", str(tmp_path))
    file_path = tmp_path / "file.txt"
    file_path.write_text("ok", encoding="utf-8")
    assert security.resolve_allowed_path(file_path, purpose="test", directory=False) == file_path
    with pytest.raises(ValueError, match="not a directory"):
        security.resolve_allowed_path(file_path, purpose="test", directory=True)
    with pytest.raises(ValueError, match="does not exist"):
        security.resolve_allowed_path(tmp_path / "missing", purpose="test")
    monkeypatch.setenv("LIMIT", "invalid")
    assert security.bounded_int_env("LIMIT", 5, minimum=1, maximum=10) == 5
    monkeypatch.setenv("LIMIT", "999")
    assert security.bounded_int_env("LIMIT", 5, minimum=1, maximum=10) == 10


def test_explicit_workspace_root_allows_visible_tool_path_but_not_escape(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "README.md"
    source.write_text("ok", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("no", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ALLOWED_ROOTS", "")

    assert security.resolve_allowed_path(
        source,
        purpose="source",
        directory=False,
        explicit_root=workspace,
    ) == source
    with pytest.raises(ValueError, match="outside allowed roots"):
        security.resolve_allowed_path(
            outside,
            purpose="source",
            directory=False,
            explicit_root=workspace,
        )


def test_explicit_workspace_root_rejects_broad_and_sensitive_roots(tmp_path):
    with pytest.raises(ValueError, match="too broad"):
        security.explicit_workspace_root(Path(Path.cwd().anchor))
    with pytest.raises(ValueError, match="too broad"):
        security.explicit_workspace_root(Path.home())
    sensitive = tmp_path / ".ssh"
    sensitive.mkdir()
    with pytest.raises(ValueError, match="sensitive"):
        security.explicit_workspace_root(sensitive)


def test_master_consent_enables_agy_backends(monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_USER_CONSENT", "1")
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_ENABLE_CLI_BRIDGE", raising=False)
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_ENABLE_AGY_SESSION", raising=False)
    assert security.cli_bridge_enabled() is True
    assert security.agy_session_enabled() is True
    status = security.consent_status()
    assert status["user_consent"] is True
    assert status["consent_source"] == "GOOGLE_ANTIGRAVITY_USER_CONSENT"


def test_models_prefers_agy_provider_then_cli_then_static(monkeypatch):
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_PROVIDER", "agy-oauth")
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_ENABLE_AGY_SESSION", "1")
    with patch.object(
        models.provider,
        "list_models",
        return_value=[
            {"id": "text-model", "display": "Text", "methods": ["generateContent"]},
            {"id": "image-model", "display": "Image", "methods": ["generateContent"]},
        ],
    ), patch.object(
        models.provider,
        "status",
        return_value={"provider": "agy-oauth", "backend": "agy-oauth-code-assist"},
    ):
        provider_result = models.list_models()
    assert provider_result["source"] == "agy-oauth"
    assert [item["id"] for item in provider_result["text_models"]] == ["text-model"]
    assert [item["id"] for item in provider_result["image_models"]] == ["image-model"]

    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_PROVIDER", raising=False)
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_ENABLE_AGY_SESSION", raising=False)
    with patch.object(models.provider, "configured", return_value=False):
        fallback = models.list_models()
    assert fallback["source"] == "static_fallback"
    assert "model_list_static_fallback" in fallback["warnings"]


def test_models_static_fallback_when_not_configured(monkeypatch):
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_PROVIDER", raising=False)
    with patch.object(models.provider, "configured", return_value=False):
        result = models.list_models()
    assert result["source"] == "static_fallback"
    assert result["text_models"]
