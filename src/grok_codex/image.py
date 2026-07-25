"""Grok Imagine image generation with local caching."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse
import urllib.request
import uuid

from agent_hub.core import limits

from . import api, models, paths, response, security


DEFAULT_MODEL = "grok-imagine-image"
MIME_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024


def _destination(extension: str) -> Path:
    directory = paths.cache_dir() / "images"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"grok_{uuid.uuid4().hex}.{extension}"


def _save_url(url: str) -> Path:
    parsed = urlparse(url)
    hostname = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname.endswith(".x.ai"):
        raise ValueError("xAI image response URL must be an x.ai https URL")
    request = urllib.request.Request(url, headers={"User-Agent": "agent-hub/1.0"})
    with urllib.request.urlopen(request, timeout=60) as opened:
        mime_type = str(opened.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        if mime_type not in MIME_EXTENSIONS:
            raise ValueError(f"unsupported generated image MIME type: {mime_type or 'missing'}")
        data = opened.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("generated image exceeds local cache size limit")
    path = _destination(MIME_EXTENSIONS[mime_type])
    path.write_bytes(data)
    return path


def _save_b64(value: str) -> Path:
    raw = base64.b64decode(value, validate=True)
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise ValueError("generated image exceeds local cache size limit")
    extension = "png" if raw.startswith(b"\x89PNG") else "jpg"
    path = _destination(extension)
    path.write_bytes(raw)
    return path


def generate_image(arguments: Dict[str, Any]) -> Dict[str, Any]:
    security.require_consent()
    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    model = str(arguments.get("model") or DEFAULT_MODEL)
    body: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "response_format": str(arguments.get("response_format") or "url"),
        "n": max(1, min(int(arguments.get("n") or 1), 4)),
    }
    if arguments.get("aspect_ratio"):
        body["aspect_ratio"] = {
            "landscape": "16:9",
            "square": "1:1",
            "portrait": "9:16",
        }.get(str(arguments["aspect_ratio"]), str(arguments["aspect_ratio"]))
    if arguments.get("resolution") or arguments.get("image_size"):
        body["resolution"] = str(arguments.get("resolution") or arguments.get("image_size"))
    payload = api.images_generate(
        body,
        timeout=float(
            arguments.get("timeout_sec") or limits.MAX_PROVIDER_TIMEOUT_SECONDS
        ),
    )
    items = payload.get("data") if isinstance(payload.get("data"), list) else []
    if not items or not isinstance(items[0], dict):
        raise ValueError("xAI image response contained no image")
    first = items[0]
    if first.get("b64_json"):
        saved = _save_b64(str(first["b64_json"]))
    elif first.get("url"):
        saved = _save_url(str(first["url"]))
    else:
        raise ValueError("xAI image response contained neither url nor base64 data")
    return {
        "success": True,
        "text": f"Generated image: {saved}",
        "image": str(saved),
        "path": str(saved),
        "size_bytes": saved.stat().st_size,
        "mime_type": "image/png" if saved.suffix == ".png" else "image/jpeg",
        "model": model,
        "prompt": prompt,
        "revised_prompt": str(first.get("revised_prompt") or ""),
        **response.standard_fields(provider="xai", backend="xai-images", model=model),
    }


def list_models() -> list[dict[str, str]]:
    return [item for item in models.CURATED if "imagine-image" in item["id"]]
