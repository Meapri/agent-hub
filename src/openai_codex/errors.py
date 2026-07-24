"""Typed, secret-safe failures for the Codex-backed GPT provider."""

from __future__ import annotations


class CodexProviderError(RuntimeError):
    code = "codex_provider_error"


class CodexUnavailable(CodexProviderError):
    code = "codex_unavailable"


class CodexTimeout(CodexProviderError):
    code = "codex_timeout"


class CodexProtocolError(CodexProviderError):
    code = "codex_protocol_error"


class CodexProcessError(CodexProviderError):
    code = "codex_process_error"


class CodexAuthenticationRequired(CodexProviderError):
    code = "codex_authentication_required"


class CodexSubscriptionRequired(CodexProviderError):
    code = "codex_subscription_login_required"


class CodexSideEffectRefused(CodexProviderError):
    code = "codex_side_effect_refused"
