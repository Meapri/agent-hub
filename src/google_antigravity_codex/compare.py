"""Short side-by-side model comparison for the same prompt."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from . import chat, model_prefs, response


def compare_models(arguments: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    raw_models = arguments.get("models")
    models: List[str] = []
    if isinstance(raw_models, list):
        models = [model_prefs.normalize_model_id(str(m)) for m in raw_models if str(m).strip()]
    elif isinstance(raw_models, str) and raw_models.strip():
        models = [
            model_prefs.normalize_model_id(part)
            for part in raw_models.replace(";", ",").split(",")
            if part.strip()
        ]
    if len(models) < 2:
        # default pair
        a = model_prefs.resolve_model(task="chat", fallback=chat.DEFAULT_MODEL)
        b = model_prefs.resolve_model(task="code", fallback="gemini-3.1-pro-high")
        models = [a, b if b != a else "gemini-3.5-flash-high"]
    models = models[:3]  # hard cap cost

    max_tokens = int(arguments.get("max_tokens") or 256)
    temperature = arguments.get("temperature", 0.2)
    timeout = int(arguments.get("timeout_sec") or 90)
    results: List[Dict[str, Any]] = []
    for model in models:
        started = time.time()
        entry: Dict[str, Any] = {"model": model, "success": False}
        try:
            out = chat.run_chat(
                {
                    "prompt": prompt,
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "timeout_sec": timeout,
                    "retry_count": 0,
                    "grounding": str(arguments.get("grounding") or "off"),
                }
            )
            elapsed_ms = int((time.time() - started) * 1000)
            entry.update(
                {
                    "success": True,
                    "text": str(out.get("text") or "")[:4000],
                    "elapsed_ms": elapsed_ms,
                    "usage": out.get("usage") or {},
                    "backend": out.get("backend"),
                    "capacity_fallback": bool((out.get("diagnostics") or {}).get("capacity_fallback")),
                    "used_model": (out.get("diagnostics") or {}).get("used_model") or model,
                    "warnings": out.get("warnings") or [],
                }
            )
        except Exception as exc:
            entry.update(
                {
                    "error_type": getattr(exc, "code", type(exc).__name__),
                    "error": str(exc)[:300],
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
            )
        results.append(entry)

    ok = sum(1 for r in results if r.get("success"))
    lines = [f"Compared {len(results)} models ({ok} ok) for the same prompt."]
    for r in results:
        if r.get("success"):
            lines.append(
                f"- {r['model']}: {r.get('elapsed_ms')}ms"
                + (f" (fell back to {r.get('used_model')})" if r.get("capacity_fallback") else "")
            )
        else:
            lines.append(f"- {r['model']}: FAILED ({r.get('error_type')})")
    return {
        "text": "\n".join(lines),
        "success": ok > 0,
        "prompt_preview": prompt[:200],
        "results": results,
        **response.standard_fields(
            backend="compare",
            warnings=[] if ok == len(results) else ["partial_compare_failures"],
        ),
    }
