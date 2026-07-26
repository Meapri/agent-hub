"""Local, user-driven provider connection management for the setup GUI.

This GUI service deliberately stays outside the public MCP surface. Mutations
through this service must originate from a visible local user action and return
only redacted state.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import http.server
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Collection, Dict, Mapping
import unicodedata
import urllib.parse

from claude_codex import security as claude_security
from claude_codex import models as claude_models
from claude_codex import subscription_auth as claude_subscription
from google_antigravity_codex import agy_auth as google_auth
from google_antigravity_codex import account as google_account
from google_antigravity_codex import consent_cli as google_consent
from google_antigravity_codex import models as google_models
from google_antigravity_codex import oauth_login as google_oauth
from grok_codex import models as grok_models
from grok_codex import oauth_login as grok_oauth
from grok_codex import security as grok_security
from openai_codex import models as openai_models
from openai_codex import security as openai_security

from .v2.contracts import TASK_SCHEMA
from .v2.daemon import HubDaemonClient
from .v2.errors import HubV2Error
from .v2 import provider_runtime

from .provider_registry import AVAILABLE_PROVIDERS

PROVIDER_LABELS = {
    "claude": "Claude",
    "grok": "Grok",
    "gemini": "Gemini",
    "gpt": "GPT",
}

PROVIDER_SESSION_LABELS = {
    "claude": "Claude Code 구독 세션",
    "grok": "xAI 구독 세션",
    "gemini": "Google Antigravity 세션",
    "gpt": "공식 Codex ChatGPT 세션",
}

PROVIDER_LOGIN_OWNERS = {
    "claude": "Claude Code",
    "grok": "Agent Hub",
    "gemini": "Agent Hub",
    "gpt": "공식 Codex",
}

_CONSENT_MODULES = {
    "claude": claude_security,
    "grok": grok_security,
    "gpt": openai_security,
}

MAX_JOBS = 32
JOB_TTL_SECONDS = 30 * 60
MAX_MODELS = 150
MAX_MODEL_TEXT_CHARS = 180
MODEL_CATALOG_TTL_SECONDS = 10 * 60
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
GEMINI_CONNECTION_TEST_MAX_TOKENS = 512
GEMINI_CONNECTION_TEST_TIMEOUT_SECONDS = 30
GEMINI_CONNECTION_TEST_PROMPT = (
    "This is a connection check. Reply with exactly AGENT_HUB_CONNECTION_OK."
)
ACTIVE_JOB_STATES = frozenset({"pending", "working", "waiting"})
PUBLIC_WARNING_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:- "
)


class ConnectionError(RuntimeError):
    """A provider connection action could not be completed safely."""

    def __init__(self, message: str, *, code: str = "connection_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ConnectionJob:
    id: str
    provider: str
    kind: str
    state: str = "pending"
    message: str = ""
    action_url: str | None = None
    user_code: str | None = None
    requires_code: bool = False
    fallback_command: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def public(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("created_at", None)
        data.pop("updated_at", None)
        return data


def _provider(value: str) -> str:
    provider = str(value or "").strip().lower()
    if provider not in AVAILABLE_PROVIDERS:
        raise ConnectionError(
            f"지원하지 않는 제공자입니다: {provider or '(비어 있음)'}",
            code="provider_invalid",
        )
    return provider


def _public_warning(value: Any) -> str:
    warning = str(value or "").strip()
    if 0 < len(warning) <= 80 and all(char in PUBLIC_WARNING_CHARS for char in warning):
        return warning
    return "provider_warning"


def _public_model_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > MAX_MODEL_TEXT_CHARS:
        return ""
    if any(unicodedata.category(char).startswith("C") for char in text):
        return ""
    return text


def _public_model_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if MODEL_ID_PATTERN.fullmatch(text) else ""


def _local_model_payload(provider: str) -> Dict[str, Any]:
    if provider == "claude":
        models = list(claude_models.CURATED)
        source = "curated"
    elif provider == "grok":
        models = [
            item
            for item in grok_models.CURATED
            if "imagine-image" not in str(item.get("id") or "")
            and "imagine-video" not in str(item.get("id") or "")
        ]
        source = "curated"
    elif provider == "gemini":
        models = google_models.static_model_catalog()
        source = "static_fallback"
    else:
        models = [
            {
                "id": openai_models.DEFAULT_MODEL,
                "display": openai_models.DEFAULT_MODEL,
                "source": "built_in",
            }
        ]
        source = "built_in"
    return {
        "success": True,
        "source": source,
        "text_models": models,
    }


def _text_model_items(payload: Dict[str, Any]) -> list[Any]:
    items = payload.get("text_models")
    if not isinstance(items, list):
        items = payload.get("models")
    return items if isinstance(items, list) else []


def _live_model_payload(provider: str, payload: Any) -> bool:
    if not isinstance(payload, dict) or payload.get("success") is False:
        return False
    if not any(
        isinstance(item, dict) and _public_model_id(item.get("id") or item.get("model"))
        for item in _text_model_items(payload)
    ):
        return False
    source = _public_model_text(payload.get("source"))
    warnings = [str(item) for item in payload.get("warnings") or []]
    if any(
        warning.startswith("live_list_failed") or warning == "provider_model_list_empty"
        for warning in warnings
    ):
        return False
    if provider in {"claude", "grok"}:
        return source == "live"
    if provider == "gpt":
        return source == "codex-app-server"
    return source not in {"", "static", "static_fallback", "curated", "built_in"}


def _model_catalog_revision(provider: str, model_ids: Collection[str]) -> str:
    material = "\0".join([provider, *sorted(model_ids)]).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:20]


def _disambiguate_model_displays(models: list[Dict[str, Any]]) -> None:
    counts: Dict[str, int] = {}
    for item in models:
        display = str(item["display"])
        counts[display] = counts.get(display, 0) + 1
    for item in models:
        display = str(item["display"])
        if counts[display] < 2:
            continue
        detailed = f"{display} ({item['id']})"
        item["display"] = detailed if len(detailed) <= MAX_MODEL_TEXT_CHARS else str(item["id"])


def _login_url(value: Any, *, hosts: Collection[str]) -> str:
    url = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ConnectionError(
            "제공자의 안전한 로그인 주소를 확인하지 못했습니다.",
            code="login_url_invalid",
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or port not in {None, 443}
        or parsed.username
        or parsed.password
    ):
        raise ConnectionError(
            "제공자의 안전한 로그인 주소를 확인하지 못했습니다.",
            code="login_url_invalid",
        )
    return url


def _status_reader(provider: str = "all", *, probe: bool = False) -> Dict[str, Any]:
    try:
        response = HubDaemonClient().request(
            "tools/call",
            {
                "name": "agent_hub_status",
                "arguments": {"probe": probe},
            },
            timeout=15.0,
        )
        data = response.get("data") if isinstance(response, dict) else None
        provider_rows = data.get("providers") if isinstance(data, dict) else None
        if isinstance(provider_rows, dict):
            selected = (
                provider_rows if provider == "all" else {provider: provider_rows.get(provider, {})}
            )
            states = {
                provider_id: dict(row.get("state") or {})
                for provider_id, row in selected.items()
                if isinstance(row, dict)
            }
            if states:
                return {"providers": states, "runtime": "agent-hubd"}
    except HubV2Error:
        pass
    selected = AVAILABLE_PROVIDERS if provider == "all" else (provider,)
    states: Dict[str, Any] = {}
    for provider_id in selected:
        result = provider_runtime.status(provider_id, probe=probe)
        data = result.get("data") if isinstance(result, dict) else None
        provider_rows = data.get("providers") if isinstance(data, dict) else None
        if isinstance(provider_rows, dict) and isinstance(provider_rows.get(provider_id), dict):
            states[provider_id] = dict(provider_rows[provider_id])
    if not states:
        raise ConnectionError("제공자 상태를 읽지 못했습니다.", code="status_unavailable")
    return {"providers": states, "runtime": "local-status-fallback"}


def _daemon_tool(name: str, arguments: Mapping[str, Any]) -> Dict[str, Any] | None:
    try:
        result = HubDaemonClient().request(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
            timeout=1810.0,
        )
    except HubV2Error:
        return None
    return result if isinstance(result, dict) else None


class ConnectionManager:
    """Coordinate visible GUI actions without exposing credential material."""

    def __init__(
        self,
        *,
        status_reader: Callable[..., Dict[str, Any]] = _status_reader,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._status_reader = status_reader
        self._use_daemon = status_reader is _status_reader
        self._command_runner = command_runner
        self._jobs: Dict[str, ConnectionJob] = {}
        self._lock = threading.RLock()
        self._starting_logins: set[str] = set()
        self._starting_refreshes: set[str] = set()
        self._starting_tests: set[str] = set()
        self._cancel_events: Dict[str, threading.Event] = {}
        self._callback_servers: Dict[str, http.server.HTTPServer] = {}
        self._owned_pending_flows: Dict[str, str] = {}
        self._job_flows: Dict[str, str] = {}
        self._external_processes: Dict[str, subprocess.Popen[str]] = {}
        self._model_catalogs: Dict[str, Dict[str, Any]] = {}
        self._auth_generations: Dict[str, int] = {provider: 0 for provider in AVAILABLE_PROVIDERS}
        self._closed = False

    def _daemon_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> Dict[str, Any] | None:
        return _daemon_tool(name, arguments) if self._use_daemon else None

    def status(self, provider: str = "all", *, probe: bool = False) -> Dict[str, Any]:
        selected = "all" if provider == "all" else _provider(provider)
        raw = self._status_reader(selected, probe=probe)
        public: Dict[str, Dict[str, Any]] = {}
        for provider_id, state in raw["providers"].items():
            auth_mode = state.get("auth_mode")
            auth_ready = bool(state.get("auth_ready", state.get("authenticated")))
            logged_in = bool(
                state.get(
                    "logged_in",
                    state.get("configured", state.get("authenticated")),
                )
                or auth_ready
            )
            account_present = bool(
                state.get(
                    "account_present",
                    state.get("configured", state.get("authenticated")),
                )
                or logged_in
            )
            refreshable = bool(state.get("refreshable") and logged_in and not auth_ready)
            relogin_required = bool(
                not auth_ready
                and not refreshable
                and (state.get("relogin_required") or account_present)
            )
            session_label = PROVIDER_SESSION_LABELS[provider_id]
            if provider_id == "claude" and auth_mode == "api_key":
                session_label = "Claude API key"
            elif provider_id == "grok" and auth_mode == "api_key":
                session_label = "xAI API key"
            elif provider_id == "gpt" and auth_mode in {"api_key", "apiKey"}:
                session_label = "Codex API key"
            safe = {
                "id": provider_id,
                "label": PROVIDER_LABELS[provider_id],
                "session_label": session_label,
                "login_owner": PROVIDER_LOGIN_OWNERS[provider_id],
                "consent": bool(state.get("consent")),
                "configured": bool(state.get("configured", state.get("authenticated"))),
                "authenticated": auth_ready,
                "logged_in": logged_in,
                "auth_ready": auth_ready,
                "account_present": account_present,
                "login_ready": auth_ready,
                "refresh_supported": bool(state.get("refresh_supported") or refreshable),
                "refreshable": refreshable,
                "relogin_required": relogin_required,
                "ready": bool(state.get("ready")),
                "invocation_ready": bool(
                    state.get("invocation_ready", state.get("ready"))
                ),
                "auto_refresh_on_invoke": bool(state.get("auto_refresh_on_invoke")),
                "auth_mode": auth_mode,
                "plan_type": state.get("plan_type"),
                "default_model": _public_model_id(state.get("default_model")) or "알 수 없음",
                "base_default_model": _public_model_id(state.get("base_default_model")) or None,
                "model_overridden": bool(state.get("model_overridden")),
                "model_managed_by_environment": bool(state.get("model_managed_by_environment")),
                "model_source": _public_model_text(state.get("model_source")),
                "model_override_scope": _public_model_text(state.get("model_override_scope"))
                or None,
                "settings_error": (
                    _public_warning(state.get("settings_error"))
                    if state.get("settings_error")
                    else None
                ),
                "warnings": [_public_warning(item) for item in state.get("warnings") or []],
                "supports_local_logout": (provider_id in {"grok", "gemini"}),
                "local_credentials_present": bool(state.get("local_credentials_present")),
                "pending_login_present": bool(state.get("pending_login_present")),
                "login_transport": (
                    "browser" if provider_id in {"grok", "gemini"} else "external_cli"
                ),
                "capabilities": self._capability_labels(state.get("capabilities")),
            }
            safe["connection_state"] = self._connection_state(safe)
            public[provider_id] = safe
        return {
            "success": True,
            "providers": public,
            "summary": {
                "ready": sum(item["ready"] for item in public.values()),
                "authenticated": sum(item["authenticated"] for item in public.values()),
                "connected": sum(item["logged_in"] for item in public.values()),
                "refreshable": sum(item["refreshable"] for item in public.values()),
                "relogin_required": sum(item["relogin_required"] for item in public.values()),
                "consent_required": sum(
                    item["logged_in"] and not item["consent"] for item in public.values()
                ),
                "total": len(public),
            },
        }

    @staticmethod
    def _connection_state(state: Dict[str, Any]) -> str:
        if state["ready"]:
            return "ready"
        if state["refreshable"]:
            return "refreshable"
        if state["relogin_required"] or (state["account_present"] and not state["logged_in"]):
            return "relogin_required"
        if state["logged_in"] or state["auth_ready"]:
            return "signed_in"
        return "signed_out"

    @staticmethod
    def _capability_labels(capabilities: Any) -> list[str]:
        if not isinstance(capabilities, dict):
            return []
        labels = {
            "chat": "대화",
            "compare": "비교",
            "review_diff": "코드 검토",
            "write": "문서 작성",
            "search": "검색",
            "vision": "이미지 입력",
            "image_generation": "이미지 생성",
        }
        return [
            labels[key] for key in labels if bool((capabilities.get(key) or {}).get("supported"))
        ]

    def grant_consent(self, provider: str, *, confirmation: str) -> Dict[str, Any]:
        provider = _provider(provider)
        if confirmation != f"connect:{provider}":
            raise ConnectionError(
                "화면에서 동의 내용을 확인해야 합니다.",
                code="consent_confirmation_required",
            )
        with self._lock:
            self._ensure_open()
            self._ensure_provider_idle(
                provider,
                "진행 중인 로그인이 끝난 뒤 사용 동의를 변경해 주세요.",
            )
            if provider == "gemini":
                path = google_consent.grant()
            else:
                path = _CONSENT_MODULES[provider].grant_consent()
            self._invalidate_model_catalog(provider)
        return {
            "success": True,
            "provider": provider,
            "consent": True,
            "stored_locally": True,
            "path_created": bool(path),
        }

    def revoke_consent(self, provider: str, *, confirmation: str) -> Dict[str, Any]:
        provider = _provider(provider)
        if confirmation != f"disconnect:{provider}":
            raise ConnectionError(
                "연결 해제를 다시 확인해 주세요.",
                code="disconnect_confirmation_required",
            )
        with self._lock:
            self._ensure_open()
            self._ensure_provider_idle(
                provider,
                "진행 중인 로그인이나 연결 확인이 끝난 뒤 연결을 해제해 주세요.",
            )
            if provider == "gemini":
                path = google_consent.revoke()
                removed = not path.exists()
            else:
                removed = bool(_CONSENT_MODULES[provider].revoke_consent())
            self._invalidate_model_catalog(provider)
            effective = self.status(provider)["providers"][provider]["consent"]
        return {
            "success": True,
            "provider": provider,
            "consent": effective,
            "removed": removed,
            "shared_login_preserved": provider in {"claude", "gpt"},
            "managed_by_environment": effective,
        }

    def remove_local_credentials(self, provider: str, *, confirmation: str) -> Dict[str, Any]:
        provider = _provider(provider)
        if confirmation != f"forget-local:{provider}":
            raise ConnectionError(
                "로컬 로그인 정보 삭제를 다시 확인해 주세요.",
                code="credential_removal_confirmation_required",
            )
        with self._lock:
            self._ensure_open()
            self._ensure_provider_idle(
                provider,
                "진행 중인 로그인이 끝난 뒤 로컬 로그인 정보를 삭제해 주세요.",
            )
            if provider == "grok":
                try:
                    removed = grok_oauth.clear_tokens()
                except OSError as exc:
                    raise ConnectionError(
                        "일부 xAI 로컬 로그인 정보를 삭제하지 못했습니다. "
                        "파일 권한을 확인하고 다시 시도해 주세요.",
                        code="credential_removal_failed",
                    ) from exc
                finally:
                    self._invalidate_model_catalog(provider)
                return {
                    "success": True,
                    "provider": provider,
                    "removed": bool(removed),
                    "text": "Agent Hub가 저장한 xAI 로그인 정보를 삭제했습니다.",
                }
            if provider == "gemini":
                result = google_account.logout({"forget_client": False})
                self._invalidate_model_catalog(provider)
                if not result.get("success"):
                    raise ConnectionError(
                        "일부 Google 로컬 로그인 정보를 삭제하지 못했습니다. "
                        "파일 권한을 확인하고 다시 시도해 주세요.",
                        code="credential_removal_failed",
                    )
                return {
                    "success": True,
                    "provider": provider,
                    "removed": bool(result.get("removed")),
                    "removed_count": int(result.get("removed_count") or 0),
                    "text": (
                        "Agent Hub가 저장한 Google 로그인 정보를 삭제했습니다."
                        if result.get("removed")
                        else "삭제할 Agent Hub Google 로그인 정보가 없습니다."
                    ),
                }
            owner = PROVIDER_LOGIN_OWNERS[provider]
            raise ConnectionError(
                f"{PROVIDER_LABELS[provider]} 로그인은 {owner}에서 공동으로 사용하므로 "
                "Agent Hub가 로그아웃하지 않습니다.",
                code="shared_login_preserved",
            )

    def start_login(self, provider: str) -> Dict[str, Any]:
        provider = _provider(provider)
        with self._lock:
            self._ensure_open()
            current = self.status(provider)["providers"][provider]
            if not current["consent"]:
                raise ConnectionError(
                    "먼저 Agent Hub 사용 동의를 완료해 주세요.",
                    code="consent_required",
                )
            active = self._active_job(provider)
            if active is not None:
                if active.kind == "login":
                    return active.public()
                raise ConnectionError(
                    "이 제공자의 연결 확인이 진행 중입니다. 완료된 뒤 다시 로그인해 주세요.",
                    code="provider_busy",
                )
            if provider in self._starting_logins:
                raise ConnectionError(
                    "로그인 시작 요청을 처리하고 있습니다.",
                    code="login_in_progress",
                )
            if provider in self._starting_tests:
                raise ConnectionError(
                    "이 제공자의 연결 확인이 시작되고 있습니다. 완료된 뒤 다시 로그인해 주세요.",
                    code="provider_busy",
                )
            if provider in self._starting_refreshes:
                raise ConnectionError(
                    "이 제공자의 로그인 갱신이 시작되고 있습니다. 완료된 뒤 다시 로그인해 주세요.",
                    code="provider_busy",
                )
            self._starting_logins.add(provider)
            self._invalidate_model_catalog(provider)
        try:
            if provider == "grok":
                return self._start_grok_login()
            if provider == "gemini":
                return self._start_gemini_login()
            command = (
                ["claude", "auth", "login", "--claudeai"]
                if provider == "claude"
                else ["codex", "login"]
            )
            return self._start_external_login(provider, command)
        finally:
            with self._lock:
                self._starting_logins.discard(provider)

    def start_refresh(self, provider: str) -> Dict[str, Any]:
        provider = _provider(provider)
        with self._lock:
            self._ensure_open()
            active = self._active_job(provider)
            if active is not None:
                if active.kind == "refresh":
                    return active.public()
                raise ConnectionError(
                    "이 제공자의 다른 연결 작업이 진행 중입니다. 완료된 뒤 갱신해 주세요.",
                    code="provider_busy",
                )
            current = self.status(provider)["providers"][provider]
            if not current["consent"]:
                raise ConnectionError(
                    "먼저 Agent Hub 사용 동의를 완료해 주세요.",
                    code="consent_required",
                )
            if not current["refreshable"]:
                raise ConnectionError(
                    "현재 로그인은 바로 갱신할 수 없습니다. 다시 로그인해 주세요.",
                    code="refresh_unavailable",
                )
            if provider in self._starting_refreshes:
                raise ConnectionError(
                    "로그인 갱신 요청을 처리하고 있습니다.",
                    code="refresh_in_progress",
                )
            if provider in self._starting_logins or provider in self._starting_tests:
                raise ConnectionError(
                    "이 제공자의 다른 연결 작업이 시작되고 있습니다. 완료된 뒤 갱신해 주세요.",
                    code="provider_busy",
                )
            self._starting_refreshes.add(provider)
            self._invalidate_model_catalog(provider)
            try:
                job = self._create_job(
                    provider,
                    kind="refresh",
                    state="working",
                    message=f"{PROVIDER_LABELS[provider]} 로그인 정보를 갱신하는 중입니다.",
                )
                cancel_event = threading.Event()
                self._cancel_events[job.id] = cancel_event
                threading.Thread(
                    target=self._run_refresh,
                    args=(job.id, cancel_event),
                    daemon=True,
                ).start()
                return job.public()
            finally:
                self._starting_refreshes.discard(provider)

    def complete_login(self, provider: str, job_id: str, code_or_url: str) -> Dict[str, Any]:
        provider = _provider(provider)
        value = str(code_or_url or "").strip()
        if not value:
            raise ConnectionError(
                "리디렉션 URL이나 인증 코드를 입력해 주세요.", code="code_required"
            )
        with self._lock:
            self._ensure_open()
            job = self._jobs.get(str(job_id or ""))
            if (
                job is None
                or job.provider != provider
                or job.kind != "login"
                or not job.requires_code
                or job.state != "waiting"
            ):
                raise ConnectionError(
                    "완료할 로그인 작업이 아닙니다.",
                    code="login_job_invalid",
                )
            flow_id = self._job_flows.get(job.id)
            if not flow_id:
                raise ConnectionError(
                    "로그인 흐름 소유권을 확인하지 못했습니다.",
                    code="oauth_flow_id_missing",
                )
            cancel_event = self._cancel_events.get(job.id)
            if cancel_event is None:
                raise ConnectionError(
                    "로그인 취소 상태를 확인하지 못했습니다.",
                    code="oauth_cancel_state_missing",
                )
            job.state = "working"
            job.message = "Google 로그인을 완료하는 중입니다."
            job.updated_at = time.time()
            threading.Thread(
                target=self._complete_gemini_code,
                args=(job_id, value, flow_id, cancel_event),
                daemon=True,
            ).start()
        return self._job(job_id).public()

    def start_test(self, provider: str) -> Dict[str, Any]:
        provider = _provider(provider)
        with self._lock:
            self._ensure_open()
            state = self.status(provider)["providers"][provider]
            if not state["consent"] or not (
                state["login_ready"] or state["invocation_ready"]
            ):
                raise ConnectionError(
                    "동의와 로그인을 모두 완료한 뒤 연결을 테스트할 수 있습니다.",
                    code="provider_not_ready",
                )
            selected_model = _public_model_id(state.get("default_model"))
            if provider == "gemini" and not selected_model:
                raise ConnectionError(
                    "테스트할 Gemini 모델을 확인하지 못했습니다.",
                    code="model_unavailable",
                )
            auth_generation = self._auth_generations[provider]
            active = self._active_job(provider)
            if active is not None:
                if active.kind == "test":
                    return active.public()
                raise ConnectionError(
                    "이 제공자의 로그인이 진행 중입니다. 완료된 뒤 연결을 확인해 주세요.",
                    code="provider_busy",
                )
            if provider in self._starting_tests:
                raise ConnectionError(
                    "연결 확인 요청을 처리하고 있습니다.",
                    code="test_in_progress",
                )
            if provider in self._starting_logins:
                raise ConnectionError(
                    "이 제공자의 로그인이 시작되고 있습니다. 완료된 뒤 연결을 확인해 주세요.",
                    code="provider_busy",
                )
            if provider in self._starting_refreshes:
                raise ConnectionError(
                    "이 제공자의 로그인 갱신이 시작되고 있습니다. 완료된 뒤 연결을 확인해 주세요.",
                    code="provider_busy",
                )
            self._starting_tests.add(provider)
        try:
            with self._lock:
                job = self._create_job(
                    provider,
                    kind="test",
                    state="working",
                    message=(
                        "선택한 Gemini 모델에 짧은 실제 요청을 보내는 중입니다."
                        if provider == "gemini"
                        else "모델 목록을 요청해 연결을 확인하는 중입니다."
                    ),
                )
                threading.Thread(
                    target=self._run_test,
                    args=(job.id, selected_model, auth_generation),
                    daemon=True,
                ).start()
            return job.public()
        finally:
            with self._lock:
                self._starting_tests.discard(provider)

    def models(self, provider: str, *, refresh: bool = False) -> Dict[str, Any]:
        provider = _provider(provider)
        with self._lock:
            self._ensure_open()
            auth_generation = self._auth_generations[provider]
        state = self.status(provider)["providers"][provider]
        payload = _local_model_payload(provider)
        live_eligible = bool(state["ready"] or state["invocation_ready"])
        live_unavailable = bool(refresh and not live_eligible)
        refreshed = False
        if refresh and live_eligible:
            daemon_result = self._daemon_call(
                "agent_hub_catalog",
                {"provider": provider, "refresh": True},
            )
            daemon_data = daemon_result.get("data") if isinstance(daemon_result, dict) else None
            daemon_entry = (
                (daemon_data.get("providers") or {}).get(provider)
                if isinstance(daemon_data, dict) and isinstance(daemon_data.get("providers"), dict)
                else None
            )
            worker_result = daemon_entry.get("result") if isinstance(daemon_entry, dict) else None
            worker_data = worker_result.get("data") if isinstance(worker_result, dict) else None
            live_payload = (
                (worker_data.get("models") or {}).get(provider)
                if isinstance(worker_data, dict) and isinstance(worker_data.get("models"), dict)
                else None
            )
            if live_payload is None:
                result = provider_runtime.catalog(provider, refresh=True)
                data = result.get("data") if isinstance(result, dict) else None
                live_payload = (
                    (data.get("models") or {}).get(provider)
                    if isinstance(data, dict) and isinstance(data.get("models"), dict)
                    else None
                )
            if _live_model_payload(provider, live_payload):
                payload = live_payload
                refreshed = True
            else:
                live_unavailable = True
        raw_models = _text_model_items(payload)
        models: list[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_models[:MAX_MODELS]:
            if not isinstance(item, dict):
                continue
            model_id = _public_model_id(item.get("id") or item.get("model"))
            if not model_id or model_id in seen:
                continue
            display = _public_model_text(item.get("display") or item.get("displayName") or model_id)
            models.append(
                {
                    "id": model_id,
                    "display": display or model_id,
                    "source": _public_model_text(item.get("source")) or "provider",
                    "selectable": True,
                }
            )
            seen.add(model_id)
        _disambiguate_model_displays(models)
        selected = _public_model_id(state.get("default_model"))
        revision = _model_catalog_revision(provider, seen)
        with self._lock:
            self._ensure_open()
            if auth_generation != self._auth_generations[provider]:
                raise ConnectionError(
                    "연결 상태가 바뀌었습니다. 모델 목록을 다시 불러와 주세요.",
                    code="model_catalog_stale",
                )
            self._model_catalogs[provider] = {
                "revision": revision,
                "ids": frozenset(seen),
                "expires_at": time.monotonic() + MODEL_CATALOG_TTL_SECONDS,
                "auth_generation": auth_generation,
            }
        if selected and selected not in seen:
            models.insert(
                0,
                {
                    "id": selected,
                    "display": selected,
                    "source": "selected_unavailable",
                    "selectable": False,
                },
            )
        if not models:
            raise ConnectionError(
                "선택할 수 있는 텍스트 모델이 없습니다.",
                code="model_catalog_empty",
            )
        return {
            "success": True,
            "provider": provider,
            "models": models,
            "selected_model": selected,
            "catalog_revision": revision,
            "source": _public_model_text(payload.get("source")) or "provider",
            "refreshed": refreshed,
            "refresh_requested": bool(refresh),
            "live_unavailable": live_unavailable,
        }

    def set_default_model(
        self,
        provider: str,
        model: str,
        *,
        catalog_revision: str,
    ) -> Dict[str, Any]:
        provider = _provider(provider)
        self._ensure_open()
        requested = _public_model_id(model)
        if not requested:
            raise ConnectionError(
                "선택할 모델을 확인해 주세요.",
                code="model_invalid",
            )
        with self._lock:
            self._ensure_open()
            self._ensure_provider_idle(
                provider,
                "진행 중인 로그인이 끝난 뒤 기본 모델을 저장해 주세요.",
            )
            catalog = self._model_catalogs.get(provider)
            valid_catalog = bool(
                catalog
                and secrets.compare_digest(
                    str(catalog.get("revision") or ""),
                    str(catalog_revision or ""),
                )
                and float(catalog.get("expires_at") or 0) >= time.monotonic()
                and catalog.get("auth_generation") == self._auth_generations[provider]
            )
            allowed = set(catalog.get("ids") or ()) if valid_catalog else set()
            if not valid_catalog:
                raise ConnectionError(
                    "모델 목록이 변경되었거나 만료되었습니다. 목록을 다시 불러와 주세요.",
                    code="model_catalog_stale",
                )
            if requested not in allowed:
                raise ConnectionError(
                    "현재 제공자 모델 목록에 없는 모델은 저장할 수 없습니다.",
                    code="model_not_available",
                )
            result = provider_runtime.set_default_model(provider, requested)
            if not isinstance(result, dict) or result.get("success") is False:
                raise ConnectionError(
                    "선택한 모델을 저장하지 못했습니다. 다시 시도해 주세요.",
                    code="model_save_failed",
                )
            current = self.status(provider)["providers"][provider]
        return {
            "success": True,
            "provider": provider,
            "selected_model": current["default_model"],
            "model_overridden": current["model_overridden"],
            "model_source": current["model_source"],
        }

    def reset_default_model(
        self,
        provider: str,
        *,
        confirmation: str,
    ) -> Dict[str, Any]:
        provider = _provider(provider)
        if confirmation != f"reset-model:{provider}":
            raise ConnectionError(
                "기본 모델 초기화를 다시 확인해 주세요.",
                code="model_reset_confirmation_required",
            )
        with self._lock:
            self._ensure_open()
            self._ensure_provider_idle(
                provider,
                "진행 중인 로그인이 끝난 뒤 기본 모델을 초기화해 주세요.",
            )
            current = self.status(provider)["providers"][provider]
            if current["model_managed_by_environment"] and not current["model_overridden"]:
                raise ConnectionError(
                    "이 모델은 환경 설정에서 관리되고 있어 웹 화면에서 초기화할 수 없습니다.",
                    code="model_managed_by_environment",
                )
            result = provider_runtime.reset_default_model(
                provider,
                gemini_task=(
                    "chat"
                    if provider == "gemini" and current.get("model_override_scope") == "task:chat"
                    else None
                ),
            )
            if not isinstance(result, dict) or result.get("success") is False:
                raise ConnectionError(
                    "기본 모델 설정을 초기화하지 못했습니다. 다시 시도해 주세요.",
                    code="model_reset_failed",
                )
            updated = self.status(provider)["providers"][provider]
        return {
            "success": True,
            "provider": provider,
            "selected_model": updated["default_model"],
            "model_overridden": updated["model_overridden"],
            "model_source": updated["model_source"],
        }

    def job(self, job_id: str) -> Dict[str, Any]:
        return self._job(job_id).public()

    def close(self) -> None:
        """Stop local helpers without changing completed provider logins."""

        with self._lock:
            self._closed = True
            self._model_catalogs.clear()
            cancel_events = list(self._cancel_events.values())
            callback_servers = list(self._callback_servers.values())
            pending_flows = list(self._owned_pending_flows.items())
            external_processes = list(self._external_processes.values())
            self._external_processes.clear()
            now = time.time()
            for event in cancel_events:
                event.set()
            for job in self._jobs.values():
                if job.state in ACTIVE_JOB_STATES:
                    job.state = "cancelled"
                    job.message = "연결 관리 종료로 작업을 중단했습니다."
                    job.updated_at = now
        for server in callback_servers:
            try:
                server.server_close()
            except OSError:
                pass
        for provider, flow_id in pending_flows:
            self._clear_owned_pending(provider, flow_id)
        for process in external_processes:
            self._terminate_external_process(process)

    def _start_grok_login(self) -> Dict[str, Any]:
        try:
            grok_oauth.clear_unusable_pending_login()
            started = grok_oauth.start_login(open_browser=False)
            flow_id = str(started.get("flow_id") or "")
            if not flow_id:
                raise ConnectionError(
                    "Grok 로그인 흐름 소유권을 확인하지 못했습니다.",
                    code="oauth_flow_id_missing",
                )
            action_url = _login_url(
                started.get("verification_uri_complete") or started.get("verification_uri"),
                hosts={"accounts.x.ai", "auth.x.ai"},
            )
        except ConnectionError:
            self._clear_pending_flow("grok", locals().get("flow_id"))
            raise
        except grok_oauth.LoginInProgressError as exc:
            raise ConnectionError(
                "다른 창에서 Grok 로그인이 진행 중입니다. "
                "기존 로그인을 완료하거나 취소한 뒤 다시 시도해 주세요.",
                code="login_in_progress",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            self._clear_pending_flow("grok", locals().get("flow_id"))
            raise ConnectionError(
                "Grok 로그인을 시작하지 못했습니다. 네트워크 상태를 확인하고 다시 시도해 주세요.",
                code="login_start_failed",
            ) from exc
        cancel_event = threading.Event()
        try:
            with self._lock:
                self._ensure_open()
                job = self._create_job(
                    "grok",
                    kind="login",
                    state="waiting",
                    message="브라우저에서 xAI 로그인을 승인해 주세요.",
                    action_url=action_url,
                    user_code=str(started.get("user_code") or "") or None,
                )
                self._owned_pending_flows["grok"] = flow_id
                self._cancel_events[job.id] = cancel_event
                self._job_flows[job.id] = flow_id
                threading.Thread(
                    target=self._complete_grok,
                    args=(job.id, cancel_event, flow_id),
                    daemon=True,
                ).start()
        except ConnectionError:
            self._clear_pending_flow("grok", flow_id)
            raise
        return job.public()

    def _complete_grok(
        self,
        job_id: str,
        cancel_event: threading.Event,
        flow_id: str,
    ) -> None:
        try:
            grok_oauth.complete_login(
                cancel_event=cancel_event,
                expected_flow_id=flow_id,
                commit_guard=lambda: self._lock,
            )
        except Exception:  # noqa: BLE001
            if not cancel_event.is_set():
                self._update_job(
                    job_id,
                    state="failed",
                    message="Grok 로그인을 완료하지 못했습니다. 다시 시도해 주세요.",
                )
            self._clear_owned_pending("grok", flow_id)
            return
        finally:
            with self._lock:
                self._cancel_events.pop(job_id, None)
                self._job_flows.pop(job_id, None)
        self._clear_owned_pending("grok", flow_id)
        if not cancel_event.is_set():
            self._update_job(
                job_id,
                state="complete",
                message="Grok 연결이 완료되었습니다.",
            )

    def _start_gemini_login(self) -> Dict[str, Any]:
        callback_server: http.server.HTTPServer | None = None
        try:
            callback_server = http.server.HTTPServer(
                ("127.0.0.1", google_oauth.LOCAL_PORT),
                http.server.BaseHTTPRequestHandler,
            )
        except OSError:
            callback_server = None

        try:
            google_oauth.clear_unusable_pending_login()
            started = google_oauth.start_login(use_local_redirect=callback_server is not None)
            flow_id = str(started.get("flow_id") or "")
            if not flow_id:
                raise ConnectionError(
                    "Gemini 로그인 흐름 소유권을 확인하지 못했습니다.",
                    code="oauth_flow_id_missing",
                )
            action_url = _login_url(
                started.get("auth_url"),
                hosts={"accounts.google.com"},
            )
        except ConnectionError:
            self._clear_pending_flow("gemini", locals().get("flow_id"))
            if callback_server is not None:
                callback_server.server_close()
            raise
        except google_oauth.OAuthLoginError as exc:
            if callback_server is not None:
                callback_server.server_close()
            if exc.code in {"oauth_login_in_progress", "oauth_pending_invalid"}:
                raise ConnectionError(
                    "다른 창에서 Gemini 로그인이 진행 중이거나 이전 로그인 상태를 "
                    "정리해야 합니다. 기존 로그인을 완료하거나 취소한 뒤 다시 시도해 주세요.",
                    code="login_in_progress",
                ) from exc
            raise ConnectionError(
                "Gemini 로그인을 시작하지 못했습니다. 다시 시도해 주세요.",
                code="login_start_failed",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            self._clear_pending_flow("gemini", locals().get("flow_id"))
            if callback_server is not None:
                callback_server.server_close()
            raise ConnectionError(
                "Gemini 로그인을 시작하지 못했습니다. 네트워크 상태를 확인하고 다시 시도해 주세요.",
                code="login_start_failed",
            ) from exc

        cancel_event = threading.Event()
        try:
            with self._lock:
                self._ensure_open()
                job = self._create_job(
                    "gemini",
                    kind="login",
                    state="waiting",
                    message=(
                        "브라우저에서 Google 로그인을 완료해 주세요."
                        if callback_server is not None
                        else "Google 로그인 뒤 리디렉션 URL을 이 화면에 붙여 넣어 주세요."
                    ),
                    action_url=action_url,
                    requires_code=True,
                )
                self._owned_pending_flows["gemini"] = flow_id
                self._cancel_events[job.id] = cancel_event
                self._job_flows[job.id] = flow_id
                if callback_server is None:
                    return job.public()
                self._callback_servers[job.id] = callback_server
                self._install_google_callback(
                    callback_server,
                    job.id,
                    flow_id,
                    cancel_event,
                )
        except ConnectionError:
            self._clear_pending_flow("gemini", flow_id)
            if callback_server is not None:
                callback_server.server_close()
            raise
        return job.public()

    def _install_google_callback(
        self,
        server: http.server.HTTPServer,
        job_id: str,
        flow_id: str,
        cancel_event: threading.Event,
    ) -> None:
        manager = self

        class CallbackHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/auth/callback":
                    body = b"Not found"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                callback_url = f"http://localhost:{google_oauth.LOCAL_PORT}{self.path}"
                try:
                    google_oauth.validate_callback_state(
                        callback_url,
                        require_state=True,
                        expected_flow_id=flow_id,
                    )
                except Exception:  # noqa: BLE001
                    claimed = False
                    title = "유효하지 않은 로그인 요청입니다."
                    response_status = 400
                else:
                    claimed = manager._claim_job(
                        job_id,
                        expected_state="waiting",
                        state="working",
                        message="Google 인증 결과를 확인하는 중입니다.",
                    )
                    response_status = 200
                if not claimed and response_status == 200:
                    title = "이미 로그인 결과를 처리하고 있습니다."
                elif claimed:
                    try:
                        google_oauth.complete_login(
                            callback_url,
                            probe=False,
                            expected_flow_id=flow_id,
                            cancel_event=cancel_event,
                            commit_guard=lambda: manager._lock,
                        )
                    except Exception:  # noqa: BLE001
                        manager._update_job(
                            job_id,
                            state="failed",
                            message=("Gemini 로그인을 완료하지 못했습니다. 다시 시도해 주세요."),
                        )
                        title = "로그인을 완료하지 못했습니다."
                    else:
                        manager._update_job(
                            job_id,
                            state="complete",
                            message="Gemini 연결이 완료되었습니다.",
                        )
                        title = "Agent Hub 연결이 완료되었습니다."
                    manager._clear_owned_pending("gemini", flow_id)
                    with manager._lock:
                        manager._job_flows.pop(job_id, None)
                body = (
                    "<!doctype html><meta charset='utf-8'>"
                    "<meta name='color-scheme' content='light'>"
                    "<title>Agent Hub</title>"
                    "<body style='font:16px -apple-system,BlinkMacSystemFont,sans-serif;"
                    "padding:48px;color:#0b1f3a;background:#f7f9fc'>"
                    f"<h1 style='font-size:24px'>{title}</h1>"
                    "<p>이 탭을 닫고 Agent Hub 연결 관리 화면으로 돌아가세요.</p></body>"
                ).encode("utf-8")
                self.send_response(response_status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        server.RequestHandlerClass = CallbackHandler

        def serve_callback() -> None:
            deadline = time.monotonic() + 300
            try:
                server.timeout = 0.5
                while time.monotonic() < deadline:
                    job = self._job(job_id)
                    if job.state not in ACTIVE_JOB_STATES:
                        break
                    try:
                        server.handle_request()
                    except OSError:
                        break
                job = self._job(job_id)
                if job.state in ACTIVE_JOB_STATES:
                    self._update_job(
                        job_id,
                        state="failed",
                        message="Google 로그인 대기 시간이 만료되었습니다. 다시 시도해 주세요.",
                    )
                    self._clear_owned_pending("gemini", flow_id)
            finally:
                with self._lock:
                    self._callback_servers.pop(job_id, None)
                    self._cancel_events.pop(job_id, None)
                    self._job_flows.pop(job_id, None)
                try:
                    server.server_close()
                except OSError:
                    pass

        threading.Thread(target=serve_callback, daemon=True).start()

    def _complete_gemini_code(
        self,
        job_id: str,
        value: str,
        flow_id: str,
        cancel_event: threading.Event,
    ) -> None:
        try:
            google_oauth.complete_login(
                value,
                probe=False,
                expected_flow_id=flow_id,
                cancel_event=cancel_event,
                commit_guard=lambda: self._lock,
            )
        except Exception:  # noqa: BLE001
            self._update_job(
                job_id,
                state="failed",
                message="Gemini 로그인을 완료하지 못했습니다. 다시 시도해 주세요.",
            )
        else:
            self._update_job(
                job_id,
                state="complete",
                message="Gemini 연결이 완료되었습니다.",
            )
        finally:
            self._close_callback(job_id)
            self._clear_owned_pending("gemini", flow_id)
            with self._lock:
                self._cancel_events.pop(job_id, None)
                self._job_flows.pop(job_id, None)

    def _start_external_login(self, provider: str, command: list[str]) -> Dict[str, Any]:
        fallback_command = " ".join(command)
        executable = shutil.which(command[0])
        if not executable:
            raise ConnectionError(
                f"{command[0]} 명령을 찾을 수 없습니다.",
                code="login_cli_missing",
            )
        command = [executable, *command[1:]]
        with self._lock:
            job = self._create_job(
                provider,
                kind="login",
                state="working",
                message=f"{PROVIDER_LOGIN_OWNERS[provider]} 로그인 창을 여는 중입니다.",
                fallback_command=fallback_command,
            )
            threading.Thread(
                target=self._run_external_login,
                args=(job.id, command),
                daemon=True,
            ).start()
        return job.public()

    def _run_external_login(self, job_id: str, command: list[str]) -> None:
        if self._command_runner is not None:
            self._run_injected_external_login(job_id, command)
            return

        process: subprocess.Popen[str] | None = None
        try:
            with self._lock:
                job = self._jobs.get(job_id)
                if self._closed or job is None or job.state not in ACTIVE_JOB_STATES:
                    return
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    start_new_session=os.name == "posix",
                )
                self._external_processes[job_id] = process
            returncode = process.wait(timeout=10 * 60)
        except subprocess.TimeoutExpired:
            if process is not None:
                self._terminate_external_process(process)
            self._update_job(
                job_id,
                state="failed",
                message="로그인 대기 시간이 만료되었습니다. 다시 시도해 주세요.",
            )
            return
        except OSError:
            self._update_job(
                job_id,
                state="failed",
                message=(
                    "공식 로그인 도구를 실행하지 못했습니다. "
                    "화면의 명령을 터미널에서 실행해 주세요."
                ),
            )
            return
        finally:
            if process is not None:
                with self._lock:
                    current = self._external_processes.get(job_id)
                    if current is process:
                        self._external_processes.pop(job_id, None)
        if returncode != 0:
            self._update_job(
                job_id,
                state="failed",
                message=(
                    "공식 로그인 도구가 완료되지 않았습니다. "
                    "화면의 명령을 터미널에서 다시 실행해 주세요."
                ),
            )
            return
        self._finish_external_login(job_id)

    def _run_injected_external_login(
        self,
        job_id: str,
        command: list[str],
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if self._closed or job is None or job.state not in ACTIVE_JOB_STATES:
                return
        try:
            result = self._command_runner(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10 * 60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._update_job(
                job_id,
                state="failed",
                message="로그인 대기 시간이 만료되었습니다. 다시 시도해 주세요.",
            )
            return
        except OSError:
            self._update_job(
                job_id,
                state="failed",
                message=(
                    "공식 로그인 도구를 실행하지 못했습니다. "
                    "화면의 명령을 터미널에서 실행해 주세요."
                ),
            )
            return
        if result.returncode != 0:
            self._update_job(
                job_id,
                state="failed",
                message=(
                    "공식 로그인 도구가 완료되지 않았습니다. "
                    "화면의 명령을 터미널에서 다시 실행해 주세요."
                ),
            )
            return
        self._finish_external_login(job_id)

    def _finish_external_login(self, job_id: str) -> None:
        provider = self._job(job_id).provider
        try:
            authenticated = self.status(provider)["providers"][provider]["authenticated"]
        except Exception:  # noqa: BLE001
            authenticated = False
        self._update_job(
            job_id,
            state="complete" if authenticated else "failed",
            message=(
                f"{PROVIDER_LABELS[provider]} 로그인이 확인되었습니다."
                if authenticated
                else "로그인 도구는 종료됐지만 계정 상태를 확인하지 못했습니다."
            ),
        )

    def _run_refresh(
        self,
        job_id: str,
        cancel_event: threading.Event,
    ) -> None:
        provider = self._job(job_id).provider
        try:
            self._refresh_provider(
                provider,
                cancel_event=cancel_event,
                commit_guard=lambda: self._refresh_commit_guard(job_id),
            )
            if cancel_event.is_set():
                return
            refreshed = self.status(provider)["providers"][provider]["auth_ready"]
        except Exception:  # noqa: BLE001
            if not cancel_event.is_set():
                self._update_job(
                    job_id,
                    state="failed",
                    message=(
                        f"{PROVIDER_LABELS[provider]} 로그인 정보를 갱신하지 못했습니다. "
                        "다시 시도하거나 다시 로그인해 주세요."
                    ),
                )
            return
        finally:
            with self._lock:
                self._cancel_events.pop(job_id, None)
        if cancel_event.is_set():
            return
        self._update_job(
            job_id,
            state="complete" if refreshed else "failed",
            message=(
                f"{PROVIDER_LABELS[provider]} 로그인 정보가 갱신되었습니다."
                if refreshed
                else (
                    f"{PROVIDER_LABELS[provider]} 로그인 갱신 결과를 확인하지 못했습니다. "
                    "다시 로그인해 주세요."
                )
            ),
        )

    @staticmethod
    def _refresh_provider(
        provider: str,
        *,
        cancel_event: threading.Event,
        commit_guard: Callable[[], Any],
    ) -> Any:
        if provider == "claude":
            return claude_subscription.refresh_access_token(
                cancel_event=cancel_event,
                commit_guard=commit_guard,
            )
        if provider == "grok":
            return grok_oauth.force_refresh_access_token(
                cancel_event=cancel_event,
                commit_guard=commit_guard,
            )
        if provider == "gemini":
            return google_auth.force_refresh_credentials(
                cancel_event=cancel_event,
                commit_guard=commit_guard,
            )
        raise ConnectionError(
            "GPT 세션 갱신은 공식 Codex가 관리합니다.",
            code="refresh_unavailable",
        )

    @contextmanager
    def _refresh_commit_guard(self, job_id: str):
        with self._lock:
            job = self._jobs.get(job_id)
            cancel_event = self._cancel_events.get(job_id)
            if (
                self._closed
                or job is None
                or job.kind != "refresh"
                or job.state not in ACTIVE_JOB_STATES
                or cancel_event is None
                or cancel_event.is_set()
            ):
                raise ConnectionError(
                    "로그인 갱신이 취소되었습니다.",
                    code="refresh_cancelled",
                )
            yield

    def _run_test(
        self,
        job_id: str,
        selected_model: str,
        auth_generation: int,
    ) -> None:
        provider = self._job(job_id).provider
        generated = False
        auth_changed = False
        with self._lock:
            job = self._jobs.get(job_id)
            if self._closed or job is None or job.state not in ACTIVE_JOB_STATES:
                return
            if self._auth_generations[provider] != auth_generation:
                self._update_job(
                    job_id,
                    state="failed",
                    message="연결 상태가 바뀌어 테스트를 다시 실행해야 합니다.",
                )
                return
        try:
            daemon_result = self._daemon_call(
                "agent_hub_execute",
                {
                    "provider": provider,
                    "model": selected_model,
                    "task": {
                        "schema": TASK_SCHEMA,
                        "intent": "Return a short connection acknowledgement.",
                        "capability": "chat",
                        "inline_input": GEMINI_CONNECTION_TEST_PROMPT,
                        "constraints": {
                            "provider_allowlist": [provider],
                            "max_tokens": GEMINI_CONNECTION_TEST_MAX_TOKENS,
                            "timeout_seconds": GEMINI_CONNECTION_TEST_TIMEOUT_SECONDS,
                        },
                        "retention": "ephemeral",
                    },
                },
            )
            daemon_data = daemon_result.get("data") if isinstance(daemon_result, dict) else None
            execution_result = daemon_data.get("result") if isinstance(daemon_data, dict) else None
            generated = bool(
                daemon_result
                and daemon_result.get("success")
                and isinstance(daemon_data, dict)
                and daemon_data.get("provider") == provider
                and isinstance(execution_result, dict)
                and str(
                    execution_result.get("text")
                    or (execution_result.get("data") or {}).get("text")
                    or ""
                ).strip()
            )
            success = bool(daemon_result is not None and generated)
        except Exception:  # noqa: BLE001
            self._update_job(
                job_id,
                state="failed",
                message="연결 테스트에 실패했습니다. 다시 시도해 주세요.",
            )
            return
        with self._lock:
            if self._auth_generations[provider] != auth_generation:
                success = False
                generated = False
                auth_changed = True
            self._update_job(
                job_id,
                state="complete" if success else "failed",
                message=(
                    "연결 상태가 바뀌어 테스트를 다시 실행해야 합니다."
                    if auth_changed
                    else "Gemini 선택 모델의 실제 응답까지 정상입니다."
                    if success and generated
                    else f"{PROVIDER_LABELS[provider]} 선택 모델의 실제 응답까지 정상입니다."
                    if success and generated
                    else "Gemini 선택 모델의 실제 응답을 확인하지 못했습니다."
                    if provider == "gemini"
                    else f"{PROVIDER_LABELS[provider]} 모델 목록을 확인하지 못했습니다."
                ),
            )

    def _create_job(
        self,
        provider: str,
        *,
        kind: str,
        state: str,
        message: str,
        action_url: str | None = None,
        user_code: str | None = None,
        requires_code: bool = False,
        fallback_command: str | None = None,
    ) -> ConnectionJob:
        job = ConnectionJob(
            id=secrets.token_urlsafe(12),
            provider=provider,
            kind=kind,
            state=state,
            message=message,
            action_url=action_url,
            user_code=user_code,
            requires_code=requires_code,
            fallback_command=fallback_command,
        )
        with self._lock:
            self._ensure_open()
            self._prune_jobs()
            self._jobs[job.id] = job
        return job

    def _job(self, job_id: str) -> ConnectionJob:
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            if job is None:
                raise ConnectionError("작업을 찾을 수 없습니다.", code="job_not_found")
            return job

    def _update_job(self, job_id: str, **updates: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            was_active = job.state in ACTIVE_JOB_STATES
            next_state = updates.get("state")
            if (
                job.state not in ACTIVE_JOB_STATES
                and next_state is not None
                and next_state != job.state
            ):
                return
            for key, value in updates.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            job.updated_at = time.time()
            if (
                job.kind in {"login", "refresh"}
                and was_active
                and job.state not in ACTIVE_JOB_STATES
            ):
                self._invalidate_model_catalog(job.provider)

    def _claim_job(
        self,
        job_id: str,
        *,
        expected_state: str,
        state: str,
        message: str,
    ) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state != expected_state:
                return False
            job.state = state
            job.message = message
            job.updated_at = time.time()
            return True

    def _active_job(
        self,
        provider: str,
        *,
        kind: str | None = None,
    ) -> ConnectionJob | None:
        candidates = [
            job
            for job in self._jobs.values()
            if job.provider == provider
            and (kind is None or job.kind == kind)
            and job.state in ACTIVE_JOB_STATES
        ]
        return max(candidates, key=lambda item: item.updated_at) if candidates else None

    def _ensure_provider_idle(self, provider: str, message: str) -> None:
        with self._lock:
            if (
                self._active_job(provider) is not None
                or provider in self._starting_logins
                or provider in self._starting_refreshes
                or provider in self._starting_tests
            ):
                raise ConnectionError(message, code="provider_busy")

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConnectionError(
                "연결 관리가 종료되었습니다.",
                code="manager_closed",
            )

    def _invalidate_model_catalog(self, provider: str) -> None:
        with self._lock:
            self._auth_generations[provider] += 1
            self._model_catalogs.pop(provider, None)

    def _close_callback(self, job_id: str) -> None:
        with self._lock:
            server = self._callback_servers.pop(job_id, None)
        if server is not None:
            try:
                server.server_close()
            except OSError:
                pass

    def _clear_owned_pending(
        self,
        provider: str,
        expected_flow_id: str | None = None,
    ) -> None:
        with self._lock:
            flow_id = self._owned_pending_flows.get(provider)
            if not flow_id:
                return
            if expected_flow_id and not secrets.compare_digest(
                flow_id,
                expected_flow_id,
            ):
                return
        if not self._clear_pending_flow(provider, flow_id):
            return
        with self._lock:
            current = self._owned_pending_flows.get(provider)
            if current and secrets.compare_digest(current, flow_id):
                self._owned_pending_flows.pop(provider, None)

    @staticmethod
    def _clear_pending_flow(
        provider: str,
        flow_id: str | None,
    ) -> bool:
        if not flow_id:
            return False
        try:
            if provider == "grok":
                return bool(
                    grok_oauth.clear_pending_login(
                        expected_flow_id=flow_id,
                    )
                )
            elif provider == "gemini":
                return bool(
                    google_oauth.clear_pending_login(
                        expected_flow_id=flow_id,
                    )
                )
            else:
                return False
        except OSError:
            return False

    @staticmethod
    def _terminate_external_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except OSError:
                pass

    def _prune_jobs(self) -> None:
        cutoff = time.time() - JOB_TTL_SECONDS
        with self._lock:
            expired = [job_id for job_id, job in self._jobs.items() if job.updated_at < cutoff]
            for job_id in expired:
                self._jobs.pop(job_id, None)
            if len(self._jobs) >= MAX_JOBS:
                oldest = sorted(self._jobs.values(), key=lambda item: item.updated_at)
                for job in oldest[: len(self._jobs) - MAX_JOBS + 1]:
                    self._jobs.pop(job.id, None)
