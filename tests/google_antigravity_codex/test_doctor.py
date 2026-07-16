from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_doctor():
    path = ROOT / "scripts" / "google_antigravity_doctor.py"
    spec = importlib.util.spec_from_file_location("google_antigravity_doctor_script", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_doctor_fails_when_provider_probe_is_unhealthy(tmp_path, monkeypatch, capsys):
    doctor = load_doctor()
    monkeypatch.setattr(
        doctor.provider,
        "status",
        lambda probe=False: {
            "configured": False,
            "healthy": False,
            "error_type": "provider_not_configured",
            "error": "no oauth",
        },
    )
    monkeypatch.setattr(
        doctor.oauth_login,
        "login_status",
        lambda: {"success": False, "text": "not ready"},
    )
    monkeypatch.setattr(doctor.agy_auth, "status", lambda probe=False: {"enabled": True})
    monkeypatch.setattr(
        doctor.security,
        "consent_status",
        lambda: {"user_consent": True},
    )
    monkeypatch.setattr("sys.argv", ["doctor"])
    code = doctor.main()
    assert code == 1
    out = capsys.readouterr().out
    assert "not ready" in out


def test_doctor_succeeds_for_ready_provider_without_live_probe(tmp_path, monkeypatch, capsys):
    doctor = load_doctor()
    monkeypatch.setattr(
        doctor.provider,
        "status",
        lambda probe=False: {
            "configured": True,
            "healthy": True,
            "provider": "agy-oauth",
            "backend": "agy-oauth-code-assist",
        },
    )
    monkeypatch.setattr(
        doctor.oauth_login,
        "login_status",
        lambda: {"success": True, "text": "ready"},
    )
    monkeypatch.setattr(doctor.agy_auth, "status", lambda probe=False: {"enabled": True})
    monkeypatch.setattr(doctor.security, "consent_status", lambda: {"user_consent": True})
    monkeypatch.setattr("sys.argv", ["doctor"])
    code = doctor.main()
    assert code == 0
    assert "provider ready" in capsys.readouterr().out


def test_doctor_live_requires_success(monkeypatch, capsys):
    doctor = load_doctor()
    monkeypatch.setattr(
        doctor.provider,
        "status",
        lambda probe=False: {"configured": True, "healthy": True},
    )
    monkeypatch.setattr(doctor.oauth_login, "login_status", lambda: {"success": True})
    monkeypatch.setattr(doctor.agy_auth, "status", lambda probe=False: {})
    monkeypatch.setattr(doctor.security, "consent_status", lambda: {})
    monkeypatch.setattr(
        doctor,
        "live_probe",
        lambda: {"requested": True, "success": False, "error": "boom"},
    )
    monkeypatch.setattr("sys.argv", ["doctor", "--live"])
    code = doctor.main()
    assert code == 1
