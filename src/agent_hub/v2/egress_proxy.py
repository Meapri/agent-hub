"""Ephemeral localhost proxy that enforces provider manifest domains."""

from __future__ import annotations

import select
import socket
import socketserver
import threading
from typing import Any
from urllib.parse import urlsplit

from agent_hub.core.limits import MAX_PROVIDER_TIMEOUT_SECONDS

from .errors import HubV2Error

MAX_PROXY_HEADER_BYTES = 64 * 1024
PROXY_IDLE_TIMEOUT_SECONDS = float(MAX_PROVIDER_TIMEOUT_SECONDS)


def _allowed(host: str, domains: frozenset[str]) -> bool:
    normalized = host.rstrip(".").lower()
    return any(normalized == domain or normalized.endswith(f".{domain}") for domain in domains)


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, domains: frozenset[str]) -> None:
        super().__init__(("127.0.0.1", 0), _ProxyHandler)
        self.domains = domains


class _ProxyHandler(socketserver.BaseRequestHandler):
    server: _ProxyServer

    def _header(self) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_PROXY_HEADER_BYTES:
                raise ValueError("header_too_large")
        return bytes(data)

    @staticmethod
    def _relay(left: socket.socket, right: socket.socket) -> None:
        sockets = [left, right]
        while True:
            readable, _, _ = select.select(
                sockets,
                [],
                [],
                PROXY_IDLE_TIMEOUT_SECONDS,
            )
            if not readable:
                return
            for source in readable:
                payload = source.recv(65536)
                if not payload:
                    return
                (right if source is left else left).sendall(payload)

    def handle(self) -> None:
        try:
            header = self._header()
            first, _, remainder = header.partition(b"\r\n")
            parts = first.decode("ascii", "strict").split()
            if len(parts) != 3:
                return
            method, target, version = parts
            if method.upper() == "CONNECT":
                host, separator, port_text = target.rpartition(":")
                if not separator or not _allowed(host, self.server.domains):
                    self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                    return
                port = int(port_text)
                upstream = socket.create_connection((host, port), timeout=15.0)
                try:
                    self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    self._relay(self.request, upstream)
                finally:
                    upstream.close()
                return
            parsed = urlsplit(target)
            host = parsed.hostname or ""
            if not _allowed(host, self.server.domains):
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if parsed.scheme == "https":
                self.request.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            upstream = socket.create_connection((host, port), timeout=15.0)
            try:
                upstream.sendall(f"{method} {path} {version}\r\n".encode("ascii") + remainder)
                self._relay(self.request, upstream)
            finally:
                upstream.close()
        except (OSError, UnicodeError, ValueError):
            try:
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except OSError:
                pass


class ProviderEgressProxy:
    def __init__(self, domains: list[str]) -> None:
        normalized = frozenset(
            str(domain).strip().lower().rstrip(".") for domain in domains if domain
        )
        if not normalized:
            raise HubV2Error(
                "provider_egress_unavailable",
                "The provider manifest has no approved egress domains.",
                scope="provider",
            )
        self._server = _ProxyServer(normalized)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            daemon=True,
            name="agent-hub-provider-egress-proxy",
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> "ProviderEgressProxy":
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)

    def __enter__(self) -> "ProviderEgressProxy":
        return self.start()

    def __exit__(self, *_args: Any) -> None:
        self.close()
