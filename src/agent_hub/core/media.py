"""Safe, provider-neutral image input normalization."""

from __future__ import annotations

import base64
import ipaddress
import mimetypes
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse


MAX_IMAGE_BYTES = 20 * 1024 * 1024
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
SENSITIVE_PARTS = {".aws", ".azure", ".git", ".gnupg", ".kube", ".ssh"}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _workspace_root(value: Any) -> Path:
    if not str(value or "").strip():
        raise ValueError("workspace_root is required when an image uses a local path")
    root = Path(str(value)).expanduser().resolve()
    if not root.is_dir() or root in {Path(root.anchor), Path.home().resolve()}:
        raise ValueError(f"workspace_root is missing, invalid, or too broad: {root}")
    if any(part.lower() in SENSITIVE_PARTS for part in root.parts):
        raise ValueError(f"workspace_root points to a sensitive location: {root}")
    return root


def _public_https_url(source: str) -> bool:
    parsed = urlparse(source)
    hostname = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname or hostname in {"localhost", "localhost.localdomain"}:
        return False
    if hostname.endswith((".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return bool(address.is_global)


def _data_url(raw: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"


def _validate_data_url(value: str) -> str:
    header, sep, encoded = value.partition(",")
    if not sep or not header.startswith("data:image/") or ";base64" not in header:
        raise ValueError("image data must be a base64 data:image URL")
    mime_type = header.removeprefix("data:").split(";", 1)[0].lower()
    if mime_type not in ALLOWED_MIME_TYPES:
        raise ValueError(f"unsupported image MIME type: {mime_type}")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("image data URL contains invalid base64") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
    return value


def normalize_image(value: Any, *, workspace_root: Any = None) -> Dict[str, str]:
    item = value if isinstance(value, dict) else {"source": value}
    source = str(
        item.get("path")
        or item.get("url")
        or item.get("data")
        or item.get("image_url")
        or item.get("source")
        or ""
    ).strip()
    if not source:
        raise ValueError("each image requires path, url, data, image_url, or source")
    detail = str(item.get("detail") or "auto").strip().lower()
    if detail not in {"auto", "low", "high"}:
        raise ValueError("image detail must be auto, low, or high")
    if source.startswith("data:image/"):
        url = _validate_data_url(source)
        mime_type = source[5:].split(";", 1)[0].lower()
        return {"url": url, "mime_type": mime_type, "detail": detail, "source": "inline"}
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        if not _public_https_url(source):
            raise ValueError("remote images must use a public https URL")
        return {
            "url": source,
            "mime_type": str(item.get("mime_type") or "").lower(),
            "detail": detail,
            "source": "remote",
        }
    root = _workspace_root(workspace_root)
    path = Path(source).expanduser().resolve()
    if not _inside(path, root):
        raise ValueError(f"image path is outside workspace_root: {path}")
    if any(part.lower() in SENSITIVE_PARTS for part in path.parts):
        raise ValueError(f"image path points to a sensitive location: {path}")
    if not path.is_file():
        raise ValueError(f"image path does not exist or is not a file: {path}")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes: {path}")
    mime_type = str(item.get("mime_type") or mimetypes.guess_type(path.name)[0] or "").lower()
    if mime_type not in ALLOWED_MIME_TYPES:
        raise ValueError(f"unsupported image MIME type for {path.name}: {mime_type or 'unknown'}")
    return {
        "url": _data_url(path.read_bytes(), mime_type),
        "mime_type": mime_type,
        "detail": detail,
        "source": str(path),
    }


def normalize_images(values: Any, *, workspace_root: Any = None) -> List[Dict[str, str]]:
    if values is None or values == "":
        return []
    items: Iterable[Any] = values if isinstance(values, list) else [values]
    normalized = [normalize_image(item, workspace_root=workspace_root) for item in items]
    if len(normalized) > 20:
        raise ValueError("at most 20 images may be sent in one Agent Hub call")
    return normalized


def user_content(prompt: str, images: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = [
        {"type": "input_image", "image_url": item["url"], "detail": item["detail"]}
        for item in images
    ]
    if prompt:
        parts.append({"type": "input_text", "text": prompt})
    return parts
