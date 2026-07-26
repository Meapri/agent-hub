from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from agent_hub import connect_app
from agent_hub.connect_app import build_server


class FakeManager:
    def __init__(self):
        self.granted = []
        self.forgotten = []
        self.refreshed = []
        self.model_updates = []
        self.model_resets = []
        self.egress_decisions = []
        self.closed = False

    def status(self):
        return {
            "success": True,
            "providers": {},
            "summary": {
                "ready": 0,
                "authenticated": 0,
                "connected": 0,
                "refreshable": 0,
                "relogin_required": 0,
                "consent_required": 0,
                "total": 0,
            },
        }

    def job(self, job_id):
        return {"id": job_id, "state": "complete"}

    def egress_reviews(self):
        return {
            "success": True,
            "schema": "agent_hub_egress_review_list_v1",
            "reviews": [
                {
                    "review_id": "egr_fixture",
                    "provider": "gpt",
                    "model": "gpt-fixture",
                    "destinations": ["gpt"],
                    "entries": [
                        {
                            "path_alias": "README.md",
                            "chars": 100,
                            "classification": "project",
                        }
                    ],
                    "status": "pending",
                }
            ],
            "pending_count": 1,
            "approved_count": 0,
        }

    def decide_egress_review(self, review_id, *, decision):
        self.egress_decisions.append((review_id, decision))
        return {"success": True, "review": {"review_id": review_id, "status": "approved"}}

    def start_refresh(self, provider):
        self.refreshed.append(provider)
        return {
            "success": True,
            "id": "refresh-job",
            "provider": provider,
            "kind": "refresh",
            "state": "working",
        }

    def grant_consent(self, provider, *, confirmation):
        self.granted.append((provider, confirmation))
        return {"success": True, "provider": provider}

    def models(self, provider, *, refresh=False):
        return {
            "success": True,
            "provider": provider,
            "models": [
                {
                    "id": "test-model",
                    "display": "Test Model",
                    "source": "test",
                    "selectable": True,
                }
            ],
            "catalog_revision": "test-revision",
            "refreshed": refresh,
        }

    def set_default_model(self, provider, model, *, catalog_revision):
        self.model_updates.append((provider, model, catalog_revision))
        return {
            "success": True,
            "provider": provider,
            "selected_model": model,
        }

    def reset_default_model(self, provider, *, confirmation):
        self.model_resets.append((provider, confirmation))
        return {
            "success": True,
            "provider": provider,
            "selected_model": "test-model",
        }

    def remove_local_credentials(self, provider, *, confirmation):
        self.forgotten.append((provider, confirmation))
        return {
            "success": True,
            "provider": provider,
            "removed": True,
        }

    def close(self):
        self.closed = True


def test_no_open_prints_the_bootstrap_session_url(monkeypatch, capsys):
    class FakeServer:
        origin = "http://127.0.0.1:4567"
        session_token = "session-token"

        def serve_forever(self, **_kwargs):
            return None

        def server_close(self):
            return None

    monkeypatch.setattr(connect_app, "build_server", lambda **_kwargs: FakeServer())

    assert connect_app.main(["--no-open"]) == 0
    output = capsys.readouterr().out
    assert "브라우저에서 열기: http://127.0.0.1:4567/?session=session-token" in output


def _running_server(manager=None):
    manager = manager or FakeManager()
    server = build_server(manager=manager, session_token="test-session")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, manager, thread


def _opener():
    return urllib.request.build_opener()


def _session_request(url, *, data=None, method=None, headers=None):
    return urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "X-Agent-Hub-Session": "test-session",
            **(headers or {}),
        },
    )


def test_initial_nonce_bootstraps_tab_session_and_serves_assets():
    assert connect_app.ConnectServer.daemon_threads is False
    server, _manager, thread = _running_server()
    opener = _opener()
    try:
        with opener.open(f"{server.origin}/?session=test-session") as response:
            html = response.read().decode()
        assert "모델 연결 관리" in html
        assert 'id="detail-panel" tabindex="-1"' in html
        assert 'id="job-announcer"' in html

        with opener.open(f"{server.origin}/styles.css") as response:
            css = response.read().decode()
        assert "--blue: #1769e0" in css

        with opener.open(f"{server.origin}/app.js") as response:
            javascript = response.read().decode()
        assert "function clearModelCatalog(provider)" in javascript
        assert "function renderEgressReviews()" in javascript
        assert "data-egress-action" in javascript
        assert (
            'if (["login", "refresh"].includes(job.kind)) clearModelCatalog(provider);'
            in javascript
        )
        assert "providerAuthFingerprint(previous)" in javascript
        assert "최신 모델 목록 불러오기" in javascript
        assert (
            "loadModels({ refresh: Boolean(provider?.ready || provider?.invocation_ready) })"
            in javascript
        )
        assert "다음 요청에서 세션을 자동으로 갱신합니다." in javascript
        assert "소량의 사용량이 발생합니다." in javascript
        assert "async function startRefresh()" in javascript
        assert (
            'data-action="${autoRefresh ? "test" : refreshable ? "refresh" : "test"}"' in javascript
        )
        assert "세션 갱신" in javascript
        assert "const authStepComplete = authComplete && !reloginRequired;" in javascript
        assert "function restoreDetailFocus({ detailBusy })" in javascript
        assert "function announceJob(job)" in javascript
        assert '$("#job-announcer").textContent = text;' in javascript
        assert "provider.local_credentials_present || provider.pending_login_present" in javascript
        assert "if (state.forgetInFlight) return;" in javascript
        assert "state.forgetInFlight = provider;" in javascript
        assert 'setBusy(provider, "forget", true);' in javascript
        assert "clearProviderJob(provider);" in javascript
        assert '$("#forget-dialog").addEventListener("cancel"' in javascript
        assert "event.preventDefault();" in javascript
        assert javascript.index(
            "clearProviderJob(provider);",
            javascript.index("async function forgetLocal"),
        ) < javascript.index(
            "} catch (error) {",
            javascript.index("async function forgetLocal"),
        )

        with opener.open(_session_request(f"{server.origin}/api/status")) as response:
            payload = json.load(response)
        assert payload["success"] is True
        assert response.headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_refresh_uses_authenticated_same_origin_post():
    server, manager, thread = _running_server()
    opener = _opener()
    try:
        opener.open(f"{server.origin}/?session=test-session").read()
        missing_origin = urllib.request.Request(
            f"{server.origin}/api/providers/gemini/refresh",
            data=b"{}",
            method="POST",
            headers={"X-Agent-Hub-Session": "test-session"},
        )
        with pytest.raises(urllib.error.HTTPError) as origin_error:
            opener.open(missing_origin)
        assert origin_error.value.code == 403

        missing_session = urllib.request.Request(
            f"{server.origin}/api/providers/gemini/refresh",
            data=b"{}",
            method="POST",
            headers={
                "Origin": server.origin,
                "X-Agent-Hub-Intent": "provider-management",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as session_error:
            opener.open(missing_session)
        assert session_error.value.code == 403
        assert manager.refreshed == []

        headers = {
            "Content-Type": "application/json",
            "Origin": server.origin,
            "X-Agent-Hub-Intent": "provider-management",
            "X-Agent-Hub-Session": "test-session",
        }
        request = urllib.request.Request(
            f"{server.origin}/api/providers/gemini/refresh",
            data=b"{}",
            method="POST",
            headers=headers,
        )

        with opener.open(request) as response:
            payload = json.load(response)

        assert payload["kind"] == "refresh"
        assert manager.refreshed == ["gemini"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_api_rejects_missing_session_header():
    server, _manager, thread = _running_server()
    try:
        urllib.request.urlopen(f"{server.origin}/api/status")
    except urllib.error.HTTPError as error:
        assert error.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_api_session_is_header_scoped_and_does_not_accept_localhost_cookie():
    server, _manager, thread = _running_server()
    try:
        request = urllib.request.Request(
            f"{server.origin}/api/status",
            headers={"Cookie": "agent_hub_session=test-session"},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_mutation_requires_same_origin_and_visible_intent_header():
    server, manager, thread = _running_server()
    opener = _opener()
    try:
        opener.open(f"{server.origin}/?session=test-session").read()
        body = json.dumps({"confirmation": "connect:claude"}).encode()
        missing_origin = urllib.request.Request(
            f"{server.origin}/api/providers/claude/consent",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Agent-Hub-Session": "test-session",
            },
        )
        try:
            opener.open(missing_origin)
        except urllib.error.HTTPError as error:
            assert error.code == 403
        else:
            raise AssertionError("mutation without origin was accepted")

        allowed = urllib.request.Request(
            f"{server.origin}/api/providers/claude/consent",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": server.origin,
                "X-Agent-Hub-Intent": "provider-management",
                "X-Agent-Hub-Session": "test-session",
            },
        )
        with opener.open(allowed) as response:
            payload = json.load(response)
        assert payload["success"] is True
        assert manager.granted == [("claude", "connect:claude")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_egress_review_requires_authenticated_visible_gui_action():
    server, manager, thread = _running_server()
    opener = _opener()
    try:
        opener.open(f"{server.origin}/?session=test-session").read()
        with opener.open(_session_request(f"{server.origin}/api/egress-reviews")) as response:
            listed = json.load(response)
        assert listed["reviews"][0]["review_id"] == "egr_fixture"

        blocked = urllib.request.Request(
            f"{server.origin}/api/egress-reviews/egr_fixture/approve",
            data=b"{}",
            method="POST",
            headers={"X-Agent-Hub-Session": "test-session"},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(blocked)
        assert error.value.code == 403
        assert manager.egress_decisions == []

        approved = urllib.request.Request(
            f"{server.origin}/api/egress-reviews/egr_fixture/approve",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": server.origin,
                "X-Agent-Hub-Intent": "provider-management",
                "X-Agent-Hub-Session": "test-session",
            },
        )
        with opener.open(approved) as response:
            payload = json.load(response)
        assert payload["review"]["status"] == "approved"
        assert manager.egress_decisions == [("egr_fixture", "approve")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_forget_local_failure_returns_conflict():
    class FailingManager(FakeManager):
        def remove_local_credentials(self, provider, *, confirmation):
            del provider, confirmation
            raise connect_app.ConnectionError(
                "로컬 로그인 정보를 삭제하지 못했습니다.",
                code="credential_removal_failed",
            )

    server, _manager, thread = _running_server(FailingManager())
    opener = _opener()
    try:
        opener.open(f"{server.origin}/?session=test-session").read()
        request = urllib.request.Request(
            f"{server.origin}/api/providers/gemini/forget-local",
            data=json.dumps({"confirmation": "forget-local:gemini"}).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": server.origin,
                "X-Agent-Hub-Intent": "provider-management",
                "X-Agent-Hub-Session": "test-session",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as raised:
            opener.open(request)
        payload = json.load(raised.value)
        assert raised.value.code == 409
        assert payload["error"]["code"] == "credential_removal_failed"
        assert "경로" not in str(payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_status_failure_returns_redacted_json_error():
    class FailingManager(FakeManager):
        def status(self):
            raise RuntimeError("access_token=secret")

    server, manager, thread = _running_server(FailingManager())
    opener = _opener()
    try:
        opener.open(f"{server.origin}/?session=test-session").read()
        try:
            opener.open(_session_request(f"{server.origin}/api/status"))
        except urllib.error.HTTPError as error:
            payload = json.load(error)
            assert error.code == 500
            assert payload["error"]["code"] == "internal_error"
            assert "secret" not in str(payload)
        else:
            raise AssertionError("status failure was not converted to JSON")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
    assert manager.closed is True


def test_manager_closes_when_server_bind_fails(monkeypatch):
    manager = FakeManager()

    def fail_bind(_server):
        raise OSError("address in use")

    monkeypatch.setattr(
        "agent_hub.connect_app.ThreadingHTTPServer.server_bind",
        fail_bind,
    )

    with pytest.raises(OSError, match="address in use"):
        build_server(manager=manager, session_token="test-session")

    assert manager.closed is True


def test_model_catalog_and_mutations_use_authenticated_local_api():
    server, manager, thread = _running_server()
    opener = _opener()
    try:
        opener.open(f"{server.origin}/?session=test-session").read()
        with opener.open(
            _session_request(f"{server.origin}/api/providers/claude/models?refresh=1")
        ) as response:
            catalog = json.load(response)
        assert catalog["models"][0]["id"] == "test-model"
        assert catalog["refreshed"] is True

        headers = {
            "Content-Type": "application/json",
            "Origin": server.origin,
            "X-Agent-Hub-Intent": "provider-management",
            "X-Agent-Hub-Session": "test-session",
        }
        save = urllib.request.Request(
            f"{server.origin}/api/providers/claude/model",
            data=json.dumps(
                {
                    "model": "test-model",
                    "catalog_revision": "test-revision",
                }
            ).encode(),
            method="POST",
            headers=headers,
        )
        with opener.open(save) as response:
            assert json.load(response)["selected_model"] == "test-model"

        reset = urllib.request.Request(
            f"{server.origin}/api/providers/claude/model-reset",
            data=json.dumps({"confirmation": "reset-model:claude"}).encode(),
            method="POST",
            headers=headers,
        )
        with opener.open(reset) as response:
            assert json.load(response)["success"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert manager.model_updates == [("claude", "test-model", "test-revision")]
    assert manager.model_resets == [("claude", "reset-model:claude")]
