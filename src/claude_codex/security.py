"""Compatibility shim — consent gate implementation in agent_hub.core.consent."""
from agent_hub.core.consent import ConsentGate, CONSENT_FILE_VERSION, TRUE_VALUES, env_flag
from . import paths

_gate = ConsentGate(
    env_prefix="CLAUDE_CODEX",
    grant_script="scripts/claude_codex_consent.py",
    config_dir=paths.config_dir,
)

consent_file_path = _gate.consent_file_path
user_consent_enabled = _gate.user_consent_enabled
require_consent = _gate.require_consent
consent_status = _gate.consent_status
grant_consent = _gate.grant_consent
revoke_consent = _gate.revoke_consent

__all__ = [
    "CONSENT_FILE_VERSION", "TRUE_VALUES", "env_flag", "consent_file_path",
    "user_consent_enabled", "require_consent", "consent_status",
    "grant_consent", "revoke_consent",
]
