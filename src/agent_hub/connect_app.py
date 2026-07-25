"""Secure localhost web application for managing Agent Hub providers."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from importlib import resources
import json
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .connect_service import ConnectionError, ConnectionManager

MAX_REQUEST_BYTES = 16 * 1024
ASSET_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


class ConnectServer(ThreadingHTTPServer):
    # Active login-start handlers must finish their exact-flow cleanup before
    # the GUI process can exit.
    daemon_threads = False
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        manager: ConnectionManager,
        session_token: str,
    ) -> None:
        self.manager = manager
        self.session_token = session_token
        super().__init__(server_address, ConnectHandler)

    @property
    def origin(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}"

    def server_close(self) -> None:
        close_manager = getattr(getattr(self, "manager", None), "close", None)
        if callable(close_manager):
            close_manager()
        super().server_close()


class ConnectHandler(BaseHTTPRequestHandler):
    server: ConnectServer

    def log_message(self, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/" and self._accept_initial_session(parsed.query):
            return
        if parsed.path in {"/", "/styles.css", "/app.js"}:
            self._static(parsed.path)
            return
        if not self._authenticated():
            self._json_error(
                HTTPStatus.UNAUTHORIZED,
                "이 연결 관리 세션이 만료되었습니다. 앱을 다시 실행해 주세요.",
                "session_required",
            )
            return
        if parsed.path == "/api/status":
            self._action(self.server.manager.status)
            return
        if parsed.path.startswith("/api/jobs/"):
            job_id = urllib.parse.unquote(parsed.path.removeprefix("/api/jobs/"))
            self._action(lambda: self.server.manager.job(job_id))
            return
        parts = [urllib.parse.unquote(item) for item in parsed.path.split("/") if item]
        if (
            len(parts) == 4
            and parts[:2] == ["api", "providers"]
            and parts[3] == "models"
        ):
            refresh = urllib.parse.parse_qs(parsed.query).get("refresh", ["0"])[0]
            self._action(
                lambda: self.server.manager.models(
                    parts[2],
                    refresh=refresh in {"1", "true"},
                )
            )
            return
        self._json_error(HTTPStatus.NOT_FOUND, "페이지를 찾을 수 없습니다.", "not_found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._authenticated() or not self._same_origin():
            self._json_error(
                HTTPStatus.FORBIDDEN,
                "허용되지 않은 요청입니다.",
                "request_forbidden",
            )
            return
        try:
            body = self._request_json()
        except ConnectionError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, str(exc), exc.code)
            return
        parsed = urllib.parse.urlparse(self.path)
        parts = [urllib.parse.unquote(item) for item in parsed.path.split("/") if item]
        if parts == ["api", "shutdown"]:
            self._json(HTTPStatus.OK, {"success": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if len(parts) != 4 or parts[:2] != ["api", "providers"]:
            self._json_error(HTTPStatus.NOT_FOUND, "요청 경로를 찾을 수 없습니다.", "not_found")
            return
        provider, action = parts[2], parts[3]
        actions = {
            "consent": lambda: self.server.manager.grant_consent(
                provider,
                confirmation=str(body.get("confirmation") or ""),
            ),
            "disconnect": lambda: self.server.manager.revoke_consent(
                provider,
                confirmation=str(body.get("confirmation") or ""),
            ),
            "forget-local": lambda: self.server.manager.remove_local_credentials(
                provider,
                confirmation=str(body.get("confirmation") or ""),
            ),
            "login-start": lambda: self.server.manager.start_login(provider),
            "refresh": lambda: self.server.manager.start_refresh(provider),
            "login-complete": lambda: self.server.manager.complete_login(
                provider,
                str(body.get("job_id") or ""),
                str(body.get("code_or_url") or ""),
            ),
            "test": lambda: self.server.manager.start_test(provider),
            "model": lambda: self.server.manager.set_default_model(
                provider,
                str(body.get("model") or ""),
                catalog_revision=str(body.get("catalog_revision") or ""),
            ),
            "model-reset": lambda: self.server.manager.reset_default_model(
                provider,
                confirmation=str(body.get("confirmation") or ""),
            ),
        }
        handler = actions.get(action)
        if handler is None:
            self._json_error(HTTPStatus.NOT_FOUND, "요청 경로를 찾을 수 없습니다.", "not_found")
            return
        self._action(handler)

    def _action(self, operation: Any) -> None:
        try:
            payload = operation()
        except ConnectionError as exc:
            status = (
                HTTPStatus.CONFLICT
                if exc.code
                in {
                    "consent_required",
                    "provider_not_ready",
                    "shared_login_preserved",
                    "provider_busy",
                    "login_in_progress",
                    "test_in_progress",
                    "refresh_in_progress",
                    "refresh_unavailable",
                    "credential_removal_failed",
                }
                else HTTPStatus.BAD_REQUEST
            )
            self._json_error(status, str(exc), exc.code)
        except Exception:  # noqa: BLE001
            self._json_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "요청을 완료하지 못했습니다. 다시 시도해 주세요.",
                "internal_error",
            )
        else:
            self._json(HTTPStatus.OK, payload)

    def _accept_initial_session(self, query: str) -> bool:
        supplied = urllib.parse.parse_qs(query).get("session", [""])[0]
        if not supplied or not secrets.compare_digest(supplied, self.server.session_token):
            return False
        self.send_response(HTTPStatus.SEE_OTHER)
        self._security_headers()
        fragment = urllib.parse.quote(self.server.session_token, safe="")
        self.send_header("Location", f"/#session={fragment}")
        self.end_headers()
        return True

    def _authenticated(self) -> bool:
        supplied = self.headers.get("X-Agent-Hub-Session", "")
        return bool(
            supplied
            and secrets.compare_digest(supplied, self.server.session_token)
        )

    def _same_origin(self) -> bool:
        return (
            self.headers.get("Origin") == self.server.origin
            and self.headers.get("X-Agent-Hub-Intent") == "provider-management"
        )

    def _request_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ConnectionError("잘못된 요청 크기입니다.", code="request_invalid") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ConnectionError("요청이 너무 큽니다.", code="request_too_large")
        if length == 0:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ConnectionError("JSON 요청이 올바르지 않습니다.", code="request_invalid") from exc
        if not isinstance(parsed, dict):
            raise ConnectionError("요청은 JSON 객체여야 합니다.", code="request_invalid")
        return parsed

    def _static(self, path: str) -> None:
        names = {
            "/": "index.html",
            "/styles.css": "styles.css",
            "/app.js": "app.js",
        }
        name = names.get(path)
        if name is None:
            self._json_error(HTTPStatus.NOT_FOUND, "페이지를 찾을 수 없습니다.", "not_found")
            return
        asset = resources.files("agent_hub").joinpath("connect_ui", name)
        content = asset.read_bytes()
        suffix = "." + name.rsplit(".", 1)[-1]
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header("Content-Type", ASSET_TYPES[suffix])
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json_error(self, status: HTTPStatus, message: str, code: str) -> None:
        self._json(
            status,
            {"success": False, "error": {"code": code, "message": message}},
        )

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")


def build_server(
    *,
    port: int = 0,
    manager: ConnectionManager | None = None,
    session_token: str | None = None,
) -> ConnectServer:
    if port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")
    return ConnectServer(
        ("127.0.0.1", port),
        manager or ConnectionManager(),
        session_token or secrets.token_urlsafe(32),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the local Agent Hub connection manager")
    parser.add_argument("--port", type=int, default=0, help="localhost port (default: random)")
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)
    server = build_server(port=args.port)
    url = f"{server.origin}/?session={urllib.parse.quote(server.session_token)}"
    print(f"Agent Hub 연결 관리: {server.origin}")
    print("종료하려면 Ctrl-C를 누르세요.")
    opened = False
    if not args.no_open:
        try:
            opened = bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001
            opened = False
    if args.no_open or not opened:
        print(f"브라우저에서 열기: {url}")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
