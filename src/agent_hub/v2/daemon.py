"""Threaded Unix-socket daemon for the v2 local runtime."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import threading
from typing import Any, Mapping

from .errors import HubV2Error, public_failure, safe_unexpected_error
from .service import HubService
from .store import DEFAULT_DB_NAME, DEFAULT_STATE_DIR, HubStore
from .tools import tool_definitions

DEFAULT_SOCKET_PATH = DEFAULT_STATE_DIR / "run" / "agent-hub.sock"
MAX_DAEMON_REQUEST_BYTES = 8 * 1024 * 1024


def _safe_socket_parent(path: Path) -> Path:
    parent = path.expanduser().parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = parent.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise HubV2Error(
            "unsafe_socket_path",
            "The daemon socket directory must be a user-owned directory.",
            scope="daemon",
        )
    try:
        os.chmod(parent, 0o700)
    except OSError as exc:
        raise HubV2Error(
            "unsafe_socket_path",
            "The daemon socket directory permissions could not be secured.",
            scope="daemon",
        ) from exc
    return parent.resolve(strict=True)


def _prepare_socket(path: Path) -> Path:
    parent = _safe_socket_parent(path)
    target = parent / path.name
    try:
        info = target.lstat()
    except FileNotFoundError:
        return target
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise HubV2Error(
            "unsafe_socket_path",
            "The daemon socket target is not a user-owned socket.",
            scope="daemon",
        )
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(str(target))
    except OSError:
        target.unlink()
        return target
    finally:
        probe.close()
    raise HubV2Error(
        "daemon_already_running",
        "Another Agent Hub v2 daemon is already listening.",
        scope="daemon",
    )


class _ThreadingUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False
    allow_reuse_address = False


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_DAEMON_REQUEST_BYTES + 1)
        if len(raw) > MAX_DAEMON_REQUEST_BYTES:
            response = public_failure(
                HubV2Error(
                    "request_too_large",
                    "The daemon request exceeds the local limit.",
                    scope="daemon",
                ),
                operation="daemon",
            )
        else:
            try:
                parsed = json.loads(raw.decode("utf-8"))
                if not isinstance(parsed, Mapping):
                    raise ValueError
                response = self.server.dispatch(parsed)  # type: ignore[attr-defined]
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                response = public_failure(
                    HubV2Error(
                        "invalid_request",
                        "The daemon request is invalid.",
                        scope="daemon",
                    ),
                    operation="daemon",
                )
            except Exception:  # noqa: BLE001
                response = safe_unexpected_error(operation="daemon", scope="daemon")
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
        self.wfile.flush()


class HubDaemon:
    def __init__(self, *, socket_path: str | Path, store: HubStore) -> None:
        self.socket_path = Path(socket_path).expanduser()
        self.service = HubService(store)
        self._closing = threading.Event()
        self._recovery_thread: threading.Thread | None = None
        target = _prepare_socket(self.socket_path)
        self._server = _ThreadingUnixServer(str(target), _Handler)
        self._server.dispatch = self.dispatch  # type: ignore[attr-defined]
        os.chmod(target, 0o600)
        self.socket_path = target

    def dispatch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        method = str(request.get("method") or "")
        if method == "ping":
            result = {"success": True, "operation": "ping", "data": {}}
        elif method == "tools/list":
            result = {
                "success": True,
                "operation": "tools/list",
                "data": {"tools": tool_definitions()},
            }
        elif method == "tools/call":
            params = request.get("params")
            if not isinstance(params, Mapping):
                result = public_failure(
                    HubV2Error(
                        "invalid_request",
                        "tools/call params must be an object.",
                        scope="daemon",
                    ),
                    operation="tools/call",
                )
            else:
                result = self.service.dispatch(
                    str(params.get("name") or ""),
                    params.get("arguments") or {},
                )
        elif method == "egress/reviews":
            try:
                result = {
                    "success": True,
                    "operation": method,
                    "error": None,
                    "data": self.service.pending_egress_reviews(),
                }
            except HubV2Error as exc:
                result = public_failure(exc, operation=method)
        elif method == "egress/settings":
            try:
                result = {
                    "success": True,
                    "operation": method,
                    "error": None,
                    "data": self.service.egress_settings(),
                }
            except HubV2Error as exc:
                result = public_failure(exc, operation=method)
        elif method == "egress/settings/update":
            params = request.get("params")
            if not isinstance(params, Mapping):
                result = public_failure(
                    HubV2Error(
                        "invalid_request",
                        "The global egress setting must be an object.",
                        scope="egress",
                    ),
                    operation=method,
                )
            else:
                try:
                    result = {
                        "success": True,
                        "operation": method,
                        "error": None,
                        "data": self.service.update_egress_settings(
                            auto_approve=params.get("auto_approve"),  # type: ignore[arg-type]
                            expected_revision=params.get("expected_revision"),  # type: ignore[arg-type]
                        ),
                    }
                except HubV2Error as exc:
                    result = public_failure(exc, operation=method)
        elif method == "egress/decide":
            params = request.get("params")
            if not isinstance(params, Mapping):
                result = public_failure(
                    HubV2Error(
                        "invalid_request",
                        "The egress decision must be an object.",
                        scope="egress",
                    ),
                    operation=method,
                )
            else:
                try:
                    result = {
                        "success": True,
                        "operation": method,
                        "error": None,
                        "data": self.service.decide_egress_review(
                            str(params.get("review_id") or ""),
                            decision=str(params.get("decision") or ""),
                        ),
                    }
                except HubV2Error as exc:
                    result = public_failure(exc, operation=method)
        else:
            result = public_failure(
                HubV2Error(
                    "unknown_method",
                    "The daemon method is not supported.",
                    scope="daemon",
                ),
                operation=method or "daemon",
            )
        return {"id": request_id, "result": result}

    def _recover_once(self) -> None:
        recovery = self.service.store.recover_expired_leases()
        for item in recovery["retryable_runs"]:
            self.service.dispatch(
                "agent_hub_continue",
                {
                    "run_id": item["run_id"],
                    "expected_revision": item["revision"],
                },
            )

    def _recovery_loop(self) -> None:
        while not self._closing.wait(2.0):
            try:
                self._recover_once()
            except HubV2Error:
                continue

    def serve_forever(self) -> None:
        self._recover_once()
        self._recovery_thread = threading.Thread(
            target=self._recovery_loop,
            daemon=True,
            name="agent-hub-v2-lease-recovery",
        )
        self._recovery_thread.start()
        self._server.serve_forever(poll_interval=0.2)

    def serve_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        self._closing.set()
        self._server.shutdown()
        self._server.server_close()
        if self._recovery_thread is not None:
            self._recovery_thread.join(timeout=3.0)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


class HubDaemonClient:
    def __init__(self, socket_path: str | Path = DEFAULT_SOCKET_PATH) -> None:
        self.socket_path = Path(socket_path).expanduser()

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        request = {"id": "request", "method": method, "params": dict(params or {})}
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(timeout)
            client.connect(str(self.socket_path))
            client.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            chunks = bytearray()
            while b"\n" not in chunks:
                piece = client.recv(65536)
                if not piece:
                    break
                chunks.extend(piece)
                if len(chunks) > MAX_DAEMON_REQUEST_BYTES:
                    raise HubV2Error(
                        "daemon_response_too_large",
                        "The daemon response exceeds the local limit.",
                        scope="daemon",
                    )
        except (OSError, TimeoutError) as exc:
            raise HubV2Error(
                "daemon_unavailable",
                "The Agent Hub v2 daemon is not reachable.",
                scope="daemon",
                retryable=True,
                next_action={"type": "start_local_daemon", "command": "agent-hubd"},
            ) from exc
        finally:
            client.close()
        try:
            parsed = json.loads(bytes(chunks).split(b"\n", 1)[0])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HubV2Error(
                "daemon_protocol_error",
                "The daemon returned invalid JSON.",
                scope="daemon",
            ) from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("result"), dict):
            raise HubV2Error(
                "daemon_protocol_error",
                "The daemon response is invalid.",
                scope="daemon",
            )
        return parsed["result"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Agent Hub v2 local daemon.")
    parser.add_argument("--socket", default=str(DEFAULT_SOCKET_PATH))
    parser.add_argument(
        "--state-db",
        default=str(DEFAULT_STATE_DIR / DEFAULT_DB_NAME),
    )
    args = parser.parse_args(argv)
    daemon = HubDaemon(socket_path=args.socket, store=HubStore(args.state_db))
    try:
        daemon.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
