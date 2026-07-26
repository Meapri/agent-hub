from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from google_antigravity_codex import consent_cli, security


def test_local_consent_file_grant_and_revoke(tmp_path, monkeypatch):
    path = tmp_path / "consent.json"
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONSENT_FILE", str(path))
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_USER_CONSENT", raising=False)
    assert security.user_consent_enabled() is False
    assert consent_cli.grant() == path
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["accepted"] is True
    assert payload["version"] == security.CONSENT_FILE_VERSION
    if os.name == "posix":
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert security.agy_session_enabled() is True
    assert security.consent_status()["consent_source"] == "user-consent.json"
    before = path.read_bytes()
    revision = security.consent_revision()
    assert consent_cli.grant() == path
    assert path.read_bytes() == before
    assert security.consent_revision() == revision

    assert consent_cli.revoke() == path
    assert path.exists() is False
    assert security.user_consent_enabled() is False


@pytest.mark.parametrize("invalid_version", ["oops", "1", True, 1.9])
def test_invalid_consent_version_is_disabled_and_regrant_repairs(
    tmp_path,
    monkeypatch,
    invalid_version,
):
    path = tmp_path / "consent.json"
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONSENT_FILE", str(path))
    monkeypatch.delenv("GOOGLE_ANTIGRAVITY_USER_CONSENT", raising=False)
    path.write_text(
        json.dumps({"accepted": True, "version": invalid_version}),
        encoding="utf-8",
    )

    assert security.user_consent_enabled() is False

    consent_cli.grant()

    assert security.user_consent_enabled() is True
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS filesystem mode hardening")
def test_macos_consent_directory_is_private(tmp_path, monkeypatch):
    path = tmp_path / "private-consent" / "consent.json"
    monkeypatch.setenv("GOOGLE_ANTIGRAVITY_CONSENT_FILE", str(path))

    consent_cli.grant()

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".consent.json.*.tmp"))
