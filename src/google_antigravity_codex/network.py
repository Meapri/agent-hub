"""Conservative URL validation for plugin-controlled downloads and redirects."""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse
import urllib.request

from .security import env_flag


def validate_public_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    allowed_schemes = {"https"}
    if env_flag("GOOGLE_ANTIGRAVITY_ALLOW_HTTP_DOWNLOADS"):
        allowed_schemes.add("http")
    if parsed.scheme.lower() not in allowed_schemes:
        raise ValueError("URL must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL must have a public hostname and no embedded credentials")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"URL hostname could not be resolved: {parsed.hostname}") from exc
    if not addresses:
        raise ValueError(f"URL hostname returned no addresses: {parsed.hostname}")
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise ValueError(f"URL resolves to a private or non-global address: {address}")
    return url


class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def public_url_opener() -> urllib.request.OpenerDirector:
    # Ignore ambient proxy variables so a configured internal proxy cannot turn a
    # public-looking URL into access to an internal destination.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirectHandler())


def max_download_bytes() -> int:
    try:
        value = int(os.getenv("GOOGLE_ANTIGRAVITY_MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))
    except ValueError:
        value = 10 * 1024 * 1024
    return max(1, min(value, 50 * 1024 * 1024))


def read_limited(response, limit: int) -> bytes:  # type: ignore[no-untyped-def]
    raw_length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
    if raw_length:
        try:
            if int(raw_length) > limit:
                raise ValueError(f"download exceeds the {limit}-byte size limit")
        except ValueError as exc:
            if "size limit" in str(exc):
                raise
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"download exceeds the {limit}-byte size limit")
    return data
