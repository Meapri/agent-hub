"""Classify leaf-tool failures so fallback routing can be smarter than blind rotation."""

from __future__ import annotations

import re
from typing import Tuple

# category -> regex over the (lowercased) error text. First match wins.
_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("auth", r"unauthor|forbidden|\b401\b|\b403\b|invalid[ _-]?api|api[ _-]?key|login|consent|"
             r"token (?:expired|invalid)|credential|not authenticated|permission denied"),
    ("rate_limit", r"rate.?limit|\b429\b|quota|capacity|overload|too many request|throttl"),
    ("timeout", r"timeout|timed out|deadline exceeded|read timed"),
    ("transient", r"\b5\d\d\b|temporarily|connection (?:reset|refused|error)|network|"
                  r"unavailable|try again|econnreset"),
    ("bad_request", r"\b400\b|invalid (?:argument|parameter|request)|schema|unknown property|"
                    r"required|validation"),
)

# Categories where rotating to another provider is worth trying.
ROTATABLE = frozenset({"auth", "rate_limit", "timeout", "transient", "unknown"})


def classify(text: str) -> str:
    t = (text or "").lower()
    for category, pattern in _PATTERNS:
        if re.search(pattern, t):
            return category
    return "unknown"


def should_rotate(category: str) -> bool:
    """A malformed request (bad_request) won't be fixed by a different provider."""
    return category in ROTATABLE


# Transport/backend errors a leaf may hand back as its "successful" text body
# (e.g. Grok returning an HTTP 503 upstream-connect error string). Short + these
# signatures ⇒ the leaf failed even though it reported success.
_LEAF_ERROR_SIGNATURES = re.compile(
    r"upstream connect error|delayed connect error|connection (?:refused|reset|error)|"
    r"transport failure|disconnect/reset|service unavailable|bad gateway|gateway timeout|"
    r"econnrefused|econnreset|read timed out|returned HTTP\s*\d|\bHTTP\s*[45]\d\d\b|"
    r"\b429\b|rate.?limit",
    re.I,
)


def looks_like_leaf_error(text: str, *, max_len: int = 600) -> bool:
    """True when a leaf's response body is really just an error message, not content.

    Guarded by length — real analysis/documents are long, transport errors are short —
    so a legitimate doc that merely mentions '500' or 'timeout' isn't misclassified.
    """
    body = (text or "").strip()
    if not body or len(body) > max_len:
        return False
    return bool(_LEAF_ERROR_SIGNATURES.search(body))
