from __future__ import annotations

import pytest

from scripts import google_antigravity_login


def test_status_command_reports_presence_without_credential_path(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        google_antigravity_login,
        "login_status",
        lambda: {
            "text": "ready",
            "token_file_present": True,
            "credentials_readable": True,
            "success": True,
        },
    )

    assert google_antigravity_login.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "stored_locally=True readable=True" in output
    assert "token_file=" not in output


@pytest.mark.parametrize("command", ["complete", "interactive"])
def test_login_commands_report_presence_without_credential_path(
    monkeypatch,
    capsys,
    command,
):
    result = {
        "text": "login complete",
        "token_file_present": True,
    }
    monkeypatch.setattr(
        google_antigravity_login,
        "complete_login",
        lambda _value: result,
    )
    monkeypatch.setattr(
        google_antigravity_login,
        "run_interactive_login",
        lambda **_kwargs: result,
    )
    argv = ["complete", "callback-code"] if command == "complete" else ["interactive"]

    assert google_antigravity_login.main(argv) == 0
    output = capsys.readouterr().out
    assert "stored_locally=True" in output
    assert "token_file=" not in output
